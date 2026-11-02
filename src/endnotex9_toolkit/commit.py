"""The narrow, policy-gated commit surface exposed to MCP clients.

`writeback.py` proves a column is *mechanically* safe to rewrite (not
indexed, not tied back to a term list, not a sort key). This module adds the
*policy* half on top, for three purposes:

* filling in or correcting ordinary text fields — bibliographic fields like
  volume/issue/pages, the journal name, notes, the title, the abstract, and
  the DOI — that import left blank or wrong;
* correcting the author list and keyword list, the two fields that back
  EndNote's own Authors/Keywords term lists, verified safe by reading
  `terms.MYD` directly (see `writeback.py`'s module docstring for the
  byte-level read that established this);
* tagging records with a marker so the user can build an EndNote Smart
  Group and delete the matches themselves, inside EndNote.

`year` is deliberately absent: it is one of only two MyISAM keys on `refs`
(`writeback.py`'s docstring), so writing it needs real B-tree surgery this
package does not implement — a 2026-08 spike against the real `refs.MYI`
confirmed the key layout (two roots, one page each, accounting for all 19
index pages) but stopped short of the page-internal format and the
insert/delete/rebalance algorithm needed to touch it safely. Treat `year`,
and record deletion generally, as a separate, larger project.

Nothing here ever deletes a record or touches `custom_1`, which
`classification.py`/`writeplan.py` already own. Every write is backed up
first via `writeback.backup_library`, and every entry point defaults to
`dry_run=True`: the first call always returns a diff with nothing written,
and a caller has to pass `dry_run=False` to actually commit.
"""

from __future__ import annotations

from pathlib import Path

from . import libraryio, writeback
from .refstable import VALUE_SEPARATOR, read_rows

# Public field name -> internal EndNote column, for plain-string fields.
# Keeps MCP callers away from refs.frm column names, and keeps this list
# independent of WRITABLE_COLUMNS so a future writable column doesn't
# automatically become editable through this tool.
TEXT_FIELDS = {
    "volume": "volume",
    "issue": "number",
    "pages": "pages",
    "article_number": "section",
    "number_of_volumes": "number_of_volumes",
    "journal": "secondary_title",
    "notes": "notes",
    "title": "title",
    "abstract": "abstract",
    "doi": "electronic_resource_number",
}

# Same idea for fields EndNote stores as several values joined by
# refstable.VALUE_SEPARATOR ("\r"). Values here are lists of strings, not
# pre-joined text — this module owns the join/split.
LIST_FIELDS = {
    "author": "author",
    "keywords": "keywords",
}

# custom_1 is reserved for this server's own topic classification
# (classification.py / writeplan.py); marking must not collide with it.
MARKER_COLUMNS = frozenset({"custom_2", "custom_3", "custom_4", "custom_5", "custom_6", "custom_7"})


class CommitError(RuntimeError):
    pass


def _decode(value) -> str:
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", "replace")
    return "" if value is None else str(value)


def _decode_list(value) -> list[str]:
    return [part.strip() for part in _decode(value).split(VALUE_SEPARATOR) if part.strip()]


def _current_rows(rdb_dir: str | Path, rec_numbers, columns) -> dict[int, dict]:
    """Raw (undecoded) values for `columns` on each of `rec_numbers`."""
    wanted = set(rec_numbers)
    rows = {int(row["id"]): row for row in read_rows(rdb_dir) if int(row.get("id") or 0) in wanted}
    missing = sorted(wanted - set(rows))
    if missing:
        raise CommitError(f"no such records: {missing[:10]}")
    return {
        rec_number: {column: row.get(column) for column in columns}
        for rec_number, row in rows.items()
    }


