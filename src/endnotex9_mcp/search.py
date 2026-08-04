"""Search references via FTS5 — metadata, PDF full text, and "more like this".

Two design notes:

Bilingual expansion is deliberately *not* a hardcoded domain glossary here.
`search_references` accepts a list of queries and the MCP tool description
asks the calling model to supply Chinese and English phrasings itself — a
model that is already in the loop translates better than a static table, and
without pinning the tool to one research field.

Ranking runs the terms twice: once joined with AND, once with OR. Records
matching every term are strictly better answers, so they are placed above the
OR hits rather than being left to compete with them on bm25 alone. The OR
pass is what carries CJK queries, where jieba may segment the query and the
indexed text into different-but-overlapping words and an AND of every term
can legitimately match nothing.
"""

from __future__ import annotations

import re
import sqlite3

from . import db as dbmod
from . import tokenizer

# Column weights for bm25(): a title hit says far more than a keyword hit.
_BM25_WEIGHTS = "8.0, 2.0, 4.0, 3.0"  # title, authors, abstract, keywords

_MARK = "**"
_ELLIPSIS = "…"
_EXCERPT_WIDTH = 220

# Fields returned for a hit. Deliberately narrower than the full record:
# result lists are read by a model with a context budget, and the abstract
# is what `get_reference_details` is for.
def _clamp(limit: int) -> int:
    """Hold `limit` inside 1..MAX_LIMIT.

    `limit` arrives from the calling model, and SQLite reads a negative
    `LIMIT` as "no limit" — so an unclamped value turns one tool call into a
    dump of the whole library.
    """
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        return dbmod.MAX_LIMIT
    return max(1, min(limit, dbmod.MAX_LIMIT))


_SUMMARY_FIELDS = (
    "rec_number",
    "ref_type",
    "title",
    "authors",
    "year",
    "container_title",
    "volume",
    "issue",
    "pages",
    "article_number",
    "doi",
    "language",
    "label",
    "custom_1",
)


def _summarize(ref: dict) -> dict:
    summary = {field: ref.get(field) for field in _SUMMARY_FIELDS}
    summary["has_pdf"] = bool(ref.get("pdf_files"))
    return summary


def _term_pattern(terms: list[str]) -> re.Pattern | None:
    if not terms:
        return None
    # Longest first, so an alternation prefers 弹热效应 over the 热效应 that
    # jieba also emitted for the same run.
    ordered = sorted(set(terms), key=len, reverse=True)
    return re.compile("|".join(re.escape(t) for t in ordered), re.IGNORECASE)


