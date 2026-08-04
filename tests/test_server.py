"""The MCP layer: every tool is registered and reachable end to end.

These are thin, but they are the only tests that exercise the SDK binding —
the class that hosts the tools was renamed between MCP SDK 1.x (`FastMCP`)
and 2.x (`MCPServer`), and an import error there takes the whole server down
without any other test noticing.
"""

from __future__ import annotations

import asyncio
import json
import struct
import zipfile
from pathlib import Path

import pytest

from endnotex9_mcp import myisam, server, writeback
from endnotex9_mcp.config import Config

EXPECTED_TOOLS = {
    "search_references",
    "list_references",
    "find_related",
    "library_stats",
    "list_groups",
    "get_reference_details",
    "search_fulltext",
    "read_pdf_pages",
    "get_citation",
    "get_bibliography",
    "get_bibtex",
    "rebuild_index",
    "index_pdfs",
    "list_topics",
    "profile_library",
    "sample_references",
    "create_classification_session",
    "get_classification_session",
    "save_classification_taxonomy",
    "approve_classification_taxonomy",
    "save_classification_rules",
    "freeze_classification_rules",
    "evaluate_classification_rules",
    "list_unresolved_classifications",
    "create_classification_audit",
    "submit_classification_audit",
    "get_classification_report",
    "prepare_classification_writeback",
    "update_reference_fields",
    "mark_references",
}


@pytest.fixture
def wired(conn, xml_path, tmp_path, monkeypatch):
    """Point the module-level server state at the test fixtures."""
    monkeypatch.setattr(server, "_conn", conn)
    monkeypatch.setattr(
        server,
        "_config",
        Config(endnote_xml=str(xml_path), pdf_dir="", db_path=str(tmp_path / "library.db")),
    )
    return server


def _call(name: str, arguments: dict):
    return asyncio.run(server.mcp.call_tool(name, arguments))


def _row(rec_number: int, **fields) -> bytes:
    row = {"id": rec_number}
    row.update({k: v.encode("utf-8") if isinstance(v, str) else v for k, v in fields.items()})
    return writeback.encode_row(row)


def _whole_block(payload: bytes) -> bytes:
    pad = (-(len(payload) + 4)) % 4
    return b"\x03" + struct.pack(">H", len(payload)) + bytes([pad]) + payload + b"\x00" * pad


def _make_rdb_library(tmp_path: Path, name: str, rows: list[bytes]) -> tuple[Path, Path]:
    """A real MyISAM `refs.MYD`/`refs.MYI` plus its `.enl` mirror."""
    enl = tmp_path / f"{name}.enl"
    rdb = tmp_path / f"{name}.Data" / "rdb"
    rdb.mkdir(parents=True)

    myd = b"".join(_whole_block(r) for r in rows)
    (rdb / "refs.MYD").write_bytes(myd)

    myi = bytearray(256)
    myi[0:4] = myisam.MYI_MAGIC
    struct.pack_into(">H", myi, 4, myisam.HA_OPTION_PACK_RECORD)
    struct.pack_into(">Q", myi, writeback._MYI_SPLIT, len(rows))
    struct.pack_into(">Q", myi, writeback._MYI_DATA_FILE_LENGTH, len(myd))
    (rdb / "refs.MYI").write_bytes(bytes(myi))
    (rdb / "refs.frm").write_bytes(b"fake frm")

    with zipfile.ZipFile(enl, "w", zipfile.ZIP_STORED) as z:
        z.writestr("refs.MYD", myd)
        z.writestr("refs.frm", b"fake frm")
    return rdb, enl


def _payload(result):
    """Unwrap whatever shape the installed SDK returns.

    SDK 2.x hands back a CallToolResult whose structured_content wraps a
    non-object return value under "result"; 1.x returned a (content,
    structured) tuple. Both carry the same value underneath.
    """
    structured = getattr(result, "structured_content", None)
    if structured is None and isinstance(result, tuple):
        structured = result[1]
    if structured is None:
        # An untyped dict return has no output schema, so it arrives as JSON
        # text rather than structured content.
        content = getattr(result, "content", None) or []
        return json.loads(content[0].text) if content else None
    if isinstance(structured, dict) and set(structured) == {"result"}:
        return structured["result"]
    return structured


def test_every_tool_is_registered():
    tools = asyncio.run(server.mcp.list_tools())
    assert {t.name for t in tools} == EXPECTED_TOOLS