@libraryio.library_operation
def update_reference_fields(
    rdb_dir: str | Path,
    enl_path: str | Path,
    updates: dict[int, dict[str, object]],
    dry_run: bool = True,
    expected_fingerprint: str | None = None,
) -> dict:
    """Fill in or correct text fields and/or the author and keyword lists.

    `updates` maps record number to a mapping keyed by the public names in
    `TEXT_FIELDS` (plain strings) and/or `LIST_FIELDS` (lists of strings,
    e.g. `{"author": ["Zerovnik, Andrej", "Dall'Olio, Stefano"]}` — this
    replaces the whole list, it does not append). Returns a diff of every
    requested field either way; only writes and backs up the library when
    `dry_run=False`, and only for fields that actually change.
    """
    if not updates:
        raise CommitError("updates must contain at least one record")
    fingerprint = libraryio.source_fingerprint(rdb_dir, enl_path)
    if expected_fingerprint is not None and fingerprint != expected_fingerprint:
        raise CommitError(
            "The library changed since the preview; generate and review a fresh preview"
        )

    all_fields = {f for fields in updates.values() for f in fields}
    bad_fields = sorted(all_fields - set(TEXT_FIELDS) - set(LIST_FIELDS))
    if bad_fields:
        raise CommitError(
            f"{bad_fields} are not fields this tool may write "
            f"({', '.join(sorted(set(TEXT_FIELDS) | set(LIST_FIELDS)))})"
        )
    for rec_number, fields in updates.items():
        for field in LIST_FIELDS:
            if field in fields and not isinstance(fields[field], list):
                raise CommitError(f"{field!r} on rec_number={rec_number} must be a list of strings")

    all_columns = TEXT_FIELDS | LIST_FIELDS
    rec_numbers = set(updates)
    columns = {all_columns[f] for fields in updates.values() for f in fields}
    before = _current_rows(rdb_dir, rec_numbers, columns)
    if libraryio.source_fingerprint(rdb_dir, enl_path) != fingerprint:
        raise CommitError("The library changed while reading; generate a fresh preview")

    diffs = []
    for rec_number, fields in updates.items():
        for field, new_value in fields.items():
            if field in LIST_FIELDS:
                column = LIST_FIELDS[field]
                old_value = _decode_list(before[rec_number][column])
                new_clean = [str(v).strip() for v in new_value if str(v).strip()]
            else:
                column = TEXT_FIELDS[field]
                old_value = _decode(before[rec_number][column])
                new_clean = str(new_value)
            diffs.append(
                {
                    "rec_number": rec_number,
                    "field": field,
                    "old_value": old_value,
                    "new_value": new_clean,
                }
            )
    changed = [d for d in diffs if d["old_value"] != d["new_value"]]

    if dry_run:
        return {
            "dry_run": True,
            "would_write": len(changed),
            "unchanged": len(diffs) - len(changed),
            "diffs": diffs,
            "source_fingerprint": fingerprint,
        }
    if not changed:
        return {"dry_run": False, "records_written": 0, "backup_path": None, "diffs": diffs}

    backup_path = writeback.backup_library(rdb_dir, enl_path)
    column_updates: dict[int, dict[str, str]] = {}
    for d in changed:
        field = d["field"]
        if field in LIST_FIELDS:
            column, text = LIST_FIELDS[field], VALUE_SEPARATOR.join(d["new_value"])
        else:
            column, text = TEXT_FIELDS[field], d["new_value"]
        column_updates.setdefault(d["rec_number"], {})[column] = text
    result = writeback.set_fields(rdb_dir, enl_path, column_updates)

    return {
        "dry_run": False,
        "records_written": result["records_written"],
        "backup_path": str(backup_path),
        "diffs": diffs,
    }


@libraryio.library_operation
def mark_records(
    rdb_dir: str | Path,
    enl_path: str | Path,
    rec_numbers: list[int],
    marker: str,
    column: str = "custom_7",
    force: bool = False,
    dry_run: bool = True,
    expected_fingerprint: str | None = None,
) -> dict:
    """Write `marker` into `column` for each of `rec_numbers`.

    Meant to feed an EndNote Smart Group (e.g. "Custom 7 contains
    <marker>") so the user reviews and deletes the matches inside EndNote
    itself — this never deletes anything. Refuses records whose `column`
    already holds different, non-empty text unless `force=True`. Only
    writes and backs up the library when `dry_run=False`, and only for
    records that do not already carry the marker.
    """
    if column not in MARKER_COLUMNS:
        raise CommitError(
            f"{column!r} may not be used for marking ({', '.join(sorted(MARKER_COLUMNS))} only); "
            "custom_1 is reserved for this server's topic classification"
        )
    if not marker.strip():
        raise CommitError("marker must be non-empty text")
    if not rec_numbers:
        raise CommitError("rec_numbers must contain at least one record")
    fingerprint = libraryio.source_fingerprint(rdb_dir, enl_path)
    if expected_fingerprint is not None and fingerprint != expected_fingerprint:
        raise CommitError(
            "The library changed since the preview; generate and review a fresh preview"
        )

    unique = sorted(set(rec_numbers))
    before = {
        rec: _decode(row[column]) for rec, row in _current_rows(rdb_dir, unique, {column}).items()
    }
    if libraryio.source_fingerprint(rdb_dir, enl_path) != fingerprint:
        raise CommitError("The library changed while reading; generate a fresh preview")
    conflicts = {rec: before[rec] for rec in unique if before[rec] and before[rec] != marker}
    if conflicts and not force:
        raise CommitError(
            f"{len(conflicts)} record(s) already hold different {column} text, e.g. "
            f"{dict(list(conflicts.items())[:5])}; pass force=True to overwrite"
        )

    to_write = [rec for rec in unique if before[rec] != marker]

    if dry_run:
        return {
            "dry_run": True,
            "would_mark": len(to_write),
            "already_marked": len(unique) - len(to_write),
            "conflicts": conflicts,
            "source_fingerprint": fingerprint,
        }
    if not to_write:
        return {"dry_run": False, "records_written": 0, "backup_path": None, "column": column}

    backup_path = writeback.backup_library(rdb_dir, enl_path)
    result = writeback.set_column(
        rdb_dir, enl_path, {rec: marker for rec in to_write}, column=column
    )

    return {
        "dry_run": False,
        "records_written": result["records_written"],
        "column": column,
        "marker": marker,
        "backup_path": str(backup_path),
    }


__all__ = [
    "LIST_FIELDS",
    "MARKER_COLUMNS",
    "TEXT_FIELDS",
    "CommitError",
    "mark_records",
    "update_reference_fields",
]
