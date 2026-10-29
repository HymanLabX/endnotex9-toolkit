"""Parsing and indexing: XML in, queryable index out."""

from __future__ import annotations

from pathlib import Path

import pytest

from endnotex9_toolkit import db, indexer, parser


def test_index_counts(conn):
    assert db.count_references(conn) == 4


def test_fields_are_parsed_from_styled_xml(conn):
    ref = db.get_reference(conn, 1)
    assert ref["title"] == "Giant Elastocaloric Effect in Ni-Mn-Ga-Based Alloys"
    assert ref["container_title"] == "ACS Applied Materials & Interfaces"
    assert ref["container_title_short"] == "ACS Appl. Mater. Interfaces"
    assert ref["authors"] == ["Li, D.", "Li, Z. B.", "Zhang, X. L.", "Liu, C."]
    assert ref["keywords"] == ["elastocaloric effect", "martensitic transformation"]
    assert ref["volume"] == "14"
    assert ref["issue"] == "1"
    assert ref["pages"] == "1505-1518"
    assert ref["label"] == "dli2022acs"
    assert ref["url"] == "https://example.org/paper"


def test_doi_prefix_is_stripped(conn):
    assert db.get_reference(conn, 1)["doi"] == "10.1021/acsami.1c21109"
    assert db.get_reference(conn, 2)["doi"] == "10.1016/j.actamat.2020.106545"


def test_language_is_normalized(conn):
    assert db.get_reference(conn, 1)["language"] == "en"  # from "English"
    assert db.get_reference(conn, 2)["language"] == "en"
    assert db.get_reference(conn, 3)["language"] == "zh"


def test_article_number_is_captured(conn):
    ref = db.get_reference(conn, 2)
    assert ref["pages"] == ""
    assert ref["article_number"] == "106545"


def test_pdf_attachment_keeps_its_folder_id(conn):
    # The folder id is where the file actually lives; dropping it makes the
    # attachment unresolvable on disk.
    assert db.get_reference(conn, 1)["pdf_files"] == ["0740201097/2022-Li-ACS.pdf"]
    assert db.get_reference(conn, 2)["pdf_files"] == []


def test_article_number_line_is_not_part_of_the_doi():
    doi = "10.1016/j.jallcom.2023.170291"
    assert parser.normalize_doi(f"ARTN 170291\r{doi}") == doi
    assert parser.normalize_doi(f"Artn 042902\r{doi}") == doi
    assert parser.normalize_doi(f"ARTN s12598-023-02374-1\r\n{doi}") == doi
    assert parser.normalize_doi(f"ARTN 170291 {doi}") == doi
    assert parser.normalize_doi(f"ARTN 170291\rhttps://doi.org/{doi}") == doi
    assert parser.normalize_doi("ARTN 170291") == ""
    assert parser.normalize_doi("") == ""


def test_serial_number_is_cleaned():
    assert parser._clean_serial_number("1878-0180 (Electronic)\r1878-0180 (Linking)") == "1878-0180"


def test_incremental_reindex_skips_unchanged(conn, xml_path):
    stats = indexer.build_index(xml_path, conn, full=False)
    assert stats["added"] == 0
    assert stats["unchanged"] == 4


def test_incremental_reindex_updates_a_changed_record(conn, xml_path):
    """A contentless FTS5 table cannot serve `DELETE ... WHERE rowid = ?`,
    which made this exact path — re-indexing an edited record — raise
    OperationalError. Guard it."""
    edited = xml_path.read_text(encoding="utf-8").replace(
        "Martensitic transformation in shape memory alloys",
        "Martensitic transformation in nickel titanium alloys",
    )
    xml_path.write_text(edited, encoding="utf-8")

    stats = indexer.build_index(xml_path, conn, full=False)
    assert stats["updated"] == 1
    assert stats["unchanged"] == 3
    assert db.get_reference(conn, 2)["title"].endswith("nickel titanium alloys")

    # The FTS index must follow the edit, not accumulate both versions.
    from endnotex9_toolkit import search

    assert search.search_references(conn, ["nickel titanium"], limit=5)[0]["rec_number"] == 2
    assert not [
        r
        for r in search.search_references(conn, ["shape memory alloys"], limit=5)
        if r["rec_number"] == 2 and "shape memory alloys" in r["title"]
    ]


def test_removed_record_is_dropped_from_index(conn, xml_path, tmp_path: Path):
    text = xml_path.read_text(encoding="utf-8")
    start = text.index("<record>\n  <rec-number>4</rec-number>")
    end = text.index("</record>", start) + len("</record>")
    xml_path.write_text(text[:start] + text[end:], encoding="utf-8")

    stats = indexer.build_index(xml_path, conn, full=False)
    assert stats["removed"] == 1
    assert db.get_reference(conn, 4) is None


def test_schema_change_fails_closed_and_preserves_the_database(tmp_path: Path, monkeypatch):
    path = tmp_path / "lib.db"
    conn = db.connect(path)
    conn.execute("INSERT INTO refs (rec_number, title, content_hash) VALUES (1, 'Keep me', 'x')")
    conn.commit()
    conn.close()

    monkeypatch.setattr(db, "SCHEMA_VERSION", db.SCHEMA_VERSION + 1)
    with pytest.raises(RuntimeError, match="Refusing to rebuild automatically"):
        db.connect(path)

    monkeypatch.setattr(db, "SCHEMA_VERSION", db.SCHEMA_VERSION - 1)
    conn = db.connect(path)
    assert db.get_meta(conn, "schema_version") == str(db.SCHEMA_VERSION)
    assert db.get_reference(conn, 1)["title"] == "Keep me"
