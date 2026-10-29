"""SQLite storage: reference metadata, an FTS5 search index, and PDF page text.

Two things worth knowing about the FTS tables:

* They are *regular* FTS5 tables, not `content=''` contentless ones. A
  contentless table cannot serve `DELETE ... WHERE rowid = ?`, which is
  exactly what re-indexing a changed record needs, and it can't back
  `snippet()` either. The duplicated text costs a few MB on a library this
  size — a fine trade for a working incremental update.
* They are fed pre-segmented text (see tokenizer.py) so CJK terms are
  searchable as individual words instead of one opaque blob per run.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import unicodedata
from pathlib import Path

from . import tokenizer

# Upper bound on how many records any retrieval path hands back in one call.
# The consumer is a model with a context budget, and `limit` arrives from that
# same model: an unbounded value (or SQLite's `LIMIT -1`, which means "no
# limit") would empty the library into the conversation.
MAX_LIMIT = 200

# Bumped whenever the host index schema changes.  A mismatch fails closed:
# classification sessions now live beside the index, so silently dropping the
# database would destroy user-reviewed work as well as rebuildable FTS data.
SCHEMA_VERSION = 4

# Columns of `refs`, in storage order. Kept as data so the insert statement
# can't drift out of sync with the table definition.
REF_COLUMNS = (
    "rec_number",
    "ref_type",
    "title",
    "title_short",
    "container_title",
    "container_title_short",
    "collection_title",
    "year",
    "volume",
    "issue",
    "pages",
    "article_number",
    "number_of_volumes",
    "abstract",
    "doi",
    "url",
    "publisher",
    "publisher_place",
    "edition",
    "isbn",
    "language",
    "label",
    "note",
    "research_notes",
    "accession_number",
    "access_date",
    "custom_1",
    "authors",
    "editors",
    "keywords",
    "urls",
    "pdf_files",
    "content_hash",
)

# Stored as JSON arrays, decoded on the way out.
_JSON_COLUMNS = ("authors", "editors", "keywords", "urls", "pdf_files")

_FTS_TOKENIZE = "unicode61 remove_diacritics 2"

# Transaction locks are keyed by *database file*, not by connection object:
# the watcher reindexes on its own connection from a background thread while
# MCP tool handlers write on the shared one, and only a file-scoped lock makes
# those two mutually exclusive. (Keying on `id(conn)` would also alias a
# recycled id onto a dead connection's lock, and the watcher opens and closes
# a connection per reindex, so ids really do get recycled here.)
_DB_LOCKS: dict[str, threading.RLock] = {}
_DB_LOCKS_GUARD = threading.Lock()


class _Connection(sqlite3.Connection):
    """A connection that carries the lock guarding its database file."""

    endnotex9_lock: threading.RLock


SCHEMA = f"""
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS refs (
    rec_number            INTEGER PRIMARY KEY,
    ref_type              TEXT,
    title                 TEXT,
    title_short           TEXT,
    container_title       TEXT,
    container_title_short TEXT,
    collection_title      TEXT,
    year                  TEXT,
    volume                TEXT,
    issue                 TEXT,
    pages                 TEXT,
    article_number        TEXT,
    number_of_volumes     TEXT,
    abstract              TEXT,
    doi                   TEXT,
    url                   TEXT,
    publisher             TEXT,
    publisher_place       TEXT,
    edition               TEXT,
    isbn                  TEXT,
    language              TEXT,
    label                 TEXT,
    note                  TEXT,
    research_notes        TEXT,
    accession_number      TEXT,
    access_date           TEXT,
    -- EndNote Custom 1. This package writes topic labels here (see
    -- writeback.py); the `topic` search filter matches against it.
    custom_1              TEXT,
    authors               TEXT,
    editors               TEXT,
    keywords              TEXT,
    urls                  TEXT,
    pdf_files             TEXT,
    content_hash          TEXT
);

CREATE INDEX IF NOT EXISTS idx_refs_year ON refs(year);
CREATE INDEX IF NOT EXISTS idx_refs_type ON refs(ref_type);

CREATE VIRTUAL TABLE IF NOT EXISTS refs_fts USING fts5(
    title, authors, abstract, keywords,
    tokenize='{_FTS_TOKENIZE}'
);

-- One row per PDF attachment that has been text-extracted.
CREATE TABLE IF NOT EXISTS pdf_docs (
    rec_number  INTEGER NOT NULL,
    rel_path    TEXT    NOT NULL,
    abs_path    TEXT,
    page_count  INTEGER,
    char_count  INTEGER,
    file_hash   TEXT,
    status      TEXT,
    PRIMARY KEY (rec_number, rel_path)
);

