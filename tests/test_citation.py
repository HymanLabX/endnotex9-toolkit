"""Citation formatting: APA 7th, GB/T 7714-2015, and BibTeX export."""

from __future__ import annotations

import pytest

from endnotex9_mcp import bibtex, citation, db


def test_apa7_journal_article(conn):
    text = citation.format_citation(db.get_reference(conn, 1), style="apa7")
    assert text.startswith("Li, D., Li, Z. B., Zhang, X. L., & Liu, C. (2022).")
    assert "ACS Applied Materials & Interfaces, 14(1), 1505-1518." in text
    assert text.endswith("https://doi.org/10.1021/acsami.1c21109")


def test_apa7_falls_back_to_the_article_number(conn):
    text = citation.format_citation(db.get_reference(conn, 2), style="apa7")
    assert "Acta Materialia, 200, 106545." in text


def test_apa7_book_section_uses_the_chapter_layout(conn):
    text = citation.format_citation(db.get_reference(conn, 4), style="apa7")
    assert "In Shape Memory Materials (pp. 1-26)." in text
    assert text.endswith("Cambridge, Cambridge University Press.")


def test_gbt7714_truncates_after_three_authors(conn):
    # GB/T 7714-2015 §10.1.2: list at most three, then the marker; western
    # given names become initials with no periods.
    text = citation.format_citation(db.get_reference(conn, 1), style="gbt7714")
    assert text.startswith("Li D, Li Z B, Zhang X L, et al. ")
    assert "[J]" in text
    assert "ACS Applied Materials & Interfaces, 2022, 14(1): 1505-1518." in text
    assert text.endswith("DOI: 10.1021/acsami.1c21109.")


def test_gbt7714_uses_deng_for_a_chinese_source(conn):
    ref = db.get_reference(conn, 3)
    ref["authors"] = ref["authors"] + ["王五", "赵六"]
    text = citation.format_citation(ref, style="gbt7714")
    assert text.startswith("张三, 李四, 王五, 等. ")
    assert "金属学报, 2019, 55(3): 321-330." in text


def test_gbt7714_keeps_two_authors_intact(conn):
    text = citation.format_citation(db.get_reference(conn, 3), style="gbt7714")
    assert text.startswith("张三, 李四. ")


def test_gbt7714_document_type_codes(conn):
    assert "[M]" in citation.format_citation(db.get_reference(conn, 4), style="gbt7714")
    assert "[J]" in citation.format_citation(db.get_reference(conn, 2), style="gbt7714")


def test_gbt7714_uses_the_article_number_as_locator(conn):
    text = citation.format_citation(db.get_reference(conn, 2), style="gbt7714")
    assert "Acta Materialia, 2020, 200: 106545." in text


def test_style_aliases_and_unknown_styles(conn):
    ref = db.get_reference(conn, 1)
    assert citation.format_citation(ref, style="GB/T 7714") == citation.format_citation(
        ref, style="gbt7714"
    )
    with pytest.raises(ValueError, match="Unknown citation style"):
        citation.format_citation(ref, style="mla9")


def test_bibliography_numbering_and_sorting(conn):
    refs = db.get_references(conn, [4, 1, 2])

    gbt = citation.format_bibliography(refs, style="gbt7714")
    assert gbt.startswith("[1] Otsuka K.")  # numeric style keeps caller order

    apa = citation.format_bibliography(refs, style="apa7")
    assert not apa.startswith("[")
    assert apa.splitlines()[0].startswith("Li, D.")  # author-date style sorts


def test_bibtex_reuses_the_endnote_label_as_citekey(conn):
    entry = bibtex.format_bibtex(db.get_references(conn, [1]))
    assert entry.startswith("@article{dli2022acs,")


def test_bibtex_derives_a_key_when_label_is_empty(conn):
    entry = bibtex.format_bibtex(db.get_references(conn, [2]))
    assert entry.startswith("@article{smith2020martensitic,")


def test_bibtex_escapes_latex_specials_and_braces_the_title(conn):
    entry = bibtex.format_bibtex(db.get_references(conn, [1]))
    assert r"\&" in entry
    assert "title      = {{Giant Elastocaloric Effect in Ni-Mn-Ga-Based Alloys}}" in entry
    assert "pages      = {1505--1518}" in entry
    assert "author     = {Li, D. and Li, Z. B. and Zhang, X. L. and Liu, C.}" in entry


def test_bibtex_entry_types_follow_the_reference_type(conn):
    assert "@incollection{" in bibtex.format_bibtex(db.get_references(conn, [4]))


def test_bibtex_article_number_becomes_eid(conn):
    entry = bibtex.format_bibtex(db.get_references(conn, [2]))
    assert "eid        = {106545}" in entry


def test_bibtex_disambiguates_colliding_keys(conn):
    ref = db.get_reference(conn, 2)
    twin = dict(ref, rec_number=99)
    text = bibtex.format_bibtex([ref, twin])
    assert "@article{smith2020martensitic," in text
    assert "@article{smith2020martensitica," in text


def test_bibtex_omits_the_abstract_unless_asked(conn):
    refs = db.get_references(conn, [1])
    assert "abstract" not in bibtex.format_bibtex(refs)
    assert "abstract" in bibtex.format_bibtex(refs, include_abstract=True)
