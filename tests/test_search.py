"""Search behaviour: CJK segmentation, bilingual merge, filters, ranking."""

from __future__ import annotations

from endnotex9_toolkit import db, search, tokenizer


def test_cjk_search_matches_a_partial_word(conn):
    # "记忆合金" appears only inside the longer run "形状记忆合金材料"; FTS5's
    # own tokenizer would treat that run as one indivisible token.
    results = search.search_references(conn, ["记忆合金"], limit=5)
    assert [r["rec_number"] for r in results] == [3]


def test_english_search(conn):
    results = search.search_references(conn, ["martensitic transformation"], limit=5)
    assert results[0]["rec_number"] in {1, 2}


def test_multi_query_merge_is_deduplicated(conn):
    results = search.search_references(conn, ["记忆合金", "elastocaloric effect"], limit=10)
    ids = [r["rec_number"] for r in results]
    assert set(ids) >= {1, 3}
    assert len(ids) == len(set(ids))


def test_all_term_matches_outrank_partial_ones(conn):
    results = search.search_references(conn, ["giant elastocaloric ni-mn-ga"], limit=10)
    assert results[0]["rec_number"] == 1
    assert results[0]["matched_all_terms"] is True
    assert any(r["matched_all_terms"] is False for r in results[1:]) or len(results) == 1


def test_hits_carry_a_readable_excerpt(conn):
    results = search.search_references(conn, ["elastocaloric"], limit=5)
    excerpt = results[0]["excerpt"]
    assert "**" in excerpt  # the match is marked up
    assert "elastocaloric" in excerpt.lower()


def test_cjk_excerpt_is_not_spaced_out_by_the_segmenter(conn):
    # The FTS copy of this abstract reads "形状 记忆 合金 记忆合金 …"; quoting
    # that back would be unreadable, so excerpts come from the original text.
    excerpt = search.search_references(conn, ["记忆合金"], limit=1)[0]["excerpt"]
    assert "**记忆合金**" in excerpt
    assert "形状记忆合金材料" in excerpt.replace("**", "")


def test_excerpt_falls_back_to_the_abstract_head_for_a_title_only_match(conn):
    excerpt = search.search_references(conn, ["giant"], limit=1)[0]["excerpt"]
    assert excerpt.startswith("A large lattice volume change")


def test_year_filter(conn):
    unfiltered = {r["rec_number"] for r in search.search_references(conn, ["martensitic"])}
    assert unfiltered == {1, 2}  # 1 via its keywords, 2 via its title
    assert {
        r["rec_number"] for r in search.search_references(conn, ["martensitic"], year_from=2021)
    } == {1}
    assert {
        r["rec_number"] for r in search.search_references(conn, ["martensitic"], year_to=2020)
    } == {2}


def test_language_filter(conn):
    results = search.search_references(conn, ["马氏体相变", "martensitic"], language="zh")
    assert [r["rec_number"] for r in results] == [3]


def test_has_pdf_filter(conn):
    results = search.search_references(conn, ["martensitic", "elastocaloric"], has_pdf=True)
    assert [r["rec_number"] for r in results] == [1]


def test_ref_type_filter(conn):
    results = search.search_references(conn, ["shape memory"], ref_type="Book Section")
    assert [r["rec_number"] for r in results] == [4]


def test_empty_query_returns_nothing(conn):
    assert search.search_references(conn, ["", "  "]) == []


def test_punctuation_only_query_does_not_break_fts(conn):
    # An unquoted "-" or '"' is a syntax error in FTS5's query grammar.
    assert search.search_references(conn, ['- " * ()']) == []


def test_find_related_excludes_the_seed_record(conn):
    related = search.find_related(conn, 1, limit=5)
    assert 1 not in [r["rec_number"] for r in related]
    assert 2 in [r["rec_number"] for r in related]  # shares "martensitic transformation"


def test_tokenizer_preserves_latin_text_for_snippets():
    # Latin characters pass through untouched so snippet() stays readable;
    # only CJK runs are rewritten.
    assert tokenizer.to_fts_text("Ni-Mn-Ga Alloys") == "Ni-Mn-Ga Alloys"
    assert "记忆" in tokenizer.to_fts_text("形状记忆合金")


def test_tokenizer_deduplicates_query_terms():
    assert tokenizer.tokenize("alloy alloy ALLOY") == ["alloy"]


def test_search_limit_is_capped(conn):
    assert search._clamp(10_000) == db.MAX_LIMIT
    assert search._clamp(20) == 20
    # A nonsense limit clamps to the floor, not the ceiling: SQLite would
    # otherwise read `LIMIT -1` as "return everything".
    assert search._clamp(-1) == 1
    assert search._clamp(0) == 1
    assert search._clamp(None) == db.MAX_LIMIT


def test_a_negative_search_limit_does_not_empty_the_library(conn):
    # SQLite reads `LIMIT -1` as "no limit"; the clamp is what stops one tool
    # call from returning every record that matches.
    results = search.search_references(conn, ["martensitic"], limit=-1)
    assert 0 < len(results) <= db.MAX_LIMIT
