"""Writing a column back into a MyISAM `refs` table.

These build real MyISAM bytes and read them back with the same reader the
package uses at runtime. The invariant every test is really checking is the
one the whole approach rests on: **a record's head block never moves**, so
the indexes in the `.MYI` stay valid without being rewritten.
"""

from __future__ import annotations

import struct
import zipfile
from pathlib import Path

import pytest

from endnotex9_mcp import myisam, writeback


def _row(rec_number: int, **fields) -> bytes:
    row = {"id": rec_number}
    row.update({k: v.encode("utf-8") if isinstance(v, str) else v for k, v in fields.items()})
    return writeback.encode_row(row)


def _whole_block(payload: bytes, extra_pad: int = 0) -> bytes:
    pad = ((-(len(payload) + 4)) % 4) + extra_pad
    return b"\x03" + struct.pack(">H", len(payload)) + bytes([pad]) + payload + b"\x00" * pad


@pytest.fixture
def library(tmp_path: Path) -> tuple[Path, Path]:
    """An rdb folder plus the `.enl` mirror, holding three records."""
    rdb = tmp_path / "Lib.Data" / "rdb"
    rdb.mkdir(parents=True)

    rows = [
        _row(1, title="Elastocaloric cooling", author="Li, D.", year="2022"),
        _row(
            2,
            title="Martensitic transformation",
            author="Smith, J.",
            year="2020",
            abstract="A" * 400,
        ),
        _row(3, title="Shape memory materials", author="Otsuka, K.", year="1998", reference_type=7),
    ]
    myd = b"".join(_whole_block(r) for r in rows)
    (rdb / "refs.MYD").write_bytes(myd)

    # A .MYI just large enough to carry the state header fields we update.
    myi = bytearray(256)
    myi[0:4] = myisam.MYI_MAGIC
    struct.pack_into(">H", myi, 4, myisam.HA_OPTION_PACK_RECORD)
    struct.pack_into(">Q", myi, writeback._MYI_SPLIT, len(rows))
    struct.pack_into(">Q", myi, writeback._MYI_DATA_FILE_LENGTH, len(myd))
    (rdb / "refs.MYI").write_bytes(bytes(myi))
    (rdb / "refs.frm").write_bytes(b"fake frm")

    enl = tmp_path / "Lib.enl"
    with zipfile.ZipFile(enl, "w", zipfile.ZIP_STORED) as z:
        z.writestr("refs.MYD", myd)
        z.writestr("refs.frm", b"fake frm")
    return rdb, enl


def _read(rdb: Path) -> dict:
    return {
        writeback.decode_row(r)["id"]: writeback.decode_row(r)
        for r in myisam.read_records(rdb / "refs.MYD")
    }


# --------------------------------------------------------------------------
# row codec
# --------------------------------------------------------------------------


def test_row_round_trips():
    original = _row(42, title="A title", year="2024", custom_1="tag")
    assert writeback.encode_row(writeback.decode_row(original)) == original


def test_decode_rejects_a_row_it_cannot_consume_exactly():
    from endnotex9_mcp import refstable

    with pytest.raises(refstable.RowError, match="consumed"):
        writeback.decode_row(_row(1, title="x") + b"\x00\x00")


def test_reference_type_is_two_bytes_not_four():
    # A Journal Article is internal type 0 and gets packed away entirely, so
    # this width is only exercised by the handful of non-article records —
    # which is exactly how a wrong width stays hidden.
    row = writeback.decode_row(_row(3, reference_type=7, title="T"))
    assert row["reference_type"] == 7
    assert row["title"] == b"T"


def test_empty_values_are_omitted_from_the_row():
    assert "custom_1" not in writeback.decode_row(_row(1, title="T", custom_1=""))


# --------------------------------------------------------------------------
# writing
# --------------------------------------------------------------------------


def test_sets_a_column_without_disturbing_other_records(library):
    rdb, enl = library
    before = _read(rdb)

    writeback.set_column(rdb, enl, {2: "分类A"}, column="custom_1")

    after = _read(rdb)
    assert after[2]["custom_1"] == "分类A".encode()
    assert after[2]["title"] == before[2]["title"]
    for rec in (1, 3):
        assert after[rec] == before[rec]


