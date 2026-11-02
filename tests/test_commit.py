"""The policy-gated commit surface: bibliographic fields and marker tags.

Builds the same kind of synthetic MyISAM fixture as test_writeback.py, since
commit.py reads current field values straight out of refs.MYD before and
after writing.
"""

from __future__ import annotations

import struct
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from endnotex9_toolkit import commit, libraryio, myisam, writeback


def _row(rec_number: int, **fields) -> bytes:
    row = {"id": rec_number}
    row.update({k: v.encode("utf-8") if isinstance(v, str) else v for k, v in fields.items()})
    return writeback.encode_row(row)


def _whole_block(payload: bytes) -> bytes:
    pad = (-(len(payload) + 4)) % 4
    return b"\x03" + struct.pack(">H", len(payload)) + bytes([pad]) + payload + b"\x00" * pad


@pytest.fixture
def library(tmp_path: Path) -> tuple[Path, Path]:
    rdb = tmp_path / "Lib.Data" / "rdb"
    rdb.mkdir(parents=True)

    rows = [
        _row(1, title="Elastocaloric cooling", author="Li, D.", year="2022"),
        _row(2, title="Martensitic transformation", author="Smith, J.", year="2020", volume="14"),
        _row(
            3,
            title="Shape memory materials",
            author="Otsuka, K.",
            year="1998",
            custom_1="topic-a",
            custom_7="reviewer-note",
        ),
    ]
    myd = b"".join(_whole_block(r) for r in rows)
    (rdb / "refs.MYD").write_bytes(myd)

    myi = bytearray(256)
    myi[0:4] = myisam.MYI_MAGIC
    struct.pack_into(">H", myi, 4, myisam.HA_OPTION_PACK_RECORD)
    struct.pack_into(">Q", myi, writeback._MYI_SPLIT, len(rows))
    struct.pack_into(">Q", myi, writeback._MYI_DATA_FILE_LENGTH, len(myd))
    # No deleted blocks yet, so the free chain is empty — which MyISAM
    # spells with the end-of-chain sentinel, never zero (offset 0 is the
    # file's first block, a live record).
    struct.pack_into(">Q", myi, writeback._MYI_DELLINK, writeback._FREE_LIST_END)
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
# update_reference_fields: scalar TEXT_FIELDS
# --------------------------------------------------------------------------


def test_dry_run_reports_the_diff_and_writes_nothing(library):
    rdb, enl = library
    before = (rdb / "refs.MYD").read_bytes()

    result = commit.update_reference_fields(
        rdb, enl, {1: {"volume": "9", "pages": "1-10"}}, dry_run=True
    )

    assert result["dry_run"] is True
    assert result["would_write"] == 2
    assert {d["field"]: d["new_value"] for d in result["diffs"]} == {"volume": "9", "pages": "1-10"}
    assert all(d["old_value"] == "" for d in result["diffs"])
    assert (rdb / "refs.MYD").read_bytes() == before


def test_committing_writes_and_backs_up(library):
    rdb, enl = library

    result = commit.update_reference_fields(
        rdb, enl, {1: {"volume": "9", "issue": "2", "pages": "1-10"}}, dry_run=False
    )

    assert result["dry_run"] is False
    assert result["records_written"] == 1
    assert result["backup_path"]
    backup = Path(result["backup_path"])
    assert (backup / "rdb" / "refs.MYD").exists()
    assert (backup / enl.name).exists()

    row = _read(rdb)[1]
    assert row["volume"] == b"9"
    assert row["number"] == b"2"
    assert row["pages"] == b"1-10"


def test_a_field_already_matching_is_reported_but_not_written(library):
    rdb, enl = library
    # rec 2 already has volume=14
    result = commit.update_reference_fields(rdb, enl, {2: {"volume": "14"}}, dry_run=False)
    assert result["records_written"] == 0
    assert result["backup_path"] is None
    assert result["diffs"] == [
        {"rec_number": 2, "field": "volume", "old_value": "14", "new_value": "14"}
    ]


def test_refuses_non_writable_fields(library):
    rdb, enl = library
    with pytest.raises(commit.CommitError, match="not fields this tool may write"):
        commit.update_reference_fields(rdb, enl, {1: {"year": "2030"}}, dry_run=True)


def test_refuses_unknown_record_numbers(library):
    rdb, enl = library
    with pytest.raises(commit.CommitError, match="no such records"):
        commit.update_reference_fields(rdb, enl, {999: {"volume": "1"}}, dry_run=True)


def test_only_changed_fields_are_backed_up_and_written(library):
    rdb, enl = library
    # rec 2 already has volume=14; only pages actually changes.
    result = commit.update_reference_fields(
        rdb, enl, {2: {"volume": "14", "pages": "5-9"}}, dry_run=False
    )
    assert result["records_written"] == 1
    row = _read(rdb)[2]
    assert row["volume"] == b"14"
    assert row["pages"] == b"5-9"


