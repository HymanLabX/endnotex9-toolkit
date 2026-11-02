"""Saved cleaner previews are applied without replanning or expanding their scope."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from endnotex9_toolkit import commit, libraryio

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/lit-cleaner/scripts"
sys.path.insert(0, str(SCRIPTS))
import common  # noqa: E402
import dedup  # noqa: E402
import files as files_script  # noqa: E402
import metadata  # noqa: E402


@pytest.fixture
def library(tmp_path, monkeypatch):
    from test_commit import library as make_library

    rdb, enl = make_library.__wrapped__(tmp_path)
    pdf_dir = enl.with_suffix(".Data") / "PDF"
    pdf_dir.mkdir()
    monkeypatch.setattr(common, "load_library", lambda: (rdb, str(enl), str(pdf_dir)))
    return rdb, enl, pdf_dir


def _run(module, directory, monkeypatch, *args):
    monkeypatch.setattr(sys, "argv", [str(module.__file__), "--output-dir", str(directory), *args])
    module.main()


def test_metadata_commit_applies_saved_diff_without_requerying(library, tmp_path, monkeypatch):
    rdb, _enl, _pdf = library
    out = tmp_path / "reports"
    monkeypatch.setattr(
        metadata,
        "build_updates",
        lambda *args: (
            {1: {"volume": "9"}},
            {"volume": 1},
            {"author_count_mismatch": [], "author_surname_mismatch": []},
        ),
    )
    _run(metadata, out, monkeypatch)

    def must_not_replan(*args, **kwargs):
        raise AssertionError("Commit must use the saved preview")

    monkeypatch.setattr(metadata, "build_updates", must_not_replan)
    monkeypatch.setattr(metadata.crossref, "fetch_many", must_not_replan)
    _run(metadata, out, monkeypatch, "--commit")
    assert common.text(common.read_all_rows(rdb)[1], "volume") == "9"


def test_metadata_saved_preview_rejects_changed_library(library, tmp_path, monkeypatch):
    rdb, enl, _pdf = library
    out = tmp_path / "reports"
    monkeypatch.setattr(
        metadata,
        "build_updates",
        lambda *args: (
            {1: {"volume": "9"}},
            {},
            {"author_count_mismatch": [], "author_surname_mismatch": []},
        ),
    )
    _run(metadata, out, monkeypatch)
    commit.update_reference_fields(rdb, enl, {2: {"notes": "later"}}, dry_run=False)
    with pytest.raises(commit.CommitError, match="changed since the preview"):
        _run(metadata, out, monkeypatch, "--commit")
    assert common.text(common.read_all_rows(rdb)[1], "volume") == ""


def test_dedup_saved_plan_merges_and_marks_under_one_library_lock(library, tmp_path, monkeypatch):
    rdb, enl, _pdf = library
    out = tmp_path / "reports"
    out.mkdir()
    (out / "dedup_plan.json").write_text(
        json.dumps(
            {
                "merge_updates": {"1": {"volume": "14"}},
                "delete_recs": [2],
                "marker": "LIT_CLEANER_DUPLICATE",
                "column": "custom_7",
                "source_fingerprint": libraryio.source_fingerprint(rdb, enl),
            }
        ),
        encoding="utf-8",
    )
    _run(dedup, out, monkeypatch, "--commit")
    rows = common.read_all_rows(rdb)
    assert common.text(rows[1], "volume") == "14"
    assert common.text(rows[2], "custom_7") == "LIT_CLEANER_DUPLICATE"


def test_file_commit_never_adds_unreviewed_candidates(library, tmp_path, monkeypatch):
    _rdb, _enl, pdf_dir = library
    reviewed = pdf_dir / "reviewed.pdf"
    reviewed.write_bytes(b"synthetic")
    out = tmp_path / "reports"
    _run(files_script, out, monkeypatch)
    later = pdf_dir / "later.pdf"
    later.write_bytes(b"synthetic")
    _run(files_script, out, monkeypatch, "--commit")
    assert (pdf_dir / files_script.QUARANTINE_DIRNAME / reviewed.name).is_file()
    assert later.is_file()


def test_file_commit_rejects_an_unrelated_backup_in_the_report(library, tmp_path):
    rdb, enl, pdf_dir = library
    unrelated = tmp_path / "backups/personal"
    unrelated.mkdir(parents=True)
    report = {
        "orphans": [],
        "prunable_backups": [str(unrelated)],
        "retention": {"keep_recent": 0, "age_days": 0},
        "source_fingerprint": libraryio.source_fingerprint(rdb, enl),
    }
    with pytest.raises(RuntimeError, match="candidates changed"):
        files_script.apply_report(report, rdb, str(enl), str(pdf_dir))
    assert unrelated.is_dir()