def test_head_block_offsets_are_preserved(library):
    """The reason no index rebuild is needed."""
    rdb, enl = library

    def head_positions(path: Path) -> dict:
        data = path.read_bytes()
        out, pos = {}, 0
        while pos < len(data):
            block = myisam._read_block(data, pos)
            if block.first and not block.deleted:
                record = data[block.pos + block.header_len :][: block.data_len]
                out[int.from_bytes(record[writeback.PACK_BITS :][:4], "little")] = block.pos
            pos += max(block.span, 1)
        return out

    before = head_positions(rdb / "refs.MYD")
    writeback.set_column(rdb, enl, {1: "x", 2: "y", 3: "z"}, column="custom_1")
    after = head_positions(rdb / "refs.MYD")
    assert after == before


def test_record_count_is_unchanged(library):
    rdb, enl = library
    writeback.set_column(rdb, enl, {1: "a", 2: "b", 3: "c"}, column="custom_1")
    assert len(myisam.read_records(rdb / "refs.MYD")) == 3


def test_writes_survive_a_second_pass(library):
    rdb, enl = library
    writeback.set_column(rdb, enl, {1: "first"}, column="custom_1")
    writeback.set_column(rdb, enl, {1: "second", 3: "other"}, column="custom_1")
    rows = _read(rdb)
    assert rows[1]["custom_1"] == b"second"
    assert rows[3]["custom_1"] == b"other"


def test_clearing_a_column(library):
    rdb, enl = library
    writeback.set_column(rdb, enl, {1: "tag"}, column="custom_1")
    writeback.set_column(rdb, enl, {1: ""}, column="custom_1")
    assert "custom_1" not in _read(rdb)[1]


def test_state_counters_are_updated(library):
    rdb, enl = library
    writeback.set_column(rdb, enl, {1: "a" * 200}, column="custom_1")
    myi = (rdb / "refs.MYI").read_bytes()
    assert (
        struct.unpack_from(">Q", myi, writeback._MYI_DATA_FILE_LENGTH)[0]
        == (rdb / "refs.MYD").stat().st_size
    )


def test_enl_mirror_is_repacked_and_stays_stored(library):
    rdb, enl = library
    writeback.set_column(rdb, enl, {2: "tag"}, column="custom_1")
    with zipfile.ZipFile(enl) as z:
        assert z.read("refs.MYD") == (rdb / "refs.MYD").read_bytes()
        assert all(info.compress_type == zipfile.ZIP_STORED for info in z.infolist())


# --------------------------------------------------------------------------
# shrinking a record by more than one pad byte can record (_MAX_PAD)
# --------------------------------------------------------------------------


@pytest.fixture
def library_with_a_bloated_record(tmp_path: Path) -> tuple[Path, Path]:
    """Record 1 starts with ~1000 bytes of notes; record 2 sits right after it.

    Clearing record 1's notes leaves far more slack than one pad byte can
    hold (`_MAX_PAD` is 255) — the real-world shape of clearing a PubMed
    citation dump out of a bloated `notes` field.
    """
    rdb = tmp_path / "Lib.Data" / "rdb"
    rdb.mkdir(parents=True)

    rows = [
        _row(1, title="Elastocaloric cooling", author="Li, D.", year="2022", notes="N" * 1000),
        _row(2, title="Martensitic transformation", author="Smith, J.", year="2020"),
    ]
    myd = b"".join(_whole_block(r) for r in rows)
    (rdb / "refs.MYD").write_bytes(myd)

    myi = bytearray(256)
    myi[0:4] = myisam.MYI_MAGIC
    struct.pack_into(">H", myi, 4, myisam.HA_OPTION_PACK_RECORD)
    struct.pack_into(">Q", myi, writeback._MYI_SPLIT, len(rows))
    struct.pack_into(">Q", myi, writeback._MYI_DATA_FILE_LENGTH, len(myd))
    (rdb / "refs.MYI").write_bytes(bytes(myi))
    (rdb / "refs.frm").write_bytes(b"fake frm")

    enl = tmp_path / "Lib.enl"
    with zipfile.ZipFile(enl, "w", zipfile.ZIP_STORED) as z:
        z.writestr("refs.MYD", myd)
        z.writestr("refs.frm", b"fake frm")
    return rdb, enl


def test_clearing_a_field_that_leaves_more_than_max_pad_slack(library_with_a_bloated_record):
    rdb, enl = library_with_a_bloated_record

    writeback.set_column(rdb, enl, {1: ""}, column="notes")

    rows = _read(rdb)
    assert "notes" not in rows[1]
    assert rows[1]["title"] == b"Elastocaloric cooling"
    # record 2's own data is untouched, meaning record 1's filler landed
    # exactly where the old block ended without disturbing anything after it
    assert rows[2]["title"] == b"Martensitic transformation"
    assert rows[2]["author"] == b"Smith, J."