# --------------------------------------------------------------------------
# update_reference_fields: journal/notes (scalar) and author/keywords (list)
# --------------------------------------------------------------------------


def test_journal_and_notes_are_plain_text_fields(library):
    rdb, enl = library
    result = commit.update_reference_fields(
        rdb,
        enl,
        {1: {"journal": "Journal of Physical Chemistry Letters", "notes": ""}},
        dry_run=False,
    )
    assert result["records_written"] == 1
    row = _read(rdb)[1]
    assert row["secondary_title"] == b"Journal of Physical Chemistry Letters"
    assert "notes" not in row


def test_title_and_abstract_are_plain_text_fields(library):
    rdb, enl = library
    result = commit.update_reference_fields(
        rdb,
        enl,
        {1: {"title": "A corrected title", "abstract": "A corrected abstract."}},
        dry_run=False,
    )
    assert result["records_written"] == 1
    row = _read(rdb)[1]
    assert row["title"] == b"A corrected title"
    assert row["abstract"] == b"A corrected abstract."


def test_doi_is_a_plain_text_field(library):
    rdb, enl = library
    result = commit.update_reference_fields(
        rdb, enl, {1: {"doi": "10.1016/j.actamat.2020.106545"}}, dry_run=False
    )
    assert result["records_written"] == 1
    assert _read(rdb)[1]["electronic_resource_number"] == b"10.1016/j.actamat.2020.106545"


def test_author_and_keywords_are_list_fields(library):
    rdb, enl = library

    preview = commit.update_reference_fields(
        rdb,
        enl,
        {1: {"author": ["Zerovnik, Andrej", "Dall'Olio, Stefano"]}},
        dry_run=True,
    )
    assert preview["diffs"] == [
        {
            "rec_number": 1,
            "field": "author",
            "old_value": ["Li, D."],
            "new_value": ["Zerovnik, Andrej", "Dall'Olio, Stefano"],
        }
    ]

    result = commit.update_reference_fields(
        rdb,
        enl,
        {1: {"author": ["Zerovnik, Andrej", "Dall'Olio, Stefano"], "keywords": ["elastocaloric"]}},
        dry_run=False,
    )
    assert result["records_written"] == 1
    row = _read(rdb)[1]
    assert row["author"] == b"Zerovnik, Andrej\rDall'Olio, Stefano"
    assert row["keywords"] == b"elastocaloric"


def test_list_field_rejects_a_non_list_value(library):
    rdb, enl = library
    with pytest.raises(commit.CommitError, match="must be a list of strings"):
        commit.update_reference_fields(rdb, enl, {1: {"author": "Not, A. List"}}, dry_run=True)


def test_list_field_strips_blanks_and_a_no_op_write_is_not_backed_up(library):
    rdb, enl = library
    # rec 1's author is "Li, D."; asking for the same single value (plus a
    # blank that should be dropped) must not count as a change.
    result = commit.update_reference_fields(
        rdb, enl, {1: {"author": ["Li, D.", "  "]}}, dry_run=False
    )
    assert result == {
        "dry_run": False,
        "records_written": 0,
        "backup_path": None,
        "diffs": [
            {"rec_number": 1, "field": "author", "old_value": ["Li, D."], "new_value": ["Li, D."]}
        ],
    }


# --------------------------------------------------------------------------
# mark_records
# --------------------------------------------------------------------------


def test_mark_dry_run_reports_counts_and_writes_nothing(library):
    rdb, enl = library
    before = (rdb / "refs.MYD").read_bytes()

    result = commit.mark_records(rdb, enl, [1, 2], "DELETE_CANDIDATE", dry_run=True)

    assert len(result.pop("source_fingerprint")) == 64
    assert result == {"dry_run": True, "would_mark": 2, "already_marked": 0, "conflicts": {}}
    assert (rdb / "refs.MYD").read_bytes() == before


def test_mark_commits_and_backs_up(library):
    rdb, enl = library
    result = commit.mark_records(rdb, enl, [1, 2], "DELETE_CANDIDATE", dry_run=False)
    assert result["records_written"] == 2
    assert result["backup_path"]
    row = _read(rdb)[1]
    assert row["custom_7"] == b"DELETE_CANDIDATE"


def test_mark_refuses_custom_1(library):
    rdb, enl = library
    with pytest.raises(commit.CommitError, match="reserved"):
        commit.mark_records(rdb, enl, [1], "x", column="custom_1", dry_run=True)


def test_mark_refuses_to_clobber_existing_different_text(library):
    rdb, enl = library
    with pytest.raises(commit.CommitError, match="already hold different"):
        commit.mark_records(rdb, enl, [3], "DELETE_CANDIDATE", column="custom_7", dry_run=True)


