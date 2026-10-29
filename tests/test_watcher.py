"""Watcher behavior around EndNote's non-atomic save sequence."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from endnotex9_toolkit import watcher


def test_xml_only_handler_keeps_the_legacy_contract(tmp_path: Path):
    xml = tmp_path / "library.xml"
    xml.write_text("<xml/>", encoding="utf-8")
    handler = watcher._XmlChangeHandler(xml, lambda: None, stability_seconds=0)
    assert handler.watched_paths == (xml.resolve(),)


def test_library_handler_tracks_live_reference_and_group_tables(tmp_path: Path):
    library = tmp_path / "Research.enl"
    handler = watcher._XmlChangeHandler("", lambda: None, library_path=library, stability_seconds=0)
    assert handler.watched_paths == (
        library.resolve(),
        (tmp_path / "Research.Data" / "rdb" / "refs.MYD").resolve(),
        (tmp_path / "Research.Data" / "rdb" / "misc.MYD").resolve(),
    )


def test_atomic_rename_to_a_watched_path_is_debounced(tmp_path: Path, monkeypatch):
    xml = tmp_path / "library.xml"
    scheduled: list[float] = []
    handler = watcher._XmlChangeHandler(xml, lambda: None, stability_seconds=0)
    monkeypatch.setattr(
        handler, "_schedule", lambda delay=watcher._DEBOUNCE_SECONDS: scheduled.append(delay)
    )
    handler.on_moved(
        SimpleNamespace(
            is_directory=False,
            src_path=str(tmp_path / ".library.xml.tmp"),
            dest_path=str(xml),
        )
    )
    assert scheduled == [watcher._DEBOUNCE_SECONDS]


def test_stability_check_rejects_a_fingerprint_that_changes(tmp_path: Path, monkeypatch):
    source = tmp_path / "refs.MYD"
    source.write_bytes(b"first")

    def mutate(_delay: float) -> None:
        source.write_bytes(b"second-and-longer")

    monkeypatch.setattr(watcher.time, "sleep", mutate)
    assert watcher._stable((source,), 0.1) is False


def test_stability_check_accepts_existing_unchanged_input(tmp_path: Path):
    source = tmp_path / "refs.MYD"
    source.write_bytes(b"complete")
    assert watcher._stable((source,), 0) is True


# --------------------------------------------------------------------------
# what a wake-up actually refreshes
# --------------------------------------------------------------------------


def test_auto_reindex_also_refreshes_groups(tmp_path: Path, monkeypatch):
    """`misc.MYD` is one of the watched files precisely because it holds the
    groups, so a run that only reindexed references would be deaf to half of
    what woke it up."""
    read_groups_for: list = []
    monkeypatch.setattr(watcher.indexer, "build_index", lambda *a, **k: {"total": 0})
    monkeypatch.setattr(
        watcher.indexer,
        "index_groups",
        lambda conn, library: read_groups_for.append(library) or {"groups": 2},
    )

    watcher._reindex(None, tmp_path / "index.db", tmp_path / "Research.enl")

    assert read_groups_for == [tmp_path / "Research.enl"]


def test_a_group_read_failure_still_keeps_the_reference_update(tmp_path: Path, monkeypatch):
    """Same policy as `rebuild_index`: a locked library must not cost us the
    references as well."""
    indexed: list = []

    def explode(_conn, _library):
        raise OSError("library is locked")

    monkeypatch.setattr(
        watcher.indexer, "build_index", lambda *a, **k: indexed.append("refs") or {}
    )
    monkeypatch.setattr(watcher.indexer, "index_groups", explode)

    watcher._reindex(None, tmp_path / "index.db", tmp_path / "Research.enl")

    assert indexed == ["refs"]


def test_an_xml_only_install_never_looks_for_groups(tmp_path: Path, monkeypatch):
    xml = tmp_path / "library.xml"
    xml.write_text("<xml/>", encoding="utf-8")
    monkeypatch.setattr(watcher.indexer, "build_index", lambda *a, **k: {})
    monkeypatch.setattr(
        watcher.indexer,
        "index_groups",
        lambda *a: pytest.fail("groups live in the library, which is not configured here"),
    )

    watcher._reindex(xml, tmp_path / "index.db", None)