def test_tools_describe_themselves():
    tools = asyncio.run(server.mcp.list_tools())
    for tool in tools:
        assert tool.description, f"{tool.name} has no description for the model to read"


def test_search_tool_round_trip(wired):
    # Also the regression guard for the connection being opened on one SDK
    # worker thread and used from the next.
    results = _payload(_call("search_references", {"queries": ["elastocaloric"], "limit": 3}))
    assert results[0]["rec_number"] == 1


def test_two_calls_land_on_different_worker_threads(wired):
    for _ in range(4):
        assert _payload(_call("library_stats", {}))["total_references"] == 4


def test_citation_tool_round_trip(wired):
    assert "[J]" in _payload(_call("get_citation", {"rec_number": 1, "style": "gbt7714"}))


def test_single_reference_tools_accept_title_and_doi(wired):
    details = _payload(
        _call(
            "get_reference_details",
            {"title": "Giant Elastocaloric Effect in Ni-Mn-Ga-Based Alloys"},
        )
    )
    assert details["rec_number"] == 1
    citation = _payload(
        _call(
            "get_citation",
            {"doi": "https://doi.org/10.1021/acsami.1c21109", "style": "apa7"},
        )
    )
    assert "Giant Elastocaloric Effect" in citation


def test_batch_tools_accept_mixed_reference_selectors(wired):
    references = [
        {"doi": "10.1016/j.actamat.2020.106545"},
        {"title": "Introduction to shape memory materials"},
    ]
    bibliography = _payload(
        _call("get_bibliography", {"references": references, "style": "gbt7714"})
    )
    assert "Martensitic transformation" in bibliography
    assert "Introduction to shape memory materials" in bibliography
    bibtex = _payload(_call("get_bibtex", {"references": references}))
    assert bibtex.count("@") == 2


def test_unknown_record_is_an_error(wired):
    with pytest.raises(Exception, match="No reference with rec_number=999"):
        _call("get_reference_details", {"rec_number": 999})


def test_index_pdfs_explains_a_missing_pdf_dir(wired):
    with pytest.raises(Exception, match="No pdf_dir configured"):
        _call("index_pdfs", {})


def test_read_pdf_pages_accepts_title(wired):
    wired._conn.execute(
        "INSERT INTO pdf_pages(rec_number,rel_path,page,body) VALUES(?,?,?,?)",
        (1, "0000000001/paper.pdf", 1, "Text found by title."),
    )
    wired._conn.commit()
    result = _payload(
        _call(
            "read_pdf_pages",
            {
                "title": "Giant Elastocaloric Effect in Ni-Mn-Ga-Based Alloys",
            },
        )
    )
    assert result["rec_number"] == 1
    assert result["pages"][0]["text"] == "Text found by title."


def test_uncertain_sampling_requires_evaluated_session(wired):
    session = _payload(_call("create_classification_session", {}))
    with pytest.raises(Exception, match="requires an evaluated"):
        _call(
            "sample_references",
            {
                "session_id": session["session_id"],
                "strategy": "uncertain",
                "size": 1,
            },
        )


def test_server_watcher_receives_the_direct_library_path(tmp_path, monkeypatch):
    config = Config(
        endnote_xml="",
        library_path=str(tmp_path / "Research.enl"),
        db_path=str(tmp_path / "library.db"),
        watch=True,
    )
    monkeypatch.setattr(server, "_config", config)
    calls = []
    monkeypatch.setattr(
        server,
        "watch_and_reindex",
        lambda xml, database, library_path=None: calls.append((xml, database, library_path)),
    )
    monkeypatch.setattr(server.mcp, "run", lambda: None)
    server.main()
    assert calls == [("", config.db_path, config.library_path)]