def test_mark_force_overwrites_existing_text(library):
    rdb, enl = library
    commit.mark_records(rdb, enl, [1], "first", column="custom_7", dry_run=False)
    result = commit.mark_records(
        rdb, enl, [1], "second", column="custom_7", force=True, dry_run=False
    )
    assert result["records_written"] == 1
    assert _read(rdb)[1]["custom_7"] == b"second"


def test_mark_is_idempotent_for_already_marked_records(library):
    rdb, enl = library
    commit.mark_records(rdb, enl, [1], "DELETE_CANDIDATE", dry_run=False)
    result = commit.mark_records(rdb, enl, [1], "DELETE_CANDIDATE", dry_run=False)
    assert result == {
        "dry_run": False,
        "records_written": 0,
        "backup_path": None,
        "column": "custom_7",
    }


def test_concurrent_commits_preserve_both_changes(library):
    rdb, enl = library
    start = threading.Barrier(2)

    def update(rec):
        start.wait(timeout=5)
        return commit.update_reference_fields(
            rdb, enl, {rec: {"notes": f"note-{rec}"}}, dry_run=False
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(update, [1, 2]))
    assert all(result["records_written"] == 1 for result in results)
    rows = _read(rdb)
    assert rows[1]["notes"] == b"note-1"
    assert rows[2]["notes"] == b"note-2"


def test_failed_publication_rolls_back_all_three_files(library, monkeypatch):
    rdb, enl = library
    paths = [rdb / "refs.MYD", rdb / "refs.MYI", enl]
    originals = [p.read_bytes() for p in paths]
    replace = libraryio.os.replace
    failed = False

    def fail_once(source, target):
        nonlocal failed
        if Path(target) == enl and not failed:
            failed = True
            raise OSError("synthetic publication failure")
        return replace(source, target)

    monkeypatch.setattr(libraryio.os, "replace", fail_once)
    with pytest.raises(OSError, match="synthetic"):
        commit.update_reference_fields(rdb, enl, {1: {"notes": "changed"}}, dry_run=False)
    assert [p.read_bytes() for p in paths] == originals
    assert not libraryio.recovery_dir(enl).exists()


def test_interruption_is_recoverable_and_blocks_further_writes(library, monkeypatch, capsys):
    rdb, enl = library
    paths = [rdb / "refs.MYD", rdb / "refs.MYI", enl]
    originals = [p.read_bytes() for p in paths]
    replace = libraryio.os.replace

    def interrupt(source, target):
        if Path(target) == rdb / "refs.MYI":
            raise KeyboardInterrupt("synthetic process interruption")
        return replace(source, target)

    with monkeypatch.context() as m:
        m.setattr(libraryio.os, "replace", interrupt)
        with pytest.raises(KeyboardInterrupt):
            commit.update_reference_fields(rdb, enl, {1: {"notes": "changed"}}, dry_run=False)
    assert libraryio.recovery_dir(enl).is_dir()
    with pytest.raises(libraryio.LibraryIOError, match="recovery"):
        commit.mark_records(rdb, enl, [1], "marker", dry_run=False)
    import argparse
    import json

    from endnotex9_toolkit import cli

    cli.cmd_recover_writeback(argparse.Namespace(library=str(enl)))
    assert json.loads(capsys.readouterr().out)["recovered"] is True
    assert [p.read_bytes() for p in paths] == originals


def test_commit_rejects_a_stale_preview(library):
    rdb, enl = library
    preview = commit.update_reference_fields(rdb, enl, {1: {"notes": "reviewed"}})
    commit.update_reference_fields(rdb, enl, {2: {"notes": "another change"}}, dry_run=False)
    with pytest.raises(commit.CommitError, match="changed since the preview"):
        commit.update_reference_fields(
            rdb,
            enl,
            {1: {"notes": "reviewed"}},
            dry_run=False,
            expected_fingerprint=preview["source_fingerprint"],
        )
    assert _read(rdb)[1].get("notes", b"") == b""


def test_recovery_preserves_edits_outside_the_interrupted_transaction(library, monkeypatch):
    rdb, enl = library
    replace = libraryio.os.replace

    def interrupt(source, target):
        if Path(target) == rdb / "refs.MYI":
            raise KeyboardInterrupt("synthetic interruption")
        return replace(source, target)

    with monkeypatch.context() as m:
        m.setattr(libraryio.os, "replace", interrupt)
        with pytest.raises(KeyboardInterrupt):
            commit.update_reference_fields(rdb, enl, {1: {"notes": "changed"}}, dry_run=False)
    external_edit = b"synthetic external edit"
    enl.write_bytes(external_edit)
    with libraryio.library_lock(enl), pytest.raises(libraryio.LibraryIOError, match="edited after"):
        libraryio.recover_publication(rdb, enl)
    assert enl.read_bytes() == external_edit
    assert libraryio.recovery_dir(enl).is_dir()
