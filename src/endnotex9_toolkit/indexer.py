"""Orchestrate parsing the XML export into the SQLite index.

Incremental by default: a record is only re-written if its content_hash
changed since the last run. `full=True` wipes and rebuilds from scratch
(useful after a schema change or to recover from a corrupted index).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import db as dbmod
from . import groups as groupsmod
from . import sources

# Commit every N writes so a long first run doesn't hold one giant
# transaction open, and a crash mid-run leaves usable partial progress.
_COMMIT_EVERY = 500


def build_index(
    xml_path: str | Path | None,
    conn: sqlite3.Connection,
    full: bool = False,
    library_path: str | Path | None = None,
) -> dict:
    """Index the library's references.

    Reads the EndNote library directly when `library_path` is given, falling
    back to the XML export — see sources.py for why that order.
    """
    source, records, note = sources.open_references(conn, xml_path, library_path)
    identity = sources.source_identity(xml_path, library_path)
    if identity:
        dbmod.set_meta(conn, "source_identity", identity)

    if full:
        conn.executescript(
            "DELETE FROM refs; DELETE FROM refs_fts;"
            "DELETE FROM pdf_pages; DELETE FROM pdf_docs; DELETE FROM pdf_fts;"
        )

    known_hashes = dbmod.content_hashes(conn)
    seen_now: set[int] = set()
    added = updated = unchanged = 0
    pending = 0

    for ref in records:
        rec_number = ref["rec_number"]
        seen_now.add(rec_number)
        existing_hash = known_hashes.get(rec_number)
        if existing_hash == ref["content_hash"]:
            unchanged += 1
            continue

        dbmod.upsert_reference(conn, ref)
        if existing_hash is None:
            added += 1
        else:
            updated += 1

        pending += 1
        if pending >= _COMMIT_EVERY:
            conn.commit()
            pending = 0

    removed_ids = set(known_hashes) - seen_now
    for rec_number in removed_ids:
        dbmod.delete_reference(conn, rec_number)

    dbmod.set_meta(
        conn, "last_indexed_at", datetime.now(timezone.utc).isoformat(timespec="seconds")
    )
    dbmod.set_meta(conn, "last_source", source)
    conn.commit()

    stats = {
        "source": source,
        "added": added,
        "updated": updated,
        "unchanged": unchanged,
        "removed": len(removed_ids),
        "total": dbmod.count_references(conn),
    }
    if note:
        stats["note"] = note
    return stats


def index_groups(conn: sqlite3.Connection, library_path: str | Path) -> dict:
    """Refresh the group tree from the EndNote library itself.

    Separate from `build_index` because the two read different sources: the
    references come from the XML export, the groups from the library's own
    tables (the export has no groups in it at all). Either can be stale
    without the other.
    """
    rdb = groupsmod.rdb_dir(library_path)
    found = groupsmod.read_groups(rdb)
    members = dbmod.replace_groups(conn, found)
    dbmod.set_meta(conn, "library_path", str(library_path))
    dbmod.set_meta(
        conn, "last_groups_read_at", datetime.now(timezone.utc).isoformat(timespec="seconds")
    )
    conn.commit()
    return {
        "groups": len(found),
        "smart_groups": sum(1 for g in found if g.smart),
        "memberships": members,
        "empty_groups": sum(1 for g in found if not g.rec_numbers),
    }