def test_block_walk_after_shrinking_lands_on_a_deleted_filler_block(library_with_a_bloated_record):
    rdb, enl = library_with_a_bloated_record
    writeback.set_column(rdb, enl, {1: ""}, column="notes")

    data = (rdb / "refs.MYD").read_bytes()
    blocks, pos = [], 0
    while pos < len(data):
        block = myisam._read_block(data, pos)
        blocks.append(block)
        pos += max(block.span, 1)
    assert pos == len(data)  # the whole file still walks cleanly to EOF
    assert any(b.deleted for b in blocks)  # the freed slack became a real deleted block


def test_shrinking_survives_the_files_self_verification(library_with_a_bloated_record):
    rdb, enl = library_with_a_bloated_record
    # set_column raises before writing anything if _verify disagrees, so a
    # clean return here already proves the round trip is exact.
    writeback.set_column(rdb, enl, {1: ""}, column="notes")
    writeback.set_column(rdb, enl, {1: "back again"}, column="notes")
    assert _read(rdb)[1]["notes"] == b"back again"


# --------------------------------------------------------------------------
# shrinking a record that was *already* chained across multiple blocks (not
# just a single whole-record block with slack) enough that the new row fits
# back inside the head block alone. The head block's own `_terminating` call
# succeeds and returns early — the bug this guards against is that the
# original middle/last continuation blocks, which live at their own file
# offsets after the head, were left completely untouched: not overwritten,
# not converted to deleted blocks. See `_pack_chain`.
# --------------------------------------------------------------------------


def _first_block(rec_len: int, chunk: bytes, next_pos: int) -> bytes:
    return b"\x05" + struct.pack(">H", rec_len) + struct.pack(">H", len(chunk)) + struct.pack(">Q", next_pos) + chunk


def _middle_block(chunk: bytes, next_pos: int) -> bytes:
    return b"\x0b" + struct.pack(">H", len(chunk)) + struct.pack(">Q", next_pos) + chunk


def _last_block(chunk: bytes) -> bytes:
    pad = (-(len(chunk) + 4)) % 4
    return b"\x09" + struct.pack(">H", len(chunk)) + bytes([pad]) + chunk + b"\x00" * pad


@pytest.fixture
def library_with_a_chained_record(tmp_path: Path) -> tuple[Path, Path]:
    """Record 1 is stored as a real first/middle/last chain, not one block.

    This is the shape 107 of the reference library's own 1561 records were
    already in before this package ever wrote anything (see writeback.py's
    module docstring) — a record big enough that MyISAM itself split it
    across a head block plus continuation blocks appended after it. Record 2
    sits immediately after the chain.
    """
    rdb = tmp_path / "Lib.Data" / "rdb"
    rdb.mkdir(parents=True)

    row1 = _row(1, title="Elastocaloric cooling", author="Li, D.", year="2022", notes="N" * 700)
    row2 = _row(2, title="Martensitic transformation", author="Smith, J.", year="2020")

    chunk0, chunk1, chunk2 = row1[:300], row1[300:600], row1[600:]
    pos_head = 0
    pos_mid = pos_head + 13 + len(chunk0)
    pos_last = pos_mid + 11 + len(chunk1)

    head = _first_block(len(row1), chunk0, pos_mid)
    mid = _middle_block(chunk1, pos_last)
    last = _last_block(chunk2)
    myd = head + mid + last + _whole_block(row2)
    (rdb / "refs.MYD").write_bytes(myd)

    myi = bytearray(256)
    myi[0:4] = myisam.MYI_MAGIC
    struct.pack_into(">H", myi, 4, myisam.HA_OPTION_PACK_RECORD)
    struct.pack_into(">Q", myi, writeback._MYI_SPLIT, 2)
    struct.pack_into(">Q", myi, writeback._MYI_DATA_FILE_LENGTH, len(myd))
    (rdb / "refs.MYI").write_bytes(bytes(myi))
    (rdb / "refs.frm").write_bytes(b"fake frm")

    enl = tmp_path / "Lib.enl"
    with zipfile.ZipFile(enl, "w", zipfile.ZIP_STORED) as z:
        z.writestr("refs.MYD", myd)
        z.writestr("refs.frm", b"fake frm")
    return rdb, enl, pos_mid, pos_last