CREATE TABLE IF NOT EXISTS pdf_pages (
    id         INTEGER PRIMARY KEY,
    rec_number INTEGER NOT NULL,
    rel_path   TEXT    NOT NULL,
    page       INTEGER NOT NULL,
    body       TEXT,
    UNIQUE (rec_number, rel_path, page)
);

CREATE INDEX IF NOT EXISTS idx_pdf_pages_rec ON pdf_pages(rec_number);

CREATE VIRTUAL TABLE IF NOT EXISTS pdf_fts USING fts5(
    body,
    tokenize='{_FTS_TOKENIZE}'
);

-- EndNote's own groups, read out of the library's MyISAM tables rather than
-- the XML export (which omits them entirely). Names are not unique — a
-- library can hold two groups called "New Group" — so the key is synthetic.
CREATE TABLE IF NOT EXISTS groups (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    group_set  TEXT,
    smart      INTEGER NOT NULL DEFAULT 0,
    query      TEXT,
    uuid       TEXT
);

CREATE TABLE IF NOT EXISTS group_members (
    group_id   INTEGER NOT NULL,
    rec_number INTEGER NOT NULL,
    PRIMARY KEY (group_id, rec_number)
);

CREATE INDEX IF NOT EXISTS idx_group_members_rec ON group_members(rec_number);
"""


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name = ?", (name,)
    ).fetchone()
    return row is not None


def _lock_for_path(db_path: str | Path) -> threading.RLock:
    # normcase because two spellings of one Windows path must not get two
    # locks; resolve() for the relative-vs-absolute case on every platform.
    key = os.path.normcase(str(Path(db_path).resolve()))
    with _DB_LOCKS_GUARD:
        return _DB_LOCKS.setdefault(key, threading.RLock())


def _main_database_path(conn: sqlite3.Connection) -> str:
    for _seq, name, file in conn.execute("PRAGMA database_list"):
        if name == "main":
            return file or ""
    return ""


def connection_lock(conn: sqlite3.Connection) -> threading.RLock:
    """The process-local transaction lock for this connection's database file.

    Every connection to one database shares a lock, so a background reindex
    and an MCP write cannot interleave their transactions.
    """
    lock = getattr(conn, "endnotex9_lock", None)
    if lock is not None:
        return lock
    # A connection opened without `connect()` (a raw sqlite3 handle in a test,
    # say) still resolves to the same lock as its file. An in-memory database
    # reports an empty path and gets the one lock they all share, which is
    # correct: they are only ever used single-threaded.
    return _lock_for_path(_main_database_path(conn) or ":memory:")


def _stored_version(conn: sqlite3.Connection) -> int | None:
    if _table_exists(conn, "meta"):
        row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        if row is not None:
            try:
                return int(row[0])
            except (TypeError, ValueError):
                return None
        return None
    # A `refs` table with no `meta` table is the pre-versioning v1 layout.
    return 1 if _table_exists(conn, "refs") else None


def connect(db_path: str | Path) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False because the MCP SDK dispatches synchronous tool
    # handlers onto a worker thread pool: the connection is opened on
    # whichever thread served the first call and used from another on the
    # next one. Sharing it is safe — CPython's sqlite3 reports
    # threadsafety == 3 (SQLITE_THREADSAFE serialized), so the library
    # serialises access internally — and WAL keeps the background reindex
    # from blocking readers.
    conn = sqlite3.connect(str(db_path), timeout=15.0, check_same_thread=False, factory=_Connection)
    conn.endnotex9_lock = _lock_for_path(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 10000")

    version = _stored_version(conn)
    if version is not None and version != SCHEMA_VERSION:
        conn.close()
        raise RuntimeError(
            f"Unsupported database schema version {version}; expected "
            f"{SCHEMA_VERSION}. Refusing to rebuild automatically because "
            "the database may contain classification sessions. Re-index into "
            "a new database or run an explicit migration."
        )

    conn.executescript(SCHEMA)
    if version is None:
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        conn.commit()
    return conn


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


# --------------------------------------------------------------------------
# references
# --------------------------------------------------------------------------


def content_hashes(conn: sqlite3.Connection) -> dict[int, str]:
    """All known (rec_number -> content_hash) pairs in one query.

    Indexing reads this once instead of issuing a SELECT per record; on the
    reference library that turns ~1.5k round trips into one.
    """
    return {
        row["rec_number"]: row["content_hash"]
        for row in conn.execute("SELECT rec_number, content_hash FROM refs")
    }


def upsert_reference(conn: sqlite3.Connection, ref: dict) -> None:
    rec_number = ref["rec_number"]

    values = []
    for column in REF_COLUMNS:
        value = ref.get(column)
        if column in _JSON_COLUMNS:
            values.append(json.dumps(value or [], ensure_ascii=False))
        else:
            values.append(value)

    placeholders = ", ".join("?" for _ in REF_COLUMNS)
    conn.execute(
        f"INSERT OR REPLACE INTO refs ({', '.join(REF_COLUMNS)}) VALUES ({placeholders})",
        values,
    )

    conn.execute("DELETE FROM refs_fts WHERE rowid = ?", (rec_number,))
    conn.execute(
        "INSERT INTO refs_fts (rowid, title, authors, abstract, keywords) VALUES (?, ?, ?, ?, ?)",
        (
            rec_number,
            tokenizer.to_fts_text(ref.get("title") or ""),
            tokenizer.to_fts_text("; ".join(ref.get("authors") or [])),
            tokenizer.to_fts_text(ref.get("abstract") or ""),
            tokenizer.to_fts_text("; ".join(ref.get("keywords") or [])),
        ),
    )


def delete_reference(conn: sqlite3.Connection, rec_number: int) -> None:
    conn.execute("DELETE FROM refs WHERE rec_number = ?", (rec_number,))
    conn.execute("DELETE FROM refs_fts WHERE rowid = ?", (rec_number,))
    delete_pdf_text(conn, rec_number)


def row_to_ref(row: sqlite3.Row) -> dict:
    ref = dict(row)
    for column in _JSON_COLUMNS:
        if column in ref:
            ref[column] = json.loads(ref[column] or "[]")
    return ref


def get_reference(conn: sqlite3.Connection, rec_number: int) -> dict | None:
    row = conn.execute("SELECT * FROM refs WHERE rec_number = ?", (rec_number,)).fetchone()
    return row_to_ref(row) if row is not None else None


def find_references_by_title(conn: sqlite3.Connection, title: str, limit: int = 20) -> list[dict]:
    """Find references by exact title first, then by title substring.

    Matching is normalized and case-insensitive so a title copied from a PDF
    or bibliography is easy to use. Exact matches are preferred even when a
    shorter title would also occur inside another title.
    """
    needle = unicodedata.normalize("NFC", title or "").strip().casefold()
    if not needle:
        return []
    rows = conn.execute(
        "SELECT * FROM refs WHERE title IS NOT NULL AND trim(title) <> ''"
    ).fetchall()

    def normalized(row: sqlite3.Row) -> str:
        return unicodedata.normalize("NFC", row["title"]).strip().casefold()

    exact = [row for row in rows if normalized(row) == needle]
    candidates = exact or [row for row in rows if needle in normalized(row)]
    return [row_to_ref(row) for row in candidates[: max(1, limit)]]


def find_references_by_doi(conn: sqlite3.Connection, doi: str) -> list[dict]:
    """Find every record carrying an exact, case-insensitive DOI."""
    rows = conn.execute(
        "SELECT * FROM refs WHERE doi IS NOT NULL AND trim(doi) <> '' "
        "AND doi = ? COLLATE NOCASE ORDER BY rec_number",
        (doi,),
    ).fetchall()
    return [row_to_ref(row) for row in rows]


def get_references(conn: sqlite3.Connection, rec_numbers: list[int]) -> list[dict]:
    """Fetch several references at once, preserving the requested order."""
    if not rec_numbers:
        return []
    placeholders = ", ".join("?" for _ in rec_numbers)
    rows = conn.execute(
        f"SELECT * FROM refs WHERE rec_number IN ({placeholders})", rec_numbers
    ).fetchall()
    by_id = {row["rec_number"]: row_to_ref(row) for row in rows}
    return [by_id[n] for n in rec_numbers if n in by_id]


def count_references(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) AS c FROM refs").fetchone()["c"]


# --------------------------------------------------------------------------
# PDF text
# --------------------------------------------------------------------------


def get_pdf_doc(conn: sqlite3.Connection, rec_number: int, rel_path: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM pdf_docs WHERE rec_number = ? AND rel_path = ?", (rec_number, rel_path)
    ).fetchone()
    return dict(row) if row is not None else None


def delete_pdf_text(conn: sqlite3.Connection, rec_number: int, rel_path: str | None = None) -> None:
    if rel_path is None:
        ids = [
            r["id"]
            for r in conn.execute("SELECT id FROM pdf_pages WHERE rec_number = ?", (rec_number,))
        ]
        conn.execute("DELETE FROM pdf_pages WHERE rec_number = ?", (rec_number,))
        conn.execute("DELETE FROM pdf_docs WHERE rec_number = ?", (rec_number,))
    else:
        ids = [
            r["id"]
            for r in conn.execute(
                "SELECT id FROM pdf_pages WHERE rec_number = ? AND rel_path = ?",
                (rec_number, rel_path),
            )
        ]
        conn.execute(
            "DELETE FROM pdf_pages WHERE rec_number = ? AND rel_path = ?", (rec_number, rel_path)
        )
        conn.execute(
            "DELETE FROM pdf_docs WHERE rec_number = ? AND rel_path = ?", (rec_number, rel_path)
        )
    for page_id in ids:
        conn.execute("DELETE FROM pdf_fts WHERE rowid = ?", (page_id,))


def store_pdf_text(
    conn: sqlite3.Connection,
    rec_number: int,
    rel_path: str,
    abs_path: str,
    pages: list[str],
    file_hash: str,
    status: str = "ok",
) -> None:
    """Replace the stored text of one PDF attachment."""
    delete_pdf_text(conn, rec_number, rel_path)
    conn.execute(
        """
        INSERT INTO pdf_docs (rec_number, rel_path, abs_path, page_count, char_count, file_hash, status)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (rec_number, rel_path, abs_path, len(pages), sum(len(p) for p in pages), file_hash, status),
    )
    for page_number, body in enumerate(pages, start=1):
        if not body.strip():
            continue
        cursor = conn.execute(
            "INSERT INTO pdf_pages (rec_number, rel_path, page, body) VALUES (?, ?, ?, ?)",
            (rec_number, rel_path, page_number, body),
        )
        conn.execute(
            "INSERT INTO pdf_fts (rowid, body) VALUES (?, ?)",
            (cursor.lastrowid, tokenizer.to_fts_text(body)),
        )


