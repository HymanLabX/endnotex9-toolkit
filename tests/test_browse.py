"""Filtered listing and library-level statistics."""

from __future__ import annotations

import pytest

from endnotex9_mcp import browse, db


def test_list_returns_everything_by_default(conn):
    result = browse.list_references(conn)
    assert result["total_matching"] == 4
    assert result["returned"] == 4


def test_list_sorts_by_year_descending(conn):
    years = [r["year"] for r in browse.list_references(conn)["references"]]
    assert years == ["2022", "2020", "2019", "1998"]


def test_list_sort_options(conn):
    assert [r["year"] for r in browse.list_references(conn, sort="year_asc")["references"]] == [
        "1998",
        "2019",
        "2020",
        "2022",
    ]
    titles = [r["title"] for r in browse.list_references(conn, sort="title")["references"]]
    assert titles == sorted(titles, key=str.casefold)
    with pytest.raises(ValueError, match="Unknown sort"):
        browse.list_references(conn, sort="relevance")


def test_list_filters_combine(conn):
    result = browse.list_references(conn, year_from=2019, year_to=2022, language="en")
    assert {r["rec_number"] for r in result["references"]} == {1, 2}


def test_list_filters_are_case_insensitive_substrings(conn):
    assert browse.list_references(conn, author="zhang")["total_matching"] == 1
    assert browse.list_references(conn, journal="acta")["total_matching"] == 1
    assert browse.list_references(conn, keyword="ELASTOCALORIC")["total_matching"] == 1
    assert browse.list_references(conn, label="dli")["total_matching"] == 1


def test_list_journal_filter_also_matches_the_abbreviation(conn):
    assert browse.list_references(conn, journal="ACS Appl. Mater.")["total_matching"] == 1


def test_list_has_pdf_filter(conn):
    assert browse.list_references(conn, has_pdf=True)["total_matching"] == 1
    assert browse.list_references(conn, has_pdf=False)["total_matching"] == 3


def test_list_paginates(conn):
    page = browse.list_references(conn, limit=2, offset=2)
    assert page["total_matching"] == 4
    assert page["returned"] == 2
    assert [r["rec_number"] for r in page["references"]] == [3, 4]


def test_stats_cover_size_and_completeness(conn):
    stats = browse.library_stats(conn)
    assert stats["total_references"] == 4
    assert stats["year_range"] == [1998, 2022]
    assert stats["with_abstract"] == 2
    assert stats["with_doi"] == 2
    assert stats["with_pdf_attachment"] == 1
    assert stats["pdfs_text_indexed"] == 0  # nothing extracted yet
    assert stats["by_type"]["Journal Article"] == 3
    assert stats["by_language"]["en"] == 2
    assert stats["last_indexed_at"]


def test_stats_rank_authors_journals_and_keywords(conn):
    stats = browse.library_stats(conn)
    assert "Li, D." in stats["top_authors"]
    assert stats["top_journals"]["ACS Applied Materials & Interfaces"] == 1
    assert "elastocaloric effect" in stats["top_keywords"]
    assert len(browse.library_stats(conn, top=2)["top_journals"]) == 2


def test_limit_is_capped_and_the_cap_is_reported(conn):
    """`limit` arrives from the calling model, and SQLite reads a negative
    LIMIT as unlimited — either way an unclamped value dumps the library into
    the reply."""
    result = browse.list_references(conn, limit=10_000)
    assert result["limit_capped"] == db.MAX_LIMIT
    assert result["returned"] <= db.MAX_LIMIT
    assert result["total_matching"] == 4


def test_a_negative_limit_does_not_mean_unlimited(conn):
    # SQLite reads `LIMIT -1` as "no limit". Clamping to the floor instead of
    # the ceiling keeps a nonsense value from being the widest possible one.
    result = browse.list_references(conn, limit=-1)
    assert result["returned"] == 1
    assert result["limit_capped"] == 1


def test_an_ordinary_limit_is_not_reported_as_capped(conn):
    assert "limit_capped" not in browse.list_references(conn, limit=2)


def test_a_negative_offset_is_not_an_error(conn):
    assert browse.list_references(conn, offset=-5)["offset"] == 0