def test_shrinking_a_chained_record_back_into_its_head_block(library_with_a_chained_record):
    rdb, enl, _pos_mid, _pos_last = library_with_a_chained_record

    writeback.set_column(rdb, enl, {1: ""}, column="notes")

    rows = _read(rdb)
    assert "notes" not in rows[1]
    assert rows[1]["title"] == b"Elastocaloric cooling"
    assert rows[2]["title"] == b"Martensitic transformation"
    assert rows[2]["author"] == b"Smith, J."
    assert len(myisam.read_records(rdb / "refs.MYD")) == 2


def test_orphaned_continuation_blocks_are_reclaimed_as_deleted(library_with_a_chained_record):
    """The bug this guards: leftover mid/last blocks left as live, unlinked bytes."""
    rdb, enl, pos_mid, pos_last = library_with_a_chained_record

    writeback.set_column(rdb, enl, {1: ""}, column="notes")

    data = (rdb / "refs.MYD").read_bytes()
    blocks, pos = {}, 0
    while pos < len(data):
        block = myisam._read_block(data, pos)
        blocks[pos] = block
        pos += max(block.span, 1)
    assert pos == len(data)  # the whole file still walks cleanly to EOF

    # the old middle/last blocks' file offsets must now be deleted blocks,
    # not live, unreferenced type-11/type-9 blocks
    assert blocks[pos_mid].deleted
    assert blocks[pos_last].deleted


def test_chained_shrink_survives_the_files_self_verification(library_with_a_chained_record):
    rdb, enl, _pos_mid, _pos_last = library_with_a_chained_record
    writeback.set_column(rdb, enl, {1: ""}, column="notes")
    writeback.set_column(rdb, enl, {1: "back again"}, column="notes")
    assert _read(rdb)[1]["notes"] == b"back again"


# --------------------------------------------------------------------------
# refusals
# --------------------------------------------------------------------------


def test_refuses_columns_that_feed_endnote_indexes(library):
    rdb, enl = library
    for column in ("year", "label"):
        with pytest.raises(writeback.WritebackError, match="safe-to-write"):
            writeback.set_column(rdb, enl, {1: "x"}, column=column)


def test_writes_title_and_abstract(library):
    """Verified last, via a byte-level read of the real refs.MYI key roots."""
    rdb, enl = library
    writeback.set_fields(
        rdb,
        enl,
        {1: {"title": "A corrected title", "abstract": "A corrected abstract."}},
    )
    row = _read(rdb)[1]
    assert row["title"] == b"A corrected title"
    assert row["abstract"] == b"A corrected abstract."


def test_writes_author_journal_keywords_and_notes(library):
    """The term-list-backed columns, verified safe by reading terms.MYD directly."""
    rdb, enl = library
    writeback.set_fields(
        rdb,
        enl,
        {
            1: {
                "author": "Zerovnik, Andrej\rDall'Olio, Stefano",
                "secondary_title": "Journal of Physical Chemistry Letters",
                "keywords": "elastocaloric",
                "notes": "",
            }
        },
    )
    row = _read(rdb)[1]
    assert row["author"] == b"Zerovnik, Andrej\rDall'Olio, Stefano"
    assert row["secondary_title"] == b"Journal of Physical Chemistry Letters"
    assert row["keywords"] == b"elastocaloric"
    assert "notes" not in row


def test_refuses_unknown_record_numbers(library):
    rdb, enl = library
    with pytest.raises(writeback.WritebackError, match="no such records"):
        writeback.set_column(rdb, enl, {999: "x"}, column="custom_1")


def test_a_refused_write_leaves_the_files_untouched(library):
    rdb, enl = library
    before = (rdb / "refs.MYD").read_bytes()
    with pytest.raises(writeback.WritebackError):
        writeback.set_column(rdb, enl, {999: "x"}, column="custom_1")
    assert (rdb / "refs.MYD").read_bytes() == before


# --------------------------------------------------------------------------
# set_fields: multiple columns per record
# --------------------------------------------------------------------------


