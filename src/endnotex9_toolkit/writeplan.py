"""Prepare-only EndNote writeback plans.

This module is intentionally separated from :mod:`writeback`.  It reads
classification results and source files, records an immutable preview in
SQLite, and never writes, copies, replaces, or creates an EndNote file.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import db as dbmod
from . import sampling
from .classification import canonical_json, content_hash


class WritePlanError(RuntimeError):
    """Base class for fail-closed plan preparation errors."""


class IdempotencyConflict(WritePlanError):
    """An idempotency key was reused with a different canonical payload."""


class SourceFingerprintChanged(WritePlanError):
    """The EndNote source no longer matches a previously prepared plan."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Add plan tables without modifying the host or classification schema."""
    statements = (
        """CREATE TABLE IF NOT EXISTS writeback_plans(
          plan_id TEXT PRIMARY KEY,
          idempotency_key TEXT NOT NULL UNIQUE,
          session_id TEXT NOT NULL,
          status TEXT NOT NULL,
          payload_hash TEXT NOT NULL,
          source_fingerprint TEXT NOT NULL,
          backup_preview_path TEXT NOT NULL,
          field_name TEXT NOT NULL,
          diff_json TEXT NOT NULL,
          unchanged_count INTEGER NOT NULL,
          created_at TEXT NOT NULL,
          expired_at TEXT
        )""",
        """CREATE INDEX IF NOT EXISTS idx_writeback_plans_session
          ON writeback_plans(session_id, status)""",
        """CREATE TABLE IF NOT EXISTS writeback_plan_manifest(
          plan_id TEXT NOT NULL,
          source_name TEXT NOT NULL,
          source_path TEXT NOT NULL,
          sha256 TEXT NOT NULL,
          file_id INTEGER NOT NULL,
          size INTEGER NOT NULL,
          mtime_ns INTEGER NOT NULL,
          PRIMARY KEY(plan_id, source_name)
        )""",
    )
    with dbmod.connection_lock(conn):
        conn.execute("BEGIN IMMEDIATE")
        try:
            for statement in statements:
                conn.execute(statement)
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def _file_manifest(name: str, path: Path) -> dict:
    path = path.resolve()
    if not path.is_file():
        raise WritePlanError(f"required EndNote source is missing: {path}")
    before = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    after = path.stat()
    if (before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise SourceFingerprintChanged(f"source changed while hashing: {path}")
    return {
        "name": name,
        "path": str(path),
        "sha256": digest.hexdigest(),
        "file_id": after.st_ino,
        "size": after.st_size,
        "mtime_ns": after.st_mtime_ns,
    }


def source_manifest(rdb_dir: str | Path, enl_path: str | Path) -> list[dict]:
    rdb = Path(rdb_dir).resolve()
    sources = (
        ("enl", Path(enl_path)),
        ("refs.MYD", rdb / "refs.MYD"),
        ("refs.MYI", rdb / "refs.MYI"),
        ("misc.MYD", rdb / "misc.MYD"),
    )
    return [_file_manifest(name, path) for name, path in sources]


def _fingerprint(manifest: list[dict]) -> str:
    return hashlib.sha256(canonical_json(manifest).encode("utf-8")).hexdigest()


def _approved_facet(conn: sqlite3.Connection, session: sqlite3.Row) -> str:
    taxonomy_hash = session["taxonomy_hash"]
    if not taxonomy_hash:
        raise WritePlanError("session has no approved taxonomy")
    row = conn.execute(
        "SELECT status, body_json FROM taxonomies WHERE session_id=? AND taxonomy_hash=?",
        (session["session_id"], taxonomy_hash),
    ).fetchone()
    if row is None or row["status"] != "approved":
        raise WritePlanError("session taxonomy is not approved")
    try:
        taxonomy = json.loads(row["body_json"])
    except (TypeError, ValueError) as exc:
        raise WritePlanError("stored taxonomy is invalid JSON") from exc
    approved = [facet for facet in taxonomy.get("facets", []) if facet.get("approved")]
    if len(approved) != 1:
        raise WritePlanError("exactly one approved facet is required")
    facet = approved[0]
    if facet.get("slot", "custom_1") != "custom_1" or facet.get("mode", "single") != "single":
        raise WritePlanError("only one single facet mapped to custom_1 is supported")
    name = facet.get("id") or facet.get("name")
    if not isinstance(name, str) or not name.strip():
        raise WritePlanError("approved facet must have an id or name")
    return name


def _build_diff(
    conn: sqlite3.Connection, session: sqlite3.Row, facet: str
) -> tuple[list[dict], int]:
    session_id = session["session_id"]
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(assignments)")}
    evaluation_hash = None
    if "evaluation_hash" in columns:
        evaluation_hash = content_hash(
            {
                "session": session_id,
                "fingerprint": session["library_fingerprint"],
                "taxonomy": session["taxonomy_hash"],
                "ruleset": session["ruleset_hash"],
            }
        )
        where = "session_id=? AND evaluation_hash=?"
        params = (session_id, evaluation_hash)
    else:
        where = "session_id=?"
        params = (session_id,)
    assignments = conn.execute(
        """SELECT rec_number, facet, label, status
           FROM assignments WHERE """
        + where
        + " ORDER BY rec_number, facet",
        params,
    ).fetchall()
    if not assignments:
        raise WritePlanError("session has no assignments")
    bad = [
        row
        for row in assignments
        if row["facet"] != facet
        or row["status"] not in {"assigned", "approved"}
        or not isinstance(row["label"], str)
        or not row["label"].strip()
    ]
    if bad:
        raise WritePlanError("assignments contain conflicts, abstentions, or unsupported facets")
    if len({row["facet"] for row in assignments}) != 1:
        raise WritePlanError("multi-facet writeback is not supported")

    refs = {
        int(row["rec_number"]): row["custom_1"] or ""
        for row in conn.execute("SELECT rec_number, custom_1 FROM refs")
    }
    assigned_numbers = {int(row["rec_number"]) for row in assignments}
    missing_refs = sorted(assigned_numbers - refs.keys())
    if missing_refs:
        raise WritePlanError(f"assignments reference missing records: {missing_refs[:10]}")
    # Missing assignments are abstentions.  First-round writeback is atomic:
    # either every indexed record has a decision or no plan is prepared.
    unassigned = sorted(refs.keys() - assigned_numbers)
    if unassigned:
        raise WritePlanError(f"{len(unassigned)} records are unresolved or abstained")

    diffs: list[dict] = []
    unchanged = 0
    for row in assignments:
        rec_number = int(row["rec_number"])
        old = refs[rec_number]
        new = row["label"].strip()
        if old == new:
            unchanged += 1
            continue
        diffs.append(
            {
                "rec_number": rec_number,
                "field": "custom_1",
                "old_value": old,
                "new_value": new,
            }
        )
    return diffs, unchanged


def _stored_result(conn: sqlite3.Connection, row: sqlite3.Row, reused: bool) -> dict:
    manifest = [
        dict(item)
        for item in conn.execute(
            """SELECT source_name AS name, source_path AS path, sha256, file_id,
                      size, mtime_ns
               FROM writeback_plan_manifest WHERE plan_id=? ORDER BY source_name""",
            (row["plan_id"],),
        )
    ]
    diffs = json.loads(row["diff_json"])
    return {
        "plan_id": row["plan_id"],
        "session_id": row["session_id"],
        "idempotency_key": row["idempotency_key"],
        "status": row["status"],
        "reused": reused,
        "field": row["field_name"],
        "source_fingerprint": row["source_fingerprint"],
        "backup_preview_path": row["backup_preview_path"],
        "changes": len(diffs),
        "unchanged": row["unchanged_count"],
        "diffs": diffs,
        "manifest": manifest,
    }


def prepare_writeback(
    conn: sqlite3.Connection,
    session_id: str,
    rdb_dir: str | Path,
    enl_path: str | Path,
    idempotency_key: str,
) -> dict:
    """Persist and return a custom_1 writeback preview; never touch EndNote."""
    if not isinstance(idempotency_key, str) or not idempotency_key.strip():
        raise WritePlanError("idempotency_key is required")
    ensure_schema(conn)
    # Do not call classification.ensure_schema here: preparation must not
    # silently create a classification state that does not exist.
    if (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='classification_sessions'"
        ).fetchone()
        is None
    ):
        raise WritePlanError("classification schema is unavailable")
    session = conn.execute(
        "SELECT * FROM classification_sessions WHERE session_id=?", (session_id,)
    ).fetchone()
    if session is None:
        raise WritePlanError("unknown classification session")
    if session["status"] != "audited":
        raise WritePlanError("session must complete a labelled audit before preparation")
    if sampling.library_fingerprint(conn) != session["library_fingerprint"]:
        raise SourceFingerprintChanged(
            "indexed library changed since the classification session was created"
        )
    ruleset = conn.execute(
        "SELECT status FROM rulesets WHERE session_id=? AND ruleset_hash=?",
        (session_id, session["ruleset_hash"]),
    ).fetchone()
    if ruleset is None or ruleset["status"] != "frozen":
        raise WritePlanError("session ruleset is not frozen")
    completed_audit = conn.execute(
        """SELECT a.audit_set_id
           FROM audit_sets a
           WHERE a.session_id=?
             AND a.holdout=1
             AND a.library_fingerprint=?
             AND EXISTS (
               SELECT 1 FROM audit_items i
               WHERE i.audit_set_id=a.audit_set_id
             )
             AND NOT EXISTS (
               SELECT 1 FROM audit_items i
               WHERE i.audit_set_id=a.audit_set_id AND i.expected_label IS NULL
             )
           ORDER BY a.created_at DESC LIMIT 1""",
        (session_id, session["library_fingerprint"]),
    ).fetchone()
    if completed_audit is None:
        raise WritePlanError("session has no completed audit")

    facet = _approved_facet(conn, session)
    diffs, unchanged = _build_diff(conn, session, facet)
    manifest = source_manifest(rdb_dir, enl_path)
    source_fingerprint = _fingerprint(manifest)
    payload = {
        "session_id": session_id,
        "library_fingerprint": session["library_fingerprint"],
        "taxonomy_hash": session["taxonomy_hash"],
        "ruleset_hash": session["ruleset_hash"],
        "field": "custom_1",
        "diffs": diffs,
        "unchanged": unchanged,
        "manifest": manifest,
    }
    payload_hash = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()

    with dbmod.connection_lock(conn):
        conn.execute("BEGIN IMMEDIATE")
        try:
            current = conn.execute(
                """SELECT library_fingerprint, taxonomy_hash, ruleset_hash, status
                   FROM classification_sessions WHERE session_id=?""",
                (session_id,),
            ).fetchone()
            expected_state = (
                session["library_fingerprint"],
                session["taxonomy_hash"],
                session["ruleset_hash"],
                session["status"],
            )
            if current is None or tuple(current) != expected_state:
                raise WritePlanError("classification session changed during preparation")
            existing = conn.execute(
                "SELECT * FROM writeback_plans WHERE idempotency_key=?", (idempotency_key,)
            ).fetchone()
            if existing is not None:
                if existing["source_fingerprint"] != source_fingerprint:
                    conn.execute(
                        "UPDATE writeback_plans SET status='expired', expired_at=? WHERE plan_id=?",
                        (_now(), existing["plan_id"]),
                    )
                    conn.commit()
                    raise SourceFingerprintChanged(
                        "EndNote source fingerprint changed; the existing plan was expired"
                    )
                if existing["payload_hash"] != payload_hash:
                    raise IdempotencyConflict(
                        "idempotency key already belongs to a different payload"
                    )
                conn.commit()
                return _stored_result(conn, existing, reused=True)

            # Expire other prepared plans for this session when their source
            # snapshot differs. A new idempotency key can then create a fresh one.
            conn.execute(
                """UPDATE writeback_plans SET status='expired', expired_at=?
               WHERE session_id=? AND status='prepared' AND source_fingerprint<>?""",
                (_now(), session_id, source_fingerprint),
            )
            plan_id = str(uuid.uuid4())
            backup_preview = str(
                Path(enl_path).resolve().parent / "backups" / f"{Path(enl_path).stem}-{plan_id}"
            )
            created_at = _now()
            conn.execute(
                """INSERT INTO writeback_plans(
                 plan_id,idempotency_key,session_id,status,payload_hash,
                 source_fingerprint,backup_preview_path,field_name,diff_json,
                 unchanged_count,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    plan_id,
                    idempotency_key,
                    session_id,
                    "prepared",
                    payload_hash,
                    source_fingerprint,
                    backup_preview,
                    "custom_1",
                    canonical_json(diffs),
                    unchanged,
                    created_at,
                ),
            )
            conn.executemany(
                """INSERT INTO writeback_plan_manifest(
                 plan_id,source_name,source_path,sha256,file_id,size,mtime_ns)
               VALUES(?,?,?,?,?,?,?)""",
                [
                    (
                        plan_id,
                        item["name"],
                        item["path"],
                        item["sha256"],
                        item["file_id"],
                        item["size"],
                        item["mtime_ns"],
                    )
                    for item in manifest
                ],
            )
            conn.commit()
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
    row = conn.execute("SELECT * FROM writeback_plans WHERE plan_id=?", (plan_id,)).fetchone()
    return _stored_result(conn, row, reused=False)


__all__ = [
    "IdempotencyConflict",
    "SourceFingerprintChanged",
    "WritePlanError",
    "ensure_schema",
    "prepare_writeback",
    "source_manifest",
]
