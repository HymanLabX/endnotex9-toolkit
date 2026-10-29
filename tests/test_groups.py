"""Reading EndNote's group tree out of the library's MyISAM tables.

The fixtures build a real `misc.MYD` byte-for-byte rather than mocking the
reader, because the whole point of this module is that the on-disk layout was
reverse-engineered — a test against a mock would prove nothing about it.
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from endnotex9_toolkit import browse, db, groups, indexer, myisam

# --------------------------------------------------------------------------
# building a MyISAM file to read back
# --------------------------------------------------------------------------


def _misc_row(code: int, subcode: int | None, code_name: str, value: bytes) -> bytes:
    """One packed `misc` row: flags, integers, length-prefixed name, value."""
    ints = [code] if subcode is None else [code, subcode]
    body = bytes([0x0A if subcode is None else 0x08])
    for number in ints:
        body += number.to_bytes(4, "little")
    body += bytes([len(code_name)]) + code_name.encode("ascii")
    body += len(value).to_bytes(3, "little") + value
    return body


def _block(payload: bytes) -> bytes:
    """Wrap a record as a type-3 block (whole record, 2-byte length, padded
    to the 4-byte alignment EndNote's files use)."""
    pad = (-(len(payload) + 4)) % 4
    return b"\x03" + struct.pack(">H", len(payload)) + bytes([pad]) + payload + b"\x00" * pad


def _split_block(payload: bytes, first_len: int, total_file_pos: int) -> tuple[bytes, bytes]:
    """A type-5 first block plus the type-7 last block it points at."""
    head = payload[:first_len]
    tail = payload[first_len:]
    first = (
        b"\x05"
        + struct.pack(">H", len(payload))
        + struct.pack(">H", len(head))
        + struct.pack(">Q", total_file_pos)
        + head
    )
    last = b"\x07" + struct.pack(">H", len(tail)) + tail
    return first, last


def _rec_list(rec_numbers: list[int]) -> bytes:
    return (
        struct.pack(">I", 2)
        + struct.pack("<I", len(rec_numbers))
        + b"".join(struct.pack("<I", n) for n in rec_numbers)
    )


def _group_xml(uuid: str, name: str, smart_query: str = "") -> bytes:
    rules = "<rule>TYPE;3</rule>"
    if smart_query:
        escaped = smart_query.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        rules = (
            f"<rule>TYPE;4</rule><rule>SEARCH;&lt;ENQuery&gt;&lt;FlatQuery&gt;{escaped}"
            f"&lt;/FlatQuery&gt;&lt;/ENQuery&gt;</rule>"
        )
    return (
        f'<?xml version="1.0" encoding="UTF-8"?><group version="1"><ids>'
        f"<id>{uuid}</id><name>{name}</name></ids>"
        f"<rules>{rules}</rules></group>"
    ).encode()


def _groupset_xml(name: str, members: list[str]) -> bytes:
    member_xml = "".join(f"<member>{m}</member>" for m in members)
    return (
        f'<?xml version="1.0" encoding="UTF-8"?><groupset version="1"><ids>'
        f"<id>SET-0001</id><name>{name}</name></ids>"
        f'<members complex="true">{member_xml}</members></groupset>'
    ).encode()


UUID_A, UUID_B, UUID_C = "AAAA-0001", "BBBB-0002", "CCCC-0003"


@pytest.fixture
def rdb(tmp_path: Path) -> Path:
    """A minimal but genuine rdb folder: three groups, one of them smart, one
    of them stored across two blocks."""
    folder = tmp_path / "Test.Data" / "rdb"
    folder.mkdir(parents=True)

    blocks = [
        _block(_misc_row(1, None, "DbVersion", b"10")),
        _block(_misc_row(groups.CODE_GROUP_SPEC, 1, "GroupSpec", _group_xml(UUID_A, "Ni-Ti SMAs"))),
        _block(_misc_row(groups.CODE_GROUP_REC_LIST, 1, "GroupRecList", _rec_list([1, 2, 4]))),
        _block(
            _misc_row(groups.CODE_GROUP_SPEC, 2, "GroupSpec", _group_xml(UUID_B, "Empty Shelf"))
        ),
        _block(_misc_row(groups.CODE_GROUP_REC_LIST, 2, "GroupRecList", _rec_list([]))),
        _block(
            _misc_row(
                groups.CODE_GROUP_SPEC,
                3,
                "GroupSpec",
                _group_xml(UUID_C, "No Attachments", smart_query="('File Attachments' IS [])"),
            )
        ),
        _block(_misc_row(groups.CODE_GROUP_REC_LIST, 3, "GroupRecList", _rec_list([2, 3, 999]))),
    ]

    # The group set is written as a split record, so the reader's
    # continuation handling is exercised by every test in this file.
    payload = _misc_row(
        groups.CODE_GROUPSET_SPEC, 1, "GroupsetSpec", _groupset_xml("My Groups", [UUID_A, UUID_B])
    )
    prefix_len = sum(len(b) for b in blocks)
    first_len = len(payload) // 3
    first_block_span = 13 + first_len
    first, last = _split_block(payload, first_len, prefix_len + first_block_span)
    blocks += [first, last]

    (folder / "misc.MYD").write_bytes(b"".join(blocks))
    return folder


# --------------------------------------------------------------------------
# the MyISAM reader
# --------------------------------------------------------------------------


def test_reader_walks_the_file_and_returns_every_record(rdb):
    records = myisam.read_records(rdb / "misc.MYD")
    assert len(records) == 8  # 7 whole-record blocks + 1 split record


def test_reader_reassembles_a_split_record(rdb):
    joined = b"".join(myisam.read_records(rdb / "misc.MYD"))
    assert b"GroupsetSpec" in joined
    assert b"</groupset>" in joined  # the tail block was followed


def test_reader_rejects_a_truncated_file(rdb, tmp_path: Path):
    broken = tmp_path / "broken.MYD"
    broken.write_bytes(myisam.read_records.__module__.encode())  # not a MyISAM file at all
    with pytest.raises(myisam.MyISAMError):
        myisam.read_records(broken)


# --------------------------------------------------------------------------
# the group tree
# --------------------------------------------------------------------------


def test_reads_every_group_with_members(rdb):
    found = {g.name: g for g in groups.read_groups(rdb)}
    assert set(found) == {"Ni-Ti SMAs", "Empty Shelf", "No Attachments"}
    assert found["Ni-Ti SMAs"].rec_numbers == [1, 2, 4]
    assert found["Empty Shelf"].rec_numbers == []


def test_distinguishes_smart_groups_and_keeps_their_query(rdb):
    found = {g.name: g for g in groups.read_groups(rdb)}
    assert found["Ni-Ti SMAs"].smart is False
    smart = found["No Attachments"]
    assert smart.smart is True
    assert smart.query == "('File Attachments' IS [])"


def test_attributes_groups_to_their_set(rdb):
    found = {g.name: g for g in groups.read_groups(rdb)}
    assert found["Ni-Ti SMAs"].group_set == "My Groups"
    assert found["No Attachments"].group_set == ""  # not a member of the set


def test_rdb_dir_derives_from_the_enl_path():
    assert groups.rdb_dir("/lib/My Library.enl") == Path("/lib/My Library.Data/rdb")


def test_missing_rdb_folder_is_explained(tmp_path: Path):
    with pytest.raises(FileNotFoundError, match=r"misc\.MYD"):
        groups.read_groups(tmp_path)


# --------------------------------------------------------------------------
# indexing and querying
# --------------------------------------------------------------------------


@pytest.fixture
def grouped(conn, rdb, tmp_path: Path):
    indexer.index_groups(conn, tmp_path / "Test.enl")
    return conn


def test_index_groups_reports_what_it_read(conn, rdb, tmp_path: Path):
    stats = indexer.index_groups(conn, tmp_path / "Test.enl")
    assert stats == {"groups": 3, "smart_groups": 1, "memberships": 6, "empty_groups": 1}


def test_listed_sizes_ignore_members_that_are_not_indexed(grouped):
    listed = {g["name"]: g for g in db.list_groups(grouped)}
    # "No Attachments" lists records 2, 3 and 999; only 2 and 3 exist.
    assert listed["No Attachments"]["size"] == 2
    assert listed["No Attachments"]["stale_members"] == 1
    assert listed["Ni-Ti SMAs"]["size"] == 3


def test_empty_groups_can_be_hidden(grouped):
    assert len(db.list_groups(grouped)) == 3
    assert {g["name"] for g in db.list_groups(grouped, include_empty=False)} == {
        "Ni-Ti SMAs",
        "No Attachments",
    }


def test_search_can_be_confined_to_a_group(grouped):
    from endnotex9_toolkit import search

    unfiltered = {r["rec_number"] for r in search.search_references(grouped, ["martensitic"])}
    assert unfiltered == {1, 2}
    in_group = search.search_references(grouped, ["martensitic"], group="No Attachments")
    assert [r["rec_number"] for r in in_group] == [2]


def test_listing_can_be_confined_to_a_group(grouped):
    result = browse.list_references(grouped, group="Ni-Ti SMAs")
    assert {r["rec_number"] for r in result["references"]} == {1, 2, 4}


def test_group_filter_is_case_insensitive(grouped):
    assert browse.list_references(grouped, group="ni-ti smas")["total_matching"] == 3


def test_unknown_group_matches_nothing(grouped):
    assert browse.list_references(grouped, group="Nope")["total_matching"] == 0


def test_stats_report_the_group_count(grouped):
    stats = browse.library_stats(grouped)
    assert stats["groups"] == 3
    assert stats["last_groups_read_at"]


def test_reindexing_groups_replaces_rather_than_accumulates(conn, rdb, tmp_path: Path):
    indexer.index_groups(conn, tmp_path / "Test.enl")
    indexer.index_groups(conn, tmp_path / "Test.enl")
    assert db.count_groups(conn) == 3
    assert browse.list_references(conn, group="Ni-Ti SMAs")["total_matching"] == 3
