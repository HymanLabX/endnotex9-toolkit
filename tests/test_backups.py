from __future__ import annotations

import json
from pathlib import Path

import pytest

from endnotex9_toolkit import backups


def _backup(root: Path, name: str, library: str = "Lib.enl") -> Path:
    folder = root / "backups" / name
    (folder / "rdb").mkdir(parents=True)
    for member in (library, "rdb/refs.MYD", "rdb/refs.MYI"):
        (folder / member).write_bytes(b"synthetic backup")
    return folder


def test_pruning_ignores_other_libraries_unrelated_and_incomplete_directories(tmp_path):
    ours = _backup(tmp_path, "Lib-20260101T000000Z")
    _backup(tmp_path, "Other-20260101T000000Z", "Other.enl")
    _backup(tmp_path, "personal-backup")
    (tmp_path / "backups/Lib-20260102T000000Z").mkdir()
    assert backups.excess_backups(tmp_path / "Lib.enl", 0, 0) == ([ours], [])


def test_manifest_identity_and_timestamp_control_retention(tmp_path):
    library = tmp_path / "Lib.enl"
    older = _backup(tmp_path, "z-old")
    newer = _backup(tmp_path, "a-new")
    for folder, year in ((older, 2020), (newer, 2021)):
        backups.record_backup(folder, library)
        manifest = json.loads((folder / backups.MANIFEST).read_text())
        manifest["created_at"] = f"{year}-01-01T00:00:00+00:00"
        (folder / backups.MANIFEST).write_text(json.dumps(manifest))
    assert backups.excess_backups(library, 1, 0) == ([older], [newer])
    assert backups.excess_backups(tmp_path / "Other.enl", 0, 0) == ([], [])


@pytest.mark.parametrize("keep,age", [(-1, 14), (5, -1)])
def test_negative_retention_is_rejected(tmp_path, keep, age):
    with pytest.raises(ValueError, match="non-negative"):
        backups.excess_backups(tmp_path / "Lib.enl", keep, age)


def test_incomplete_and_damaged_manifests_are_never_pruned(tmp_path):
    pending = _backup(tmp_path, "Lib-20200101T000000Z")
    (pending / ".backup-pending").write_text("copy interrupted")
    damaged = _backup(tmp_path, "Lib-20200102T000000Z")
    (damaged / backups.MANIFEST).write_text("{invalid")
    assert backups.excess_backups(tmp_path / "Lib.enl", 0, 0) == ([], [])