def get_pdf_pages(
    conn: sqlite3.Connection, rec_number: int, first: int = 1, last: int | None = None
) -> list[dict]:
    sql = "SELECT rel_path, page, body FROM pdf_pages WHERE rec_number = ? AND page >= ?"
    params: list = [rec_number, first]
    if last is not None:
        sql += " AND page <= ?"
        params.append(last)
    sql += " ORDER BY rel_path, page"
    return [dict(row) for row in conn.execute(sql, params)]


def count_pdf_docs(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) AS c FROM pdf_docs WHERE status = 'ok'").fetchone()["c"]


# --------------------------------------------------------------------------
# groups
# --------------------------------------------------------------------------


def replace_groups(conn: sqlite3.Connection, groups: list) -> int:
    """Swap in a freshly read group tree. Cheap enough to rewrite wholesale —
    a large library still has only tens of groups."""
    conn.execute("DELETE FROM group_members")
    conn.execute("DELETE FROM groups")
    members = 0
    for group in groups:
        cursor = conn.execute(
            "INSERT INTO groups (name, group_set, smart, query, uuid) VALUES (?, ?, ?, ?, ?)",
            (group.name, group.group_set, int(group.smart), group.query, group.uuid),
        )
        conn.executemany(
            "INSERT OR IGNORE INTO group_members (group_id, rec_number) VALUES (?, ?)",
            [(cursor.lastrowid, rec) for rec in group.rec_numbers],
        )
        members += len(group.rec_numbers)
    return members


def list_groups(conn: sqlite3.Connection, include_empty: bool = True) -> list[dict]:
    """Groups with their sizes, counting only members that are actually
    indexed — EndNote's cached member lists can outlive a deleted record."""
    rows = conn.execute(
        """
        SELECT g.id, g.name, g.group_set, g.smart, g.query,
               COUNT(m.rec_number) AS listed,
               SUM(CASE WHEN r.rec_number IS NULL THEN 0 ELSE 1 END) AS indexed
        FROM groups g
        LEFT JOIN group_members m ON m.group_id = g.id
        LEFT JOIN refs r ON r.rec_number = m.rec_number
        GROUP BY g.id
        ORDER BY g.group_set, g.name
        """
    ).fetchall()
    groups = []
    for row in rows:
        size = row["indexed"] or 0
        if not include_empty and size == 0:
            continue
        group = {
            "name": row["name"],
            "group_set": row["group_set"] or "",
            "smart": bool(row["smart"]),
            "size": size,
        }
        if row["smart"] and row["query"]:
            group["query"] = row["query"]
        if (row["listed"] or 0) != size:
            group["stale_members"] = (row["listed"] or 0) - size
        groups.append(group)
    return groups


def count_groups(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) AS c FROM groups").fetchone()["c"]
