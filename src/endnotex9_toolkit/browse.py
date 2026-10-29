"""Browse and summarise the library without going through the search index.

Search answers "what matches these words". These two answer "what is in here
at all" — filtered listing and a library-level overview — which is what a
model needs before it can ask a good search question.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter

from . import db as dbmod

_LIST_FIELDS = (
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

_SORTS = {
    "year_desc": "CAST(NULLIF(year, '') AS INTEGER) DESC, rec_number DESC",
    "year_asc": "CAST(NULLIF(year, '') AS INTEGER) ASC, rec_number ASC",
    "title": "title COLLATE NOCASE ASC",
    "added_desc": "rec_number DESC",
}


def list_references(
    conn: sqlite3.Connection,
    year_from: int | None = None,
    year_to: int | None = None,
    author: str | None = None,
    journal: str | None = None,
    keyword: str | None = None,
    ref_type: str | None = None,
    language: str | None = None,
    label: str | None = None,
    has_pdf: bool | None = None,
    group: str | None = None,
    topic: str | None = None,
    sort: str = "year_desc",
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """Filtered, sorted listing. Substring filters are case-insensitive."""
    if sort not in _SORTS:
        raise ValueError(f"Unknown sort '{sort}'. Available: {', '.join(sorted(_SORTS))}")

    # `limit` comes from the calling model and SQLite treats a negative LIMIT
    # as unlimited, so an unclamped value empties the library into the reply.
    # `total_matching` plus `offset` already give the caller a correct way to
    # page through more than one call's worth.
    requested_limit = limit
    limit = max(1, min(int(limit), dbmod.MAX_LIMIT))
    offset = max(0, int(offset))

    clauses: list[str] = []
    params: list = []

    if group:
        clauses.append(
            "rec_number IN (SELECT m.rec_number FROM group_members m"
            " JOIN groups g ON g.id = m.group_id WHERE g.name = ? COLLATE NOCASE)"
        )
        params.append(group)
    if topic:
        clauses.append("custom_1 LIKE ? COLLATE NOCASE")
        params.append(f"%{topic}%")

    if year_from is not None:
        clauses.append("CAST(NULLIF(year, '') AS INTEGER) >= ?")
        params.append(year_from)
    if year_to is not None:
        clauses.append("CAST(NULLIF(year, '') AS INTEGER) <= ?")
        params.append(year_to)
    if author:
        clauses.append("authors LIKE ? COLLATE NOCASE")
        params.append(f"%{author}%")
    if journal:
        clauses.append(
            "(container_title LIKE ? COLLATE NOCASE OR container_title_short LIKE ? COLLATE NOCASE)"
        )
        params += [f"%{journal}%", f"%{journal}%"]
    if keyword:
        clauses.append("keywords LIKE ? COLLATE NOCASE")
        params.append(f"%{keyword}%")
    if ref_type:
        clauses.append("LOWER(ref_type) = LOWER(?)")
        params.append(ref_type)
    if language:
        clauses.append("LOWER(language) = LOWER(?)")
        params.append(language)
    if label:
        clauses.append("label LIKE ? COLLATE NOCASE")
        params.append(f"%{label}%")
    if has_pdf is not None:
        clauses.append(f"COALESCE(pdf_files, '[]') {'!=' if has_pdf else '='} '[]'")

    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    total = conn.execute(f"SELECT COUNT(*) AS c FROM refs{where}", params).fetchone()["c"]

    rows = conn.execute(
        f"SELECT * FROM refs{where} ORDER BY {_SORTS[sort]} LIMIT ? OFFSET ?",
        [*params, limit, offset],
    ).fetchall()

    items = []
    for row in rows:
        ref = dbmod.row_to_ref(row)
        item = {field: ref.get(field) for field in _LIST_FIELDS}
        item["has_pdf"] = bool(ref.get("pdf_files"))
        items.append(item)

    result = {
        "total_matching": total,
        "offset": offset,
        "returned": len(items),
        "references": items,
    }
    if requested_limit != limit:
        result["limit_capped"] = limit
    return result


def library_stats(conn: sqlite3.Connection, top: int = 15) -> dict:
    """A one-call overview: size, coverage, and where the library is dense."""
    total = dbmod.count_references(conn)

    def scalar(sql: str) -> int:
        return conn.execute(sql).fetchone()["c"]

    by_type = {
        row["ref_type"] or "(unspecified)": row["c"]
        for row in conn.execute(
            "SELECT ref_type, COUNT(*) AS c FROM refs GROUP BY ref_type ORDER BY c DESC"
        )
    }
    by_language = {
        row["language"] or "(unspecified)": row["c"]
        for row in conn.execute(
            "SELECT language, COUNT(*) AS c FROM refs GROUP BY language ORDER BY c DESC"
        )
    }
    by_year = {
        str(row["year"]): row["c"]
        for row in conn.execute(
            """
            SELECT year, COUNT(*) AS c FROM refs
            WHERE NULLIF(year, '') IS NOT NULL
            GROUP BY year ORDER BY CAST(year AS INTEGER) DESC LIMIT ?
            """,
            (top * 2,),
        )
    }
    top_journals = {
        row["container_title"]: row["c"]
        for row in conn.execute(
            """
            SELECT container_title, COUNT(*) AS c FROM refs
            WHERE NULLIF(container_title, '') IS NOT NULL
            GROUP BY container_title ORDER BY c DESC LIMIT ?
            """,
            (top,),
        )
    }

    author_counts: Counter[str] = Counter()
    keyword_counts: Counter[str] = Counter()
    for row in conn.execute("SELECT authors, keywords FROM refs"):
        author_counts.update(json.loads(row["authors"] or "[]"))
        keyword_counts.update(k.lower() for k in json.loads(row["keywords"] or "[]"))

    years = conn.execute(
        """
        SELECT MIN(CAST(year AS INTEGER)) AS lo, MAX(CAST(year AS INTEGER)) AS hi
        FROM refs WHERE NULLIF(year, '') IS NOT NULL
        """
    ).fetchone()

    return {
        "total_references": total,
        "year_range": [years["lo"], years["hi"]] if years and years["lo"] else None,
        "with_abstract": scalar(
            "SELECT COUNT(*) AS c FROM refs WHERE NULLIF(abstract,'') IS NOT NULL"
        ),
        "with_doi": scalar("SELECT COUNT(*) AS c FROM refs WHERE NULLIF(doi,'') IS NOT NULL"),
        "with_keywords": scalar(
            "SELECT COUNT(*) AS c FROM refs WHERE COALESCE(keywords,'[]') != '[]'"
        ),
        "with_pdf_attachment": scalar(
            "SELECT COUNT(*) AS c FROM refs WHERE COALESCE(pdf_files,'[]') != '[]'"
        ),
        "pdfs_text_indexed": dbmod.count_pdf_docs(conn),
        "pdf_pages_indexed": scalar("SELECT COUNT(*) AS c FROM pdf_pages"),
        "by_type": by_type,
        "by_language": by_language,
        "by_year": by_year,
        "top_journals": top_journals,
        "top_authors": dict(author_counts.most_common(top)),
        "top_keywords": dict(keyword_counts.most_common(top)),
        "groups": dbmod.count_groups(conn),
        "topics": topic_counts(conn),
        "last_indexed_at": dbmod.get_meta(conn, "last_indexed_at"),
        "last_pdf_indexed_at": dbmod.get_meta(conn, "last_pdf_indexed_at"),
        "last_groups_read_at": dbmod.get_meta(conn, "last_groups_read_at"),
    }


def topic_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """How many references carry each topic label.

    Labels live in EndNote's Custom 1 field, several per record joined by
    "; ", so they are split back apart here.
    """
    counts: Counter[str] = Counter()
    for row in conn.execute("SELECT custom_1 FROM refs WHERE NULLIF(custom_1,'') IS NOT NULL"):
        counts.update(part.strip() for part in row["custom_1"].split(";") if part.strip())
    return dict(counts.most_common())