def test_classification_workflow_round_trip_is_read_only(wired):
    session = _payload(
        _call(
            "create_classification_session",
            {
                "seed": 7,
                "char_budget": 8000,
            },
        )
    )
    session_id = session["session_id"]

    sample = _payload(
        _call(
            "sample_references",
            {
                "session_id": session_id,
                "strategy": "random",
                "size": 4,
                "seed": 7,
            },
        )
    )
    assert sample["returned"] == 4
    assert sample["library_fingerprint"] == session["library_fingerprint"]

    taxonomy = _payload(
        _call(
            "save_classification_taxonomy",
            {
                "session_id": session_id,
                "taxonomy": {
                    "facets": [
                        {
                            "id": "topic",
                            "approved": True,
                            "slot": "custom_1",
                            "mode": "single",
                            "labels": ["Materials"],
                        }
                    ],
                },
            },
        )
    )
    _payload(
        _call(
            "approve_classification_taxonomy",
            {
                "session_id": session_id,
                "taxonomy_hash": taxonomy["hash"],
            },
        )
    )
    rules = _payload(
        _call(
            "save_classification_rules",
            {
                "session_id": session_id,
                "ruleset": {
                    "rules": [
                        {
                            "id": "material-title",
                            "facet": "topic",
                            "label": "Materials",
                            "priority": 10,
                            "when": [
                                {
                                    "op": "contains_any",
                                    "field": "title",
                                    "values": ["alloy", "alloys", "materials", "合金"],
                                }
                            ],
                        }
                    ],
                },
            },
        )
    )
    _payload(
        _call(
            "freeze_classification_rules",
            {
                "session_id": session_id,
                "ruleset_hash": rules["hash"],
            },
        )
    )
    evaluation = _payload(
        _call(
            "evaluate_classification_rules",
            {
                "session_id": session_id,
            },
        )
    )
    assert evaluation["assigned"] == 4
    assert (
        _payload(
            _call(
                "list_unresolved_classifications",
                {
                    "session_id": session_id,
                },
            )
        )
        == []
    )

    audit = _payload(
        _call(
            "create_classification_audit",
            {
                "session_id": session_id,
                "size": 4,
                "seed": 11,
            },
        )
    )
    labels = [
        {"rec_number": rec_number, "label": "Materials"} for rec_number in audit["rec_numbers"]
    ]
    assert (
        _payload(
            _call(
                "submit_classification_audit",
                {
                    "audit_set_id": audit["audit_set_id"],
                    "labels": labels,
                },
            )
        )
        == 4
    )
    report = _payload(
        _call(
            "get_classification_report",
            {
                "audit_set_id": audit["audit_set_id"],
            },
        )
    )
    assert report["accuracy"] == 1.0
    assert report["coverage"] == 1.0
    assert "whole-library accuracy" in report["scope_note"]

    # MCP-level prepare round-trip: the preview hashes all four EndNote
    # sources but must leave every byte and timestamp untouched.
    library = Path(wired._config.db_path).parent / "Research.enl"
    rdb = library.with_suffix(".Data") / "rdb"
    rdb.mkdir(parents=True)
    for path, body in (
        (library, b"enl"),
        (rdb / "refs.MYD", b"refs"),
        (rdb / "refs.MYI", b"index"),
        (rdb / "misc.MYD", b"groups"),
    ):
        path.write_bytes(body)
    sources = [library, rdb / "refs.MYD", rdb / "refs.MYI", rdb / "misc.MYD"]
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in sources}
    wired._config = Config(
        endnote_xml="",
        library_path=str(library),
        db_path=wired._config.db_path,
    )
    plan = _payload(
        _call(
            "prepare_classification_writeback",
            {
                "session_id": session_id,
                "idempotency_key": "mcp-prepare-roundtrip",
            },
        )
    )
    assert plan["changes"] == 4
    assert plan["field"] == "custom_1"
    after = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in sources}
    assert after == before


def _wire_real_library(wired, tmp_path: Path, rows: list[bytes]) -> Path:
    rdb, enl = _make_rdb_library(tmp_path, "Research2", rows)
    wired._config = Config(
        endnote_xml="",
        library_path=str(enl),
        db_path=wired._config.db_path,
    )
    return rdb


def test_update_reference_fields_dry_run_then_commit(wired, tmp_path):
    rdb = _wire_real_library(
        wired,
        tmp_path,
        [_row(1, title="A paper", author="Doe, J.", year="2022")],
    )

    preview = _payload(
        _call(
            "update_reference_fields",
            {"updates": [{"rec_number": 1, "volume": "14", "issue": "3", "pages": "1-9"}]},
        )
    )
    assert preview["dry_run"] is True
    assert preview["would_write"] == 3
    assert (rdb / "refs.MYD").exists()
    assert not (rdb.parent / "backups").exists()

    committed = _payload(
        _call(
            "update_reference_fields",
            {
                "updates": [{"rec_number": 1, "volume": "14", "issue": "3", "pages": "1-9"}],
                "dry_run": False,
            },
        )
    )
    assert committed["dry_run"] is False
    assert committed["records_written"] == 1
    assert Path(committed["backup_path"]).is_dir()

    details = _payload(_call("get_reference_details", {"rec_number": 1}))
    assert details["volume"] == "14"
    assert details["issue"] == "3"
    assert details["pages"] == "1-9"