def test_set_fields_writes_several_columns_on_one_record(library):
    rdb, enl = library
    before = _read(rdb)

    writeback.set_fields(rdb, enl, {2: {"volume": "12", "number": "3", "pages": "45-67"}})

    after = _read(rdb)
    assert after[2]["volume"] == b"12"
    assert after[2]["number"] == b"3"
    assert after[2]["pages"] == b"45-67"
    assert after[2]["title"] == before[2]["title"]
    for rec in (1, 3):
        assert after[rec] == before[rec]


def test_set_fields_writes_different_columns_per_record(library):
    rdb, enl = library
    writeback.set_fields(
        rdb,
        enl,
        {1: {"volume": "9"}, 3: {"pages": "100-110", "section": "e12345"}},
    )
    after = _read(rdb)
    assert after[1]["volume"] == b"9"
    assert "pages" not in after[1]
    assert after[3]["pages"] == b"100-110"
    assert after[3]["section"] == b"e12345"


def test_set_fields_clearing_one_of_several_columns(library):
    rdb, enl = library
    writeback.set_fields(rdb, enl, {1: {"volume": "9", "pages": "1-2"}})
    writeback.set_fields(rdb, enl, {1: {"pages": ""}})
    row = _read(rdb)[1]
    assert row["volume"] == b"9"
    assert "pages" not in row


def test_set_fields_refuses_any_column_outside_the_safe_list(library):
    rdb, enl = library
    with pytest.raises(writeback.WritebackError, match="safe-to-write"):
        writeback.set_fields(rdb, enl, {1: {"volume": "9", "year": "2030"}})


def test_set_fields_refuses_unknown_record_numbers(library):
    rdb, enl = library
    with pytest.raises(writeback.WritebackError, match="no such records"):
        writeback.set_fields(rdb, enl, {999: {"volume": "9"}})


def test_set_fields_refusal_leaves_files_untouched(library):
    rdb, enl = library
    before = (rdb / "refs.MYD").read_bytes()
    with pytest.raises(writeback.WritebackError):
        writeback.set_fields(rdb, enl, {1: {"volume": "9", "year": "2030"}})
    assert (rdb / "refs.MYD").read_bytes() == before


def test_set_fields_head_block_offsets_are_preserved(library):
    rdb, enl = library

    def head_positions(path: Path) -> dict:
        data = path.read_bytes()
        out, pos = {}, 0
        while pos < len(data):
            block = myisam._read_block(data, pos)
            if block.first and not block.deleted:
                record = data[block.pos + block.header_len :][: block.data_len]
                out[int.from_bytes(record[writeback.PACK_BITS :][:4], "little")] = block.pos
            pos += max(block.span, 1)
        return out

    before = head_positions(rdb / "refs.MYD")
    writeback.set_fields(rdb, enl, {1: {"volume": "1", "pages": "1-2"}, 2: {"number": "4"}})
    after = head_positions(rdb / "refs.MYD")
    assert after == before


# --------------------------------------------------------------------------
# backup_library
# --------------------------------------------------------------------------


def test_backup_library_copies_enl_and_rdb_without_touching_the_live_files(library):
    rdb, enl = library
    live_myd_before = (rdb / "refs.MYD").read_bytes()
    live_enl_before = enl.read_bytes()

    dest = writeback.backup_library(rdb, enl)

    assert dest.is_dir()
    assert (dest / enl.name).read_bytes() == live_enl_before
    assert (dest / "rdb" / "refs.MYD").read_bytes() == live_myd_before
    assert (dest / "rdb" / "refs.MYI").exists()
    # the live files are untouched
    assert (rdb / "refs.MYD").read_bytes() == live_myd_before
    assert enl.read_bytes() == live_enl_before


def test_backup_library_never_reuses_a_destination(library):
    rdb, enl = library
    first = writeback.backup_library(rdb, enl)
    second = writeback.backup_library(rdb, enl)
    assert first != second
    assert first.is_dir() and second.is_dir()


# --------------------------------------------------------------------------
# _ensure_unlocked: refuse to write while EndNote (or anything else) has the
# library open. Real OS locking semantics vary by how the other process
# opened the file, so this is tested against the check's own logic — does it
# turn an OSError on open() into a refusal — via monkeypatching `open`,
# rather than against real file-locking behaviour.
# --------------------------------------------------------------------------


def _deny_write_to(path_to_deny):
    import builtins

    real_open = builtins.open
    denied = Path(path_to_deny)

    def fake_open(path, mode="r", *args, **kwargs):
        if Path(path) == denied:
            raise PermissionError(f"simulated lock on {path}")
        return real_open(path, mode, *args, **kwargs)

    return fake_open


