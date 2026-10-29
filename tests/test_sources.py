"""Reading references from the library itself, with the XML export as fallback."""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from endnotex9_toolkit import db, indexer, refstable, sources


def _block(payload: bytes) -> bytes:
    pad = (-(len(payload) + 4)) % 4
    return b"\x03" + struct.pack(">H", len(payload)) + bytes([pad]) + payload + b"\x00" * pad


def _row(rec_number: int, **fields) -> bytes:
    row = {"id": rec_number}
    row.update({k: v.encode("utf-8") if isinstance(v, str) else v for k, v in fields.items()})
    return refstable.encode_row(row)


@pytest.fixture
def rdb(tmp_path: Path) -> Path:
    """A library holding the same four records as the XML fixture, so the two
    readers can be compared."""
    folder = tmp_path / "library.Data" / "rdb"
    folder.mkdir(parents=True)

    sep = refstable.VALUE_SEPARATOR
    rows = [
        _row(
            1,
            title="Giant Elastocaloric Effect in Ni-Mn-Ga-Based Alloys",
            secondary_title="ACS Applied Materials & Interfaces",
            author=sep.join(["Li, D.", "Li, Z. B.", "Zhang, X. L.", "Liu, C."]),
            keywords=sep.join(["elastocaloric effect", "martensitic transformation"]),
            year="2022",
            volume="14",
            number="1",
            pages="1505-1518",
            abstract="A large lattice volume change boosts the elastocaloric effect.",
            label="dli2022acs",
            language="English",
            isbn="1944-8244",
            url="https://example.org/paper",
            link_to_pdf="internal-pdf://0740201097/2022-Li-ACS.pdf",
            electronic_resource_number="https://doi.org/10.1021/acsami.1c21109",
        ),
        _row(
            2,
            title="Martensitic transformation in shape memory alloys",
            secondary_title="Acta Materialia",
            author="Smith, John Robert",
            year="2020",
            volume="200",
            section="106545",
            language="en",
            electronic_resource_number="10.1016/j.actamat.2020.106545",
        ),
        _row(
            3,
            title="Ti-Nb-Zr-Sn形状记忆合金马氏体相变与弹热性能",
            secondary_title="金属学报",
            author=sep.join(["张三", "李四"]),
            year="2019",
            volume="55",
            number="3",
            pages="321-330",
            language="zh",
            abstract="研究了形状记忆合金材料的马氏体相变行为与弹热制冷性能。",
        ),
        _row(
            4,
            title="Introduction to shape memory materials",
            secondary_title="Shape Memory Materials",
            author="Otsuka, K.",
            year="1998",
            publisher="Cambridge University Press",
            place_published="Cambridge",
            pages="1-26",
            reference_type=7,
        ),
    ]
    (folder / "refs.MYD").write_bytes(b"".join(_block(r) for r in rows))
    (folder / "refs.frm").write_bytes(b"fake")
    return folder


# --------------------------------------------------------------------------
# the direct reader
# --------------------------------------------------------------------------


def test_reads_references_in_the_shape_the_parser_produces(rdb):
    refs = {r["rec_number"]: r for r in refstable.read_references(rdb)}
    ref = refs[1]
    assert ref["title"] == "Giant Elastocaloric Effect in Ni-Mn-Ga-Based Alloys"
    assert ref["container_title"] == "ACS Applied Materials & Interfaces"
    assert ref["authors"] == ["Li, D.", "Li, Z. B.", "Zhang, X. L.", "Liu, C."]
    assert ref["keywords"] == ["elastocaloric effect", "martensitic transformation"]
    assert ref["issue"] == "1"
    assert ref["label"] == "dli2022acs"


def test_carriage_returns_split_repeated_values(rdb):
    refs = {r["rec_number"]: r for r in refstable.read_references(rdb)}
    assert refs[3]["authors"] == ["张三", "李四"]


def test_doi_and_language_are_normalised_the_same_way(rdb):
    refs = {r["rec_number"]: r for r in refstable.read_references(rdb)}
    assert refs[1]["doi"] == "10.1021/acsami.1c21109"
    assert refs[1]["language"] == "en"  # from "English"
    assert refs[3]["language"] == "zh"


def test_article_number_and_pdf_path_survive(rdb):
    refs = {r["rec_number"]: r for r in refstable.read_references(rdb)}
    assert refs[2]["pages"] == ""
    assert refs[2]["article_number"] == "106545"
    assert refs[1]["pdf_files"] == ["0740201097/2022-Li-ACS.pdf"]


def test_reference_type_names_come_from_the_learned_mapping(rdb):
    plain = {r["rec_number"]: r for r in refstable.read_references(rdb)}
    assert plain[4]["ref_type"] == "endnote:type:7"  # stable direct-only code

    named = {
        r["rec_number"]: r
        for r in refstable.read_references(rdb, {0: "Journal Article", 7: "Book Section"})
    }
    assert named[1]["ref_type"] == "Journal Article"
    assert named[4]["ref_type"] == "Book Section"