def test_update_reference_fields_refuses_non_writable_fields(wired, tmp_path):
    _wire_real_library(wired, tmp_path, [_row(1, title="A paper")])
    with pytest.raises(Exception, match="not fields this tool may write"):
        _call("update_reference_fields", {"updates": [{"rec_number": 1, "title": "New Title"}]})


def test_update_reference_fields_writes_author_and_journal(wired, tmp_path):
    _wire_real_library(
        wired, tmp_path, [_row(1, title="A paper", author="Zhang, Z.", year="2024")]
    )
    committed = _payload(
        _call(
            "update_reference_fields",
            {
                "updates": [
                    {
                        "rec_number": 1,
                        "author": ["Zhang, Zhipeng", "Xiong, Tingjiao"],
                        "journal": "Journal of Physical Chemistry Letters",
                    }
                ],
                "dry_run": False,
            },
        )
    )
    assert committed["records_written"] == 1
    details = _payload(_call("get_reference_details", {"rec_number": 1}))
    assert details["authors"] == ["Zhang, Zhipeng", "Xiong, Tingjiao"]
    assert details["container_title"] == "Journal of Physical Chemistry Letters"


def test_mark_references_dry_run_then_commit(wired, tmp_path):
    rdb = _wire_real_library(
        wired,
        tmp_path,
        [_row(1, title="Duplicate copy", author="Doe, J.", year="2022")],
    )

    preview = _payload(
        _call(
            "mark_references",
            {"rec_numbers": [1], "marker": "DELETE_CANDIDATE"},
        )
    )
    assert preview == {"dry_run": True, "would_mark": 1, "already_marked": 0, "conflicts": {}}
    assert not (rdb.parent / "backups").exists()

    committed = _payload(
        _call(
            "mark_references",
            {"rec_numbers": [1], "marker": "DELETE_CANDIDATE", "dry_run": False},
        )
    )
    assert committed["records_written"] == 1
    assert Path(committed["backup_path"]).is_dir()


def test_mark_references_refuses_custom_1(wired, tmp_path):
    _wire_real_library(wired, tmp_path, [_row(1, title="A paper")])
    with pytest.raises(Exception, match="reserved"):
        _call("mark_references", {"rec_numbers": [1], "marker": "x", "column": "custom_1"})


def test_update_reference_fields_merges_entries_for_one_record(wired, tmp_path):
    """Splitting a record across entries is a reasonable thing to emit, and
    the old code kept only the last one — silently dropping fields the user
    was about to approve in the diff."""
    _wire_real_library(wired, tmp_path, [_row(1, title="A paper", year="2022")])

    preview = _payload(
        _call(
            "update_reference_fields",
            {
                "updates": [
                    {"rec_number": 1, "volume": "14"},
                    {"rec_number": 1, "pages": "1-9"},
                ]
            },
        )
    )
    assert preview["would_write"] == 2
    assert {(d["field"], d["new_value"]) for d in preview["diffs"]} == {
        ("volume", "14"),
        ("pages", "1-9"),
    }


def test_update_reference_fields_rejects_a_field_given_two_values(wired, tmp_path):
    _wire_real_library(wired, tmp_path, [_row(1, title="A paper")])
    with pytest.raises(Exception, match="give each field one value"):
        _call(
            "update_reference_fields",
            {
                "updates": [
                    {"rec_number": 1, "volume": "14"},
                    {"rec_number": 1, "volume": "15"},
                ]
            },
        )


def test_repeating_an_identical_entry_is_not_a_conflict(wired, tmp_path):
    _wire_real_library(wired, tmp_path, [_row(1, title="A paper")])
    preview = _payload(
        _call(
            "update_reference_fields",
            {
                "updates": [
                    {"rec_number": 1, "volume": "14"},
                    {"rec_number": 1, "volume": "14"},
                ]
            },
        )
    )
    assert preview["would_write"] == 1