def test_backup_library_refuses_when_refs_myd_looks_locked(library, monkeypatch):
    rdb, enl = library
    monkeypatch.setattr("builtins.open", _deny_write_to(rdb / "refs.MYD"))
    with pytest.raises(writeback.WritebackError, match="open by another process"):
        writeback.backup_library(rdb, enl)


def test_backup_library_refuses_when_enl_looks_locked(library, monkeypatch):
    rdb, enl = library
    monkeypatch.setattr("builtins.open", _deny_write_to(enl))
    with pytest.raises(writeback.WritebackError, match="open by another process"):
        writeback.backup_library(rdb, enl)


def test_set_column_refuses_when_library_looks_locked(library, monkeypatch):
    rdb, enl = library
    monkeypatch.setattr("builtins.open", _deny_write_to(rdb / "refs.MYI"))
    with pytest.raises(writeback.WritebackError, match="open by another process"):
        writeback.set_column(rdb, enl, {1: "x"}, column="custom_1")


def test_locked_refusal_leaves_files_untouched(library, monkeypatch):
    rdb, enl = library
    before = (rdb / "refs.MYD").read_bytes()
    monkeypatch.setattr("builtins.open", _deny_write_to(rdb / "refs.MYD"))
    with pytest.raises(writeback.WritebackError):
        writeback.set_column(rdb, enl, {1: "x"}, column="custom_1")
    monkeypatch.undo()
    assert (rdb / "refs.MYD").read_bytes() == before


# --------------------------------------------------------------------------
# the .enl mirror
# --------------------------------------------------------------------------


def test_enl_mirror_tracks_the_new_data_file(library):
    rdb, enl = library
    writeback.set_column(rdb, enl, {1: "topic-x"}, column="custom_1")
    with zipfile.ZipFile(enl) as z:
        assert z.read("refs.MYD") == (rdb / "refs.MYD").read_bytes()


def test_enl_repack_keeps_members_it_does_not_understand(library):
    """A member we failed to anticipate must survive the write.

    The `.enl` of the one library this was developed against holds exactly
    `refs.MYD` and `refs.frm`. Dropping anything else would be silent and
    unrecoverable, so the repack copies every member across.
    """
    rdb, enl = library
    myd = (rdb / "refs.MYD").read_bytes()
    with zipfile.ZipFile(enl, "w", zipfile.ZIP_STORED) as z:
        z.writestr("refs.MYD", myd)
        z.writestr("refs.frm", b"fake frm")
        z.writestr("refs_ext.MYD", b"unanticipated payload")
        z.writestr("nested/thing.dat", b"also unanticipated")

    writeback.set_column(rdb, enl, {1: "topic-x"}, column="custom_1")

    with zipfile.ZipFile(enl) as z:
        assert sorted(z.namelist()) == [
            "nested/thing.dat",
            "refs.MYD",
            "refs.frm",
            "refs_ext.MYD",
        ]
        assert z.read("refs_ext.MYD") == b"unanticipated payload"
        assert z.read("nested/thing.dat") == b"also unanticipated"
        assert z.read("refs.frm") == b"fake frm"
        assert z.read("refs.MYD") == (rdb / "refs.MYD").read_bytes()


def test_enl_members_stay_uncompressed(library):
    rdb, enl = library
    writeback.set_column(rdb, enl, {1: "topic-x"}, column="custom_1")
    with zipfile.ZipFile(enl) as z:
        assert {i.compress_type for i in z.infolist()} == {zipfile.ZIP_STORED}


def test_unreadable_enl_fails_before_the_rdb_tables_are_touched(library):
    """The mirror is staged and checked first, so a doomed repack costs nothing.

    Repacking after the write would leave `refs.MYD` updated and the `.enl`
    stale — a divergence only the backup can undo.
    """
    rdb, enl = library
    myd_before = (rdb / "refs.MYD").read_bytes()
    myi_before = (rdb / "refs.MYI").read_bytes()
    enl.write_bytes(b"not a zip archive at all")

    with pytest.raises(zipfile.BadZipFile):
        writeback.set_column(rdb, enl, {1: "topic-x"}, column="custom_1")

    assert (rdb / "refs.MYD").read_bytes() == myd_before
    assert (rdb / "refs.MYI").read_bytes() == myi_before
    assert not enl.with_suffix(".enl.writeback-tmp").exists()
