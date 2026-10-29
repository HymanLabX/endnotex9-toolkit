"""PDF attachment resolution, text extraction, and full-text search."""

from __future__ import annotations

from pathlib import Path

import pytest

from endnotex9_toolkit import pdf as pdfmod
from endnotex9_toolkit import search

fitz = pytest.importorskip("fitz", reason="PDF text extraction needs the [pdf] extra")

# Kept to short lines: insert_text does not wrap, and anything past the
# right margin is silently clipped out of the saved page.
PAGE_TEXT = [
    "Elastocaloric cooling in\nNi-Mn-Ga single crystals.",
    "The adiabatic temperature\nchange reached 10.5 K under\nuniaxial compres-\nsion.",
    "形状记忆合金的马氏体\n相变温度随成分变化。",
]


def _write_pdf(path: Path, pages: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    document = fitz.open()
    for body in pages:
        page = document.new_page()
        page.insert_text((72, 72), body, fontname="china-s", fontsize=11)
    document.save(str(path))
    document.close()


@pytest.fixture
def pdf_dir(tmp_path: Path) -> Path:
    root = tmp_path / "Library.Data" / "PDF"
    _write_pdf(root / "0740201097" / "2022-Li-ACS.pdf", PAGE_TEXT)
    return root


def test_resolve_exact_path(pdf_dir):
    resolved = pdfmod.resolve_pdf_path(pdf_dir, "0740201097/2022-Li-ACS.pdf")
    assert resolved is not None and resolved.name == "2022-Li-ACS.pdf"


def test_resolve_percent_encoded_path(pdf_dir):
    assert pdfmod.resolve_pdf_path(pdf_dir, "0740201097/2022-Li-ACS%2Epdf") is not None


def test_resolve_falls_back_to_the_only_pdf_in_the_folder(pdf_dir):
    # EndNote's exported filename does not always match the file on disk
    # byte for byte; the folder id does, and it holds one attachment.
    resolved = pdfmod.resolve_pdf_path(pdf_dir, "0740201097/Lopez-Pavon-renamed.pdf")
    assert resolved is not None and resolved.name == "2022-Li-ACS.pdf"


def test_resolve_returns_none_for_a_missing_folder(pdf_dir):
    assert pdfmod.resolve_pdf_path(pdf_dir, "9999999999/absent.pdf") is None
    assert pdfmod.resolve_pdf_path("", "0740201097/x.pdf") is None


@pytest.mark.parametrize(
    "untrusted",
    (
        "../outside.pdf",
        "..\\outside.pdf",
        "%2e%2e/outside.pdf",
        "C:\\outside.pdf",
        "\\\\server\\share\\outside.pdf",
        "/outside.pdf",
    ),
)
def test_resolve_rejects_paths_outside_attachment_root(pdf_dir, tmp_path, untrusted):
    (tmp_path / "outside.pdf").write_bytes(b"not an attachment")
    assert pdfmod.resolve_pdf_path(pdf_dir, untrusted) is None


def test_resolve_rejects_an_absolute_existing_pdf(pdf_dir, tmp_path):
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"not an attachment")
    assert pdfmod.resolve_pdf_path(pdf_dir, str(outside.resolve())) is None


def test_resolve_rejects_a_symlink_escape(pdf_dir, tmp_path):
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"not an attachment")
    link = pdf_dir / "0740201097" / "linked.pdf"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("creating symlinks is not permitted on this platform")
    assert pdfmod.resolve_pdf_path(pdf_dir, "0740201097/linked.pdf") is None


def test_index_extracts_every_page(conn, pdf_dir):
    stats = pdfmod.index_pdfs(conn, pdf_dir)
    assert stats["attachments_seen"] == 1
    assert stats["extracted"] == 1
    assert stats["pages"] == 3
    assert stats["failed"] == 0


def test_index_is_incremental(conn, pdf_dir):
    pdfmod.index_pdfs(conn, pdf_dir)
    again = pdfmod.index_pdfs(conn, pdf_dir)
    assert again["extracted"] == 0
    assert again["skipped_unchanged"] == 1

    forced = pdfmod.index_pdfs(conn, pdf_dir, force=True)
    assert forced["extracted"] == 1


def test_index_counts_a_missing_file_without_failing(conn, pdf_dir, tmp_path):
    stats = pdfmod.index_pdfs(conn, tmp_path / "nowhere")
    assert stats["missing_file"] == 1
    assert stats["extracted"] == 0


def test_index_survives_an_unreadable_pdf(conn, pdf_dir):
    target = pdf_dir / "0740201097" / "2022-Li-ACS.pdf"
    target.write_bytes(b"not actually a PDF")
    stats = pdfmod.index_pdfs(conn, pdf_dir)
    assert stats["failed"] == 1
    assert stats["extracted"] == 0


def test_fulltext_finds_text_that_is_not_in_the_metadata(conn, pdf_dir):
    pdfmod.index_pdfs(conn, pdf_dir)
    results = search.search_fulltext(conn, ["adiabatic temperature change"])
    assert results[0]["rec_number"] == 1
    match = results[0]["matches"][0]
    assert match["page"] == 2
    assert "**" in match["excerpt"]


def test_fulltext_rejoins_words_split_across_a_line_break(conn, pdf_dir):
    # The PDF contains "compres-\nsion"; without de-hyphenation the word
    # would be unfindable.
    pdfmod.index_pdfs(conn, pdf_dir)
    assert search.search_fulltext(conn, ["compression"])


def test_fulltext_handles_cjk(conn, pdf_dir):
    pdfmod.index_pdfs(conn, pdf_dir)
    results = search.search_fulltext(conn, ["记忆合金"])
    assert results and results[0]["matches"][0]["page"] == 3


def test_fulltext_is_empty_before_extraction(conn):
    assert search.search_fulltext(conn, ["adiabatic"]) == []


def test_read_pages_by_range(conn, pdf_dir):
    pdfmod.index_pdfs(conn, pdf_dir)
    result = pdfmod.read_pages(conn, 1, first_page=2, last_page=2)
    assert [p["page"] for p in result["pages"]] == [2]
    assert "adiabatic" in result["pages"][0]["text"]


def test_read_pages_truncates_and_says_so(conn, pdf_dir):
    pdfmod.index_pdfs(conn, pdf_dir)
    result = pdfmod.read_pages(conn, 1, max_chars=20)
    assert result["truncated"] is True
    assert result["chars_returned"] <= 20
    assert "narrower page range" in result["note"]


def test_read_pages_explains_an_empty_result(conn):
    result = pdfmod.read_pages(conn, 2)
    assert result["pages"] == []
    assert "index_pdfs" in result["note"]


def test_deleting_a_reference_drops_its_pdf_text(conn, pdf_dir):
    from endnotex9_toolkit import db

    pdfmod.index_pdfs(conn, pdf_dir)
    db.delete_reference(conn, 1)
    assert db.get_pdf_pages(conn, 1) == []
    assert search.search_fulltext(conn, ["adiabatic"]) == []