def _excerpt(text: str, pattern: re.Pattern | None, width: int = _EXCERPT_WIDTH) -> str:
    """Cut a window around the first match and mark the matches in it.

    FTS5's own snippet() would be cheaper, but it can only quote the text it
    stored — and what it stored has every CJK run split into jieba words, so
    a Chinese excerpt would come back peppered with spaces and with the
    overlapping segments repeated. Working from the original text keeps the
    excerpt readable in both languages.
    """
    text = " ".join((text or "").split())
    if not text:
        return ""

    match = pattern.search(text) if pattern else None
    if match is None:
        window = text[:width]
        return window + (_ELLIPSIS if len(text) > width else "")

    start = max(0, match.start() - width // 3)
    end = min(len(text), start + width)
    window = text[start:end]
    if pattern is not None:
        window = pattern.sub(lambda m: f"{_MARK}{m.group(0)}{_MARK}", window)
    return (_ELLIPSIS if start > 0 else "") + window + (_ELLIPSIS if end < len(text) else "")


def _quote(term: str) -> str:
    """Wrap a term as an FTS5 string literal, escaping embedded quotes."""
    return '"' + term.replace('"', '""') + '"'


def _match_expressions(terms: list[str]) -> tuple[str, str]:
    """Return the (all-terms, any-term) FTS5 match expressions for a query."""
    quoted = [_quote(t) for t in terms]
    if len(quoted) == 1:
        return quoted[0], quoted[0]
    return " AND ".join(quoted), " OR ".join(quoted)


# Group names are not unique in EndNote, so a name filter matches every group
# that carries it.
_IN_GROUP_SQL = """r.rec_number IN (
    SELECT m.rec_number FROM group_members m
    JOIN groups g ON g.id = m.group_id
    WHERE g.name = ? COLLATE NOCASE
)"""


def _filter_clause(
    year_from: int | None,
    year_to: int | None,
    ref_type: str | None,
    language: str | None,
    has_pdf: bool | None,
    group: str | None = None,
    topic: str | None = None,
) -> tuple[str, list]:
    clauses: list[str] = []
    params: list = []
    if group:
        clauses.append(_IN_GROUP_SQL)
        params.append(group)
    if topic:
        # Topic labels are written into EndNote's Custom 1 field, several per
        # record separated by "; ", so a substring match is the right test —
        # the same one EndNote's smart groups use.
        clauses.append("r.custom_1 LIKE ? COLLATE NOCASE")
        params.append(f"%{topic}%")
    if year_from is not None:
        clauses.append("CAST(NULLIF(r.year, '') AS INTEGER) >= ?")
        params.append(year_from)
    if year_to is not None:
        clauses.append("CAST(NULLIF(r.year, '') AS INTEGER) <= ?")
        params.append(year_to)
    if ref_type:
        clauses.append("LOWER(r.ref_type) = LOWER(?)")
        params.append(ref_type)
    if language:
        clauses.append("LOWER(r.language) = LOWER(?)")
        params.append(language)
    if has_pdf is not None:
        op = "!=" if has_pdf else "="
        clauses.append(f"COALESCE(r.pdf_files, '[]') {op} '[]'")
    return ("".join(f" AND {c}" for c in clauses), params)


def search_references(
    conn: sqlite3.Connection,
    queries: list[str],
    limit: int = 20,
    year_from: int | None = None,
    year_to: int | None = None,
    ref_type: str | None = None,
    language: str | None = None,
    has_pdf: bool | None = None,
    group: str | None = None,
    topic: str | None = None,
) -> list[dict]:
    """Run each query variant against FTS5 and merge the results.

    A record's place in the merged list is decided first by whether any
    variant matched all of its terms, then by the best bm25 score it earned.
    """
    limit = _clamp(limit)
    where_extra, filter_params = _filter_clause(
        year_from, year_to, ref_type, language, has_pdf, group, topic
    )
    # FTS5's auxiliary functions want the table's own name, so refs_fts is
    # spelled out rather than aliased.
    sql = f"""
        SELECT refs_fts.rowid AS rec_number, bm25(refs_fts, {_BM25_WEIGHTS}) AS rank
        FROM refs_fts
        JOIN refs r ON r.rec_number = refs_fts.rowid
        WHERE refs_fts MATCH ?{where_extra}
        ORDER BY rank
        LIMIT ?
    """

    # rec_number -> (tier, rank); tier 0 = matched every term of some query.
    best: dict[int, tuple[int, float]] = {}
    all_terms: list[str] = []

    def run(expression: str, tier: int) -> int:
        rows = conn.execute(sql, [expression, *filter_params, limit]).fetchall()
        for row in rows:
            rec_number = row["rec_number"]
            candidate = (tier, row["rank"])
            if rec_number not in best or candidate < best[rec_number]:
                best[rec_number] = candidate
        return len(rows)

    for query in queries:
        terms = tokenizer.tokenize(query)
        if not terms:
            continue
        all_terms.extend(terms)
        match_all, match_any = _match_expressions(terms)
        hits = run(match_all, 0)
        if match_any != match_all and hits < limit:
            run(match_any, 1)

    if not best:
        return []

    pattern = _term_pattern(all_terms)
    ranked = sorted(best, key=lambda rec: best[rec])[:limit]
    results = []
    for ref in dbmod.get_references(conn, ranked):
        tier, rank = best[ref["rec_number"]]
        summary = _summarize(ref)
        summary["score"] = round(-rank, 3)
        summary["matched_all_terms"] = tier == 0
        summary["excerpt"] = _excerpt(ref.get("abstract") or ref.get("title") or "", pattern)
        results.append(summary)
    return results


def find_related(conn: sqlite3.Connection, rec_number: int, limit: int = 10) -> list[dict]:
    """Find references that read like a given one.

    Uses the source record's own title and keywords as the query — a poor
    man's more-like-this that needs no embeddings and stays honest about
    what it is: term overlap, ranked by bm25.
    """
    limit = _clamp(limit)
    ref = dbmod.get_reference(conn, rec_number)
    if ref is None:
        raise ValueError(f"No reference with rec_number={rec_number}")

    seed = " ".join(filter(None, [ref.get("title") or "", "; ".join(ref.get("keywords") or [])]))
    terms = tokenizer.tokenize(seed)
    if not terms:
        return []

    _, expression = _match_expressions(terms)
    rows = conn.execute(
        f"""
        SELECT refs_fts.rowid AS rec_number, bm25(refs_fts, {_BM25_WEIGHTS}) AS rank
        FROM refs_fts
        WHERE refs_fts MATCH ? AND refs_fts.rowid != ?
        ORDER BY rank
        LIMIT ?
        """,
        (expression, rec_number, limit),
    ).fetchall()

    ranked = [row["rec_number"] for row in rows]
    ranks = {row["rec_number"]: row["rank"] for row in rows}
    results = []
    for related in dbmod.get_references(conn, ranked):
        summary = _summarize(related)
        summary["score"] = round(-ranks[related["rec_number"]], 3)
        results.append(summary)
    return results


def search_fulltext(
    conn: sqlite3.Connection,
    queries: list[str],
    limit: int = 15,
    max_pages_per_ref: int = 3,
) -> list[dict]:
    """Search the extracted text of attached PDFs.

    Results are grouped per reference, each carrying its best-matching pages
    so a follow-up `read_pdf_pages` call knows where to look.
    """
    limit = _clamp(limit)
    max_pages_per_ref = max(1, min(int(max_pages_per_ref or 1), 20))
    sql = """
        SELECT pdf_fts.rowid AS page_id, p.rec_number, p.page, p.rel_path,
               bm25(pdf_fts) AS rank
        FROM pdf_fts
        JOIN pdf_pages p ON p.id = pdf_fts.rowid
        WHERE pdf_fts MATCH ?
        ORDER BY rank
        LIMIT ?
    """
    # rec_number -> {"rank": best rank, "pages": {page: (rank, page_id, file)}}
    hits: dict[int, dict] = {}
    all_terms: list[str] = []

    for query in queries:
        terms = tokenizer.tokenize(query)
        if not terms:
            continue
        all_terms.extend(terms)
        match_all, match_any = _match_expressions(terms)
        for expression in (match_all, match_any):
            rows = conn.execute(sql, (expression, limit * max_pages_per_ref * 4)).fetchall()
            for row in rows:
                entry = hits.setdefault(row["rec_number"], {"rank": row["rank"], "pages": {}})
                entry["rank"] = min(entry["rank"], row["rank"])
                page = row["page"]
                if page not in entry["pages"] or row["rank"] < entry["pages"][page][0]:
                    entry["pages"][page] = (row["rank"], row["page_id"], row["rel_path"])
            if rows:
                break  # the AND pass already found something; skip the looser one

    if not hits:
        return []

    ranked = sorted(hits, key=lambda rec: hits[rec]["rank"])[:limit]
    pattern = _term_pattern(all_terms)

    # Page bodies are fetched only for the pages that survived ranking —
    # excerpting from the original text rather than from the segmented copy
    # FTS5 holds keeps CJK excerpts readable.
    chosen: dict[int, list[tuple[int, int, str]]] = {}
    page_ids: list[int] = []
    for rec_number in ranked:
        pages = sorted(hits[rec_number]["pages"].items(), key=lambda item: item[1][0])
        pages = pages[:max_pages_per_ref]
        chosen[rec_number] = [(page, value[1], value[2]) for page, value in pages]
        page_ids.extend(value[1] for _page, value in pages)

    bodies = {}
    if page_ids:
        placeholders = ", ".join("?" for _ in page_ids)
        bodies = {
            row["id"]: row["body"]
            for row in conn.execute(
                f"SELECT id, body FROM pdf_pages WHERE id IN ({placeholders})", page_ids
            )
        }

    results = []
    for ref in dbmod.get_references(conn, ranked):
        summary = _summarize(ref)
        summary["score"] = round(-hits[ref["rec_number"]]["rank"], 3)
        summary["matches"] = [
            {
                "page": page,
                "file": rel_path,
                "excerpt": _excerpt(bodies.get(page_id, ""), pattern),
            }
            for page, page_id, rel_path in sorted(chosen[ref["rec_number"]])
        ]
        results.append(summary)
    return results