def test_missing_library_is_reported(tmp_path: Path):
    with pytest.raises(FileNotFoundError, match=r"refs\.MYD"):
        list(refstable.read_rows(tmp_path))


# --------------------------------------------------------------------------
# source selection
# --------------------------------------------------------------------------


def test_library_is_preferred_over_the_export(tmp_path: Path, rdb, xml_path):
    conn = db.connect(tmp_path / "index.db")
    source, records, _note = sources.open_references(conn, xml_path, tmp_path / "library.enl")
    assert source == "library"
    assert len(list(records)) == 4


def test_falls_back_to_the_export_when_there_is_no_library(tmp_path: Path, xml_path):
    conn = db.connect(tmp_path / "index.db")
    source, records, note = sources.open_references(conn, xml_path, tmp_path / "absent.enl")
    assert source == "xml"
    assert "no readable library" in note
    assert len(list(records)) == 4


def test_falls_back_to_the_export_when_the_library_is_corrupt(tmp_path: Path, rdb, xml_path):
    (rdb / "refs.MYD").write_bytes(b"\xff" * 64)  # no valid block type
    conn = db.connect(tmp_path / "index.db")
    source, records, note = sources.open_references(conn, xml_path, tmp_path / "library.enl")
    assert source == "xml"
    assert "library read failed" in note
    assert len(list(records)) == 4


def test_raises_when_neither_source_exists(tmp_path: Path):
    conn = db.connect(tmp_path / "index.db")
    with pytest.raises(sources.SourceUnavailable):
        sources.open_references(conn, tmp_path / "absent.xml", tmp_path / "absent.enl")


def test_reference_type_names_are_learned_and_remembered(tmp_path: Path, rdb, xml_path):
    conn = db.connect(tmp_path / "index.db")
    source, records, note = sources.open_references(conn, xml_path, tmp_path / "library.enl")
    assert source == "library"
    assert "learned" in note
    assert sources.stored_ref_type_names(conn) == {0: "Journal Article", 7: "Book Section"}

    # Second call reuses the stored mapping rather than re-reading the export.
    _s, records, note = sources.open_references(conn, xml_path, tmp_path / "library.enl")
    assert "learned" not in note
    assert {r["rec_number"]: r["ref_type"] for r in records}[4] == "Book Section"


# --------------------------------------------------------------------------
# cross-check
# --------------------------------------------------------------------------


def test_crosscheck_finds_full_agreement(rdb, xml_path):
    mapping = sources.learn_ref_type_names(rdb, xml_path)
    report = sources.crosscheck(rdb, xml_path, mapping)
    assert report["compared"] == 4
    assert report["only_in_xml"] == [] and report["only_in_library"] == []
    disagreeing = {f: c for f, c in report["agreement"].items() if c != 4}
    assert not disagreeing, f"{disagreeing} {report['mismatch_examples']}"


def test_crosscheck_reports_a_disagreement(rdb, xml_path):
    rows = list(refstable.read_rows(rdb))
    rows[0]["title"] = b"A different title entirely"
    (rdb / "refs.MYD").write_bytes(b"".join(_block(refstable.encode_row(r)) for r in rows))

    report = sources.crosscheck(rdb, xml_path, {0: "Journal Article", 7: "Book Section"})
    assert report["agreement"]["title"] == 3
    assert report["mismatch_examples"]["title"]


# --------------------------------------------------------------------------
# indexing through the chosen source
# --------------------------------------------------------------------------


def test_index_from_the_library_alone(tmp_path: Path, rdb):
    conn = db.connect(tmp_path / "index.db")
    stats = indexer.build_index(None, conn, full=True, library_path=tmp_path / "library.enl")
    assert stats["source"] == "library"
    assert stats["total"] == 4
    assert db.get_reference(conn, 1)["title"].startswith("Giant Elastocaloric")
    assert db.get_reference(conn, 4)["ref_type"] == "endnote:type:7"


def test_reindexing_from_the_library_is_incremental(tmp_path: Path, rdb):
    conn = db.connect(tmp_path / "index.db")
    indexer.build_index(None, conn, full=True, library_path=tmp_path / "library.enl")
    again = indexer.build_index(None, conn, library_path=tmp_path / "library.enl")
    assert again["unchanged"] == 4
    assert again["added"] == 0


def test_the_two_sources_produce_the_same_index(tmp_path: Path, rdb, xml_path):
    from_lib = db.connect(tmp_path / "lib.db")
    indexer.build_index(None, from_lib, full=True, library_path=tmp_path / "library.enl")
    from_xml = db.connect(tmp_path / "xml.db")
    indexer.build_index(xml_path, from_xml, full=True)

    for rec in (1, 2, 3, 4):
        a, b = db.get_reference(from_lib, rec), db.get_reference(from_xml, rec)
        # ref_type comes from the learned mapping, absent here; the journal
        # abbreviation is resolved by EndNote at export time.
        for field in ("content_hash", "ref_type", "container_title_short"):
            a.pop(field), b.pop(field)
        assert a == b
