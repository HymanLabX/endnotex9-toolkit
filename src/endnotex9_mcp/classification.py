"""Versioned, deterministic classification sessions.

This module deliberately keeps classification state outside EndNote's tables.
Rules are declarative JSON and are evaluated by a small, bounded interpreter.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import sqlite3
import unicodedata
import uuid
from datetime import datetime, timezone
from typing import Any

from . import db as dbmod

CLASSIFICATION_DB_VERSION = 5

# The one host index layout this module knows how to extend. Deliberately NOT
# `db.SCHEMA_VERSION`: tracking that constant would make a future host schema
# silently acceptable, and the whole point of the check is to fail closed on a
# layout we have not read. `test_classification.py` asserts the two are equal,
# so bumping `db.SCHEMA_VERSION` breaks the suite — which is the intended
# prompt to come back here and decide whether v5 is really compatible.
SUPPORTED_HOST_SCHEMA = 4
_MAX_DEPTH, _MAX_STRING, _MAX_OPS, _MAX_OUTPUT = 8, 4096, 1000, 10000
_RULE_FIELDS = {
    "title",
    "title_short",
    "abstract",
    "keywords",
    "authors",
    "editors",
    "container_title",
    "collection_title",
    "year",
    "publisher",
    "language",
    "ref_type",
}


def _lock(conn: sqlite3.Connection):
    return dbmod.connection_lock(conn)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _nfc(value: Any, depth: int = 0) -> Any:
    if depth > _MAX_DEPTH:
        raise ValueError("JSON exceeds maximum nesting depth")
    if isinstance(value, str):
        if len(value) > _MAX_STRING:
            raise ValueError("string exceeds maximum length")
        return unicodedata.normalize("NFC", value)
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("NaN and Infinity are not allowed")
    if isinstance(value, dict):
        return {unicodedata.normalize("NFC", str(k)): _nfc(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        if len(value) > _MAX_OUTPUT:
            raise ValueError("array exceeds maximum length")
        return [_nfc(v, depth + 1) for v in value]
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(
        _nfc(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Add classification tables without touching refs/groups/FTS tables."""
    with _lock(conn):
        conn.execute("BEGIN IMMEDIATE")
        try:
            # We only know how to extend one host layout; unknown/partial
            # layouts fail closed rather than risking a destructive rebuild.
            if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'"
            ).fetchone():
                m = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
                if m is not None and str(m[0]) != str(SUPPORTED_HOST_SCHEMA):
                    raise RuntimeError(
                        f"unsupported host schema version {m[0]!r}; "
                        f"this classifier extends v{SUPPORTED_HOST_SCHEMA}"
                    )
            uv = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if uv > CLASSIFICATION_DB_VERSION:
                raise RuntimeError("database schema is newer than this classifier")
            conn.execute("""CREATE TABLE IF NOT EXISTS migration_history(
                version INTEGER PRIMARY KEY, state TEXT NOT NULL, migration_hash TEXT NOT NULL,
                started_at TEXT NOT NULL, applied_at TEXT)""")
            statements = [
                """CREATE TABLE IF NOT EXISTS classification_sessions(
                  session_id TEXT PRIMARY KEY, library_fingerprint TEXT NOT NULL,
                  seed INTEGER, budgets_json TEXT, status TEXT NOT NULL,
                  taxonomy_hash TEXT, ruleset_hash TEXT, parent_hash TEXT,
                  created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
                """CREATE TABLE IF NOT EXISTS taxonomies(
                  taxonomy_hash TEXT NOT NULL, session_id TEXT NOT NULL,
                  version INTEGER NOT NULL, parent_hash TEXT, status TEXT NOT NULL,
                  body_json TEXT NOT NULL, created_at TEXT NOT NULL,
                  PRIMARY KEY(session_id,taxonomy_hash), UNIQUE(session_id,version))""",
                """CREATE TABLE IF NOT EXISTS rulesets(
                  ruleset_hash TEXT NOT NULL, session_id TEXT NOT NULL,
                  version INTEGER NOT NULL, parent_hash TEXT, status TEXT NOT NULL,
                  body_json TEXT NOT NULL, created_at TEXT NOT NULL,
                  PRIMARY KEY(session_id,ruleset_hash), UNIQUE(session_id,version))""",
                """CREATE TABLE IF NOT EXISTS assignments(
                  session_id TEXT NOT NULL, evaluation_hash TEXT NOT NULL,
                  rec_number INTEGER NOT NULL, facet TEXT NOT NULL, label TEXT,
                  status TEXT NOT NULL, confidence REAL, evidence_json TEXT, rule_id TEXT,
                  taxonomy_hash TEXT, ruleset_hash TEXT, source TEXT, created_at TEXT NOT NULL,
                  PRIMARY KEY(session_id,evaluation_hash,rec_number,facet))""",
                """CREATE TABLE IF NOT EXISTS audit_sets(
                  audit_set_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                  library_fingerprint TEXT NOT NULL, seed INTEGER NOT NULL,
                  holdout INTEGER NOT NULL, sample_json TEXT NOT NULL, created_at TEXT NOT NULL)""",
                """CREATE TABLE IF NOT EXISTS audit_items(
                  audit_set_id TEXT NOT NULL, session_id TEXT NOT NULL,
                  rec_number INTEGER NOT NULL, facet TEXT NOT NULL,
                  assigned_label TEXT, assigned_status TEXT NOT NULL,
                  expected_label TEXT, PRIMARY KEY(audit_set_id,rec_number,facet),
                  UNIQUE(session_id,rec_number))""",
            ]
            migration_hash = hashlib.sha256("\n".join(statements).encode()).hexdigest()
            row = conn.execute(
                "SELECT state,migration_hash FROM migration_history WHERE version=?",
                (CLASSIFICATION_DB_VERSION,),
            ).fetchone()
            if row and row[0] != "complete":
                raise RuntimeError("classification migration is incomplete")
            if row and row[1] != migration_hash:
                raise RuntimeError("classification migration hash mismatch")
            if not row:
                conn.execute(
                    """INSERT INTO migration_history
                    (version,state,migration_hash,started_at) VALUES(?,?,?,?)""",
                    (CLASSIFICATION_DB_VERSION, "started", migration_hash, _now()),
                )
                for statement in statements:
                    conn.execute(statement)
            expected_tables = {
                "classification_sessions",
                "taxonomies",
                "rulesets",
                "assignments",
                "audit_sets",
                "audit_items",
            }
            placeholders = ",".join("?" for _ in expected_tables)
            actual = {
                r[0]
                for r in conn.execute(
                    f"SELECT name FROM sqlite_master "
                    f"WHERE type='table' AND name IN ({placeholders})",
                    tuple(expected_tables),
                )
            }
            if actual != expected_tables:
                raise RuntimeError("classification migration is partial")
            conn.execute(f"PRAGMA user_version = {CLASSIFICATION_DB_VERSION}")
            conn.execute(
                "UPDATE migration_history SET state='complete', applied_at=? WHERE version=?",
                (_now(), CLASSIFICATION_DB_VERSION),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise


# Explicit alias used by callers that treat this as a migration step.
migrate_schema = ensure_schema


def _session(conn: sqlite3.Connection, session_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM classification_sessions WHERE session_id=?", (session_id,)
    ).fetchone()
    if row is None:
        raise KeyError("unknown classification session")
    return row


def get_session(conn: sqlite3.Connection, session_id: str) -> dict:
    """Return one classification session without exposing a mutable row."""
    ensure_schema(conn)
    return dict(_session(conn, session_id))


def create_session(
    conn: sqlite3.Connection,
    library_fingerprint: str | None,
    seed: int | None = None,
    budgets: dict | None = None,
) -> dict:
    ensure_schema(conn)
    sid, now = str(uuid.uuid4()), _now()
    with _lock(conn):
        conn.execute("BEGIN IMMEDIATE")
        try:
            if library_fingerprint is None:
                # Local import avoids a module cycle: sampling reuses the
                # canonical JSON helper from this module.
                from . import sampling as samplingmod

                library_fingerprint = samplingmod.library_fingerprint(conn)
            conn.execute(
                """INSERT INTO classification_sessions
                (session_id,library_fingerprint,seed,budgets_json,status,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?)""",
                (sid, library_fingerprint, seed, canonical_json(budgets or {}), "draft", now, now),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return dict(_session(conn, sid))


def _validate_taxonomy(taxonomy: dict) -> None:
    if not isinstance(taxonomy, dict) or not isinstance(taxonomy.get("facets"), list):
        raise ValueError("taxonomy must contain facets")
    if not taxonomy["facets"]:
        raise ValueError("taxonomy must contain at least one facet")
    seen: set[str] = set()
    for facet in taxonomy["facets"]:
        if not isinstance(facet, dict):
            raise ValueError("each taxonomy facet must be an object")
        facet_id = facet.get("id") or facet.get("name")
        if not isinstance(facet_id, str) or not facet_id.strip():
            raise ValueError("each taxonomy facet requires an id or name")
        if facet_id in seen:
            raise ValueError(f"duplicate taxonomy facet: {facet_id}")
        seen.add(facet_id)
        if facet.get("mode", "single") not in {"single", "multiple"}:
            raise ValueError("facet mode must be single or multiple")
        labels = facet.get("labels")
        if labels is not None and (
            not isinstance(labels, list)
            or any(not isinstance(label, (str, dict)) for label in labels)
        ):
            raise ValueError("facet labels must be a list of strings or objects")
        normalized = _declared_labels(facet)
        if labels is not None and len(normalized) != len(labels):
            raise ValueError("facet labels need unique non-empty ids or names")
        if facet.get("approved") and not normalized:
            raise ValueError("an approved facet requires declared labels")
    approved = [f for f in taxonomy["facets"] if f.get("approved")]
    if approved and (
        len(approved) != 1
        or approved[0].get("slot", "custom_1") != "custom_1"
        or approved[0].get("mode", "single") != "single"
    ):
        raise ValueError("first round supports one approved single facet mapped to custom_1")


def _declared_labels(facet: dict) -> set[str]:
    labels = facet.get("labels")
    if not isinstance(labels, list):
        return set()
    normalized: set[str] = set()
    for item in labels:
        value = (
            item
            if isinstance(item, str)
            else item.get("id") or item.get("name") or item.get("label")
        )
        if isinstance(value, str) and value.strip():
            normalized.add(value.strip())
    return normalized


def _save_version(
    conn: sqlite3.Connection,
    table: str,
    session_id: str,
    body: dict,
    expected_status: str | None,
    status: str,
    expected_parent_hash: str | None = None,
) -> dict:
    ensure_schema(conn)
    body_text = canonical_json(body)
    h = content_hash(body)
    with _lock(conn):
        conn.execute("BEGIN IMMEDIATE")
        try:
            s = _session(conn, session_id)
            if expected_status is not None and s["status"] != expected_status:
                raise RuntimeError("session status changed")
            n = conn.execute(
                f"SELECT COALESCE(MAX(version),0)+1 FROM {table} WHERE session_id=?", (session_id,)
            ).fetchone()[0]
            parent = s["taxonomy_hash"] if table == "taxonomies" else s["ruleset_hash"]
            if expected_parent_hash is not None and expected_parent_hash != parent:
                raise RuntimeError("parent hash changed")
            hash_col = "taxonomy_hash" if table == "taxonomies" else "ruleset_hash"
            conn.execute(
                f"""INSERT INTO {table}( {hash_col},session_id,version,parent_hash,status,body_json,created_at)
                            VALUES(?,?,?,?,?,?,?)""",
                (h, session_id, n, parent, "draft", body_text, _now()),
            )
            col = "taxonomy_hash" if table == "taxonomies" else "ruleset_hash"
            cur = conn.execute(
                f"UPDATE classification_sessions SET {col}=?,status=?,updated_at=? WHERE session_id=? AND status=?",
                (h, status, _now(), session_id, s["status"]),
            )
            if cur.rowcount != 1:
                raise RuntimeError("concurrent session update")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return {"hash": h, "version": n, "parent_hash": parent, "status": status}


def save_taxonomy(
    conn: sqlite3.Connection,
    session_id: str,
    taxonomy: dict,
    expected_status: str | None = "draft",
    parent_hash: str | None = None,
) -> dict:
    _validate_taxonomy(taxonomy)
    return _save_version(
        conn, "taxonomies", session_id, taxonomy, expected_status, "taxonomy_saved", parent_hash
    )


def approve_taxonomy(
    conn: sqlite3.Connection,
    session_id: str,
    taxonomy_hash: str,
    expected_status: str = "taxonomy_saved",
) -> dict:
    ensure_schema(conn)
    with _lock(conn):
        conn.execute("BEGIN IMMEDIATE")
        try:
            s = _session(conn, session_id)
            if s["status"] != expected_status or s["taxonomy_hash"] != taxonomy_hash:
                raise RuntimeError("taxonomy approval precondition failed")
            body = json.loads(
                conn.execute(
                    "SELECT body_json FROM taxonomies WHERE session_id=? AND taxonomy_hash=?",
                    (session_id, taxonomy_hash),
                ).fetchone()[0]
            )
            _validate_taxonomy(body)
            if len([f for f in body["facets"] if f.get("approved")]) != 1:
                raise ValueError("exactly one facet must be approved")
            cur = conn.execute(
                "UPDATE taxonomies SET status='approved' WHERE session_id=? AND taxonomy_hash=? AND status='draft'",
                (session_id, taxonomy_hash),
            )
            if cur.rowcount != 1:
                raise RuntimeError("taxonomy is not an approvable draft")
            cur = conn.execute(
                "UPDATE classification_sessions SET status='taxonomy_approved',updated_at=? WHERE session_id=? AND status=?",
                (_now(), session_id, expected_status),
            )
            if cur.rowcount != 1:
                raise RuntimeError("concurrent session update")
            conn.commit()
            return {"hash": taxonomy_hash, "status": "approved"}
        except Exception:
            conn.rollback()
            raise


def _validate_rule(rule: dict, depth: int = 0, counter: list[int] | None = None) -> None:
    counter = counter if counter is not None else [0]
    counter[0] += 1
    if counter[0] > _MAX_OPS:
        raise ValueError("ruleset exceeds operation limit")
    if depth > _MAX_DEPTH or not isinstance(rule, dict):
        raise ValueError("invalid rule")
    op = rule.get("op")
    if op not in {"contains_any", "contains_all", "prefix", "year_range", "token"}:
        if "when" in rule:
            if any(
                not isinstance(rule.get(k), str) or not rule.get(k).strip()
                for k in ("id", "facet", "label")
            ):
                raise ValueError("rule requires id, facet, and label")
            unknown = set(rule) - {"id", "facet", "label", "when", "priority"}
            if unknown:
                raise ValueError(f"unknown rule keys: {sorted(unknown)}")
            if not isinstance(rule["when"], list) or not rule["when"]:
                raise ValueError("rule when must be a non-empty list")
            priority = rule.get("priority", 0)
            if (
                isinstance(priority, bool)
                or not isinstance(priority, int)
                or not -100000 <= priority <= 100000
            ):
                raise ValueError("rule priority must be a bounded integer")
            for c in rule["when"]:
                _validate_rule(c, depth + 1, counter)
            return
        raise ValueError("unsupported rule operation")
    fields = rule.get("field", "title")
    fields = fields if isinstance(fields, list) else [fields]
    if not fields or any(f not in _RULE_FIELDS for f in fields):
        raise ValueError("rule field is not allowed")
    if op == "year_range" and fields != ["year"]:
        raise ValueError("year_range only supports the year field")
    values = rule.get("values", [])
    if op != "year_range" and (
        not isinstance(values, list)
        or not values
        or any(not isinstance(value, str) or not value.strip() for value in values)
    ):
        raise ValueError("rule values must be a non-empty list")
    if len(canonical_json(rule)) > _MAX_STRING:
        raise ValueError("rule too large")
    if op == "year_range":
        lo, hi = rule.get("min"), rule.get("max")
        if set(rule) - {"op", "field", "min", "max"}:
            raise ValueError("unknown year_range rule keys")
        for bound in (lo, hi):
            if bound is not None and (isinstance(bound, bool) or not isinstance(bound, int)):
                raise ValueError("year_range bounds must be integers or null")
        if lo is not None and hi is not None and lo > hi:
            raise ValueError("year_range min cannot exceed max")
    elif set(rule) - {"op", "field", "values"}:
        raise ValueError("unknown predicate rule keys")


def _validate_ruleset(ruleset: dict) -> None:
    rules = ruleset.get("rules") if isinstance(ruleset, dict) else None
    if not isinstance(rules, list) or len(rules) > _MAX_OPS:
        raise ValueError("ruleset must contain bounded rules")
    counter = [0]
    for r in rules:
        _validate_rule(r, counter=counter)


def save_ruleset(
    conn: sqlite3.Connection,
    session_id: str,
    ruleset: dict,
    expected_status: str | None = "taxonomy_approved",
    parent_hash: str | None = None,
) -> dict:
    _validate_ruleset(ruleset)
    session = _session(conn, session_id)
    taxonomy_row = conn.execute(
        "SELECT body_json FROM taxonomies WHERE session_id=? AND taxonomy_hash=?",
        (session_id, session["taxonomy_hash"]),
    ).fetchone()
    if taxonomy_row is None:
        raise RuntimeError("approved taxonomy is unavailable")
    taxonomy = json.loads(taxonomy_row["body_json"])
    approved = [facet for facet in taxonomy["facets"] if facet.get("approved")]
    if len(approved) != 1:
        raise RuntimeError("exactly one approved facet is required")
    facet = approved[0]
    facet_id = facet.get("id") or facet.get("name")
    declared = _declared_labels(facet)
    for rule in ruleset["rules"]:
        if rule.get("facet") != facet_id:
            raise ValueError("rules must target the approved facet")
        if rule.get("label") not in declared:
            raise ValueError("rule label is not declared by the approved taxonomy")
    return _save_version(
        conn, "rulesets", session_id, ruleset, expected_status, "ruleset_saved", parent_hash
    )


def freeze_ruleset(
    conn: sqlite3.Connection,
    session_id: str,
    ruleset_hash: str,
    expected_status: str = "ruleset_saved",
) -> dict:
    ensure_schema(conn)
    with _lock(conn):
        conn.execute("BEGIN IMMEDIATE")
        try:
            s = _session(conn, session_id)
            if s["status"] != expected_status or s["ruleset_hash"] != ruleset_hash:
                raise RuntimeError("ruleset freeze precondition failed")
            cur = conn.execute(
                "UPDATE rulesets SET status='frozen' WHERE session_id=? AND ruleset_hash=? AND status='draft'",
                (session_id, ruleset_hash),
            )
            if cur.rowcount != 1:
                raise RuntimeError("ruleset is not a freezable draft")
            cur = conn.execute(
                "UPDATE classification_sessions SET status='ruleset_frozen',updated_at=? WHERE session_id=? AND status=?",
                (_now(), session_id, expected_status),
            )
            if cur.rowcount != 1:
                raise RuntimeError("concurrent session update")
            conn.commit()
            return {"hash": ruleset_hash, "status": "frozen"}
        except Exception:
            conn.rollback()
            raise


def _field(ref: dict, name: str) -> list[str]:
    if isinstance(name, list):
        return [v for field in name for v in _field(ref, field)]
    v = ref.get(name, "")
    if name in {"authors", "keywords"} and isinstance(v, str):
        try:
            decoded = json.loads(v)
        except (TypeError, ValueError):
            decoded = None
        if isinstance(decoded, list):
            v = decoded
    return [str(x) for x in v] if isinstance(v, list) else [str(v or "")]


def _match(c: dict, ref: dict) -> bool:
    op, vals = c.get("op"), c.get("values", [])
    field = c.get("field", "year" if op == "year_range" else "title")
    text = _field(ref, field)
    joined = " ".join(text).casefold()
    vals = [str(v).casefold() for v in vals]
    if op == "contains_any":
        return any(v in joined for v in vals)
    if op == "contains_all":
        return all(v in joined for v in vals)
    if op == "prefix":
        return any(x.casefold().startswith(v) for x in text for v in vals)
    if op == "token":
        return any(v in joined.split() for v in vals)
    if op == "year_range":
        try:
            y = int(str(ref.get(field or "year", ""))[:4])
        except Exception:
            return False
        return (c.get("min") is None or y >= int(c["min"])) and (
            c.get("max") is None or y <= int(c["max"])
        )
    return False


def evaluate_rules(
    conn: sqlite3.Connection, session_id: str, library_fingerprint: str | None = None
) -> dict:
    ensure_schema(conn)
    with _lock(conn):
        conn.execute("BEGIN IMMEDIATE")
        try:
            s = _session(conn, session_id)
            if s["status"] not in ("ruleset_frozen", "evaluated"):
                raise RuntimeError("ruleset must be frozen before evaluation")
            if library_fingerprint is not None and library_fingerprint != s["library_fingerprint"]:
                raise RuntimeError("library fingerprint mismatch")
            from . import sampling as samplingmod

            current_fingerprint = samplingmod.library_fingerprint(conn)
            if current_fingerprint != s["library_fingerprint"]:
                raise RuntimeError("library changed since the session snapshot")
            taxrow = conn.execute(
                "SELECT body_json,status FROM taxonomies WHERE session_id=? AND taxonomy_hash=?",
                (session_id, s["taxonomy_hash"]),
            ).fetchone()
            rulerow = conn.execute(
                "SELECT body_json,status FROM rulesets WHERE session_id=? AND ruleset_hash=?",
                (session_id, s["ruleset_hash"]),
            ).fetchone()
            if (
                not taxrow
                or taxrow["status"] != "approved"
                or not rulerow
                or rulerow["status"] != "frozen"
            ):
                raise RuntimeError("approved taxonomy and frozen ruleset are required")
            tax, ruleset = json.loads(taxrow["body_json"]), json.loads(rulerow["body_json"])
            approved = [f for f in tax["facets"] if f.get("approved")]
            if (
                len(approved) != 1
                or approved[0].get("slot", "custom_1") != "custom_1"
                or approved[0].get("mode", "single") != "single"
            ):
                raise RuntimeError("only one approved custom_1 single facet is supported")
            facet = approved[0].get("id") or approved[0].get("name")
            declared_labels = _declared_labels(approved[0])
            if any(
                rule.get("facet") != facet or rule.get("label") not in declared_labels
                for rule in ruleset["rules"]
            ):
                raise RuntimeError("frozen ruleset violates the approved taxonomy")
            evaluation_hash = content_hash(
                {
                    "session": session_id,
                    "fingerprint": s["library_fingerprint"],
                    "taxonomy": s["taxonomy_hash"],
                    "ruleset": s["ruleset_hash"],
                }
            )
            existing = conn.execute(
                """SELECT status,COUNT(*) c FROM assignments
                WHERE session_id=? AND evaluation_hash=? GROUP BY status""",
                (session_id, evaluation_hash),
            ).fetchall()
            if existing:
                conn.commit()
                out = {
                    "assigned": 0,
                    "unresolved": 0,
                    "conflict": 0,
                    "evaluation_hash": evaluation_hash,
                }
                for r in existing:
                    out[r["status"]] = r["c"]
                return out
            rows = conn.execute("SELECT * FROM refs").fetchall()
            counts = {"assigned": 0, "unresolved": 0, "conflict": 0}
            for row in rows:
                ref = dict(row)
                hits = [
                    rule
                    for rule in ruleset["rules"]
                    if rule.get("facet", facet) == facet
                    and all(_match(condition, ref) for condition in rule.get("when", [rule]))
                ]
                if hits:
                    highest = max(int(rule.get("priority", 0)) for rule in hits)
                    decisive = [rule for rule in hits if int(rule.get("priority", 0)) == highest]
                else:
                    decisive = []
                labels = {rule.get("label", "") for rule in decisive if rule.get("label")}
                evidence = {
                    "matched_rules": [
                        {
                            "id": rule.get("id"),
                            "label": rule.get("label"),
                            "priority": int(rule.get("priority", 0)),
                            "fields": sorted(
                                {
                                    field
                                    for condition in rule.get("when", [rule])
                                    for field in (
                                        condition.get("field")
                                        if isinstance(condition.get("field"), list)
                                        else [condition.get("field", "title")]
                                    )
                                }
                            ),
                        }
                        for rule in hits
                    ],
                    "decisive_priority": (
                        max(int(rule.get("priority", 0)) for rule in decisive) if decisive else None
                    ),
                    "labels": sorted(labels),
                }
                if len(labels) != 1:
                    status = "conflict" if len(labels) > 1 else "unresolved"
                    counts[status] += 1
                    conn.execute(
                        """INSERT INTO assignments(session_id,evaluation_hash,rec_number,facet,label,status,
                        evidence_json,taxonomy_hash,ruleset_hash,source,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            session_id,
                            evaluation_hash,
                            row["rec_number"],
                            facet,
                            None,
                            status,
                            canonical_json(evidence),
                            s["taxonomy_hash"],
                            s["ruleset_hash"],
                            "rules",
                            _now(),
                        ),
                    )
                    continue
                label = next(iter(labels))
                selected_rules = [rule for rule in decisive if rule.get("label") == label]
                selected = selected_rules[0]
                conn.execute(
                    """INSERT INTO assignments(session_id,evaluation_hash,rec_number,facet,label,status,confidence,evidence_json,rule_id,taxonomy_hash,ruleset_hash,source,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        session_id,
                        evaluation_hash,
                        row["rec_number"],
                        facet,
                        label,
                        "assigned",
                        1.0,
                        canonical_json(evidence),
                        selected.get("id"),
                        s["taxonomy_hash"],
                        s["ruleset_hash"],
                        "rules",
                        _now(),
                    ),
                )
                counts["assigned"] += 1
            cur = conn.execute(
                "UPDATE classification_sessions SET status='evaluated',updated_at=? WHERE session_id=? AND status IN ('ruleset_frozen','evaluated')",
                (_now(), session_id),
            )
            if cur.rowcount != 1:
                raise RuntimeError("concurrent session update")
            conn.commit()
            counts["evaluation_hash"] = evaluation_hash
            return counts
        except Exception:
            conn.rollback()
            raise


def list_unresolved(conn: sqlite3.Connection, session_id: str) -> list[dict]:
    ensure_schema(conn)
    s = _session(conn, session_id)
    return [
        dict(r)
        for r in conn.execute(
            """SELECT r.rec_number,r.title,r.abstract,a.status,a.evidence_json
        FROM assignments a JOIN refs r ON r.rec_number=a.rec_number
        WHERE a.session_id=? AND a.taxonomy_hash=? AND a.ruleset_hash=? AND a.status!='assigned'
        ORDER BY r.rec_number""",
            (session_id, s["taxonomy_hash"], s["ruleset_hash"]),
        )
    ]


def create_audit_sample(
    conn: sqlite3.Connection,
    session_id: str,
    size: int = 50,
    seed: int | None = None,
    holdout: bool = True,
) -> dict:
    ensure_schema(conn)
    if not 1 <= size <= 500:
        raise ValueError("audit size must be between 1 and 500")
    aid = str(uuid.uuid4())
    with _lock(conn):
        conn.execute("BEGIN IMMEDIATE")
        try:
            s = _session(conn, session_id)
            if s["status"] not in ("evaluated", "auditing", "audited"):
                raise RuntimeError("session is not ready for audit")
            used = {
                row[0]
                for row in conn.execute(
                    "SELECT rec_number FROM audit_items WHERE session_id=?", (session_id,)
                )
            }
            candidates = conn.execute(
                """SELECT rec_number, label, status FROM assignments
                   WHERE session_id=? AND taxonomy_hash=? AND ruleset_hash=?
                   ORDER BY rec_number""",
                (session_id, s["taxonomy_hash"], s["ruleset_hash"]),
            ).fetchall()
            chosen_seed = seed if seed is not None else (s["seed"] or 0)
            rng = random.Random(chosen_seed)
            buckets: dict[str, list[int]] = {}
            for candidate in candidates:
                if candidate["rec_number"] in used:
                    continue
                key = (
                    f"label:{candidate['label']}"
                    if candidate["status"] == "assigned"
                    else f"status:{candidate['status']}"
                )
                buckets.setdefault(key, []).append(candidate["rec_number"])
            for numbers in buckets.values():
                rng.shuffle(numbers)
            nums: list[int] = []
            keys = sorted(buckets)
            while keys and len(nums) < max(0, size):
                remaining = []
                for key in keys:
                    if buckets[key] and len(nums) < max(0, size):
                        nums.append(buckets[key].pop())
                    if buckets[key]:
                        remaining.append(key)
                keys = remaining
            if not nums:
                raise RuntimeError("no unaudited assignments are available")
            conn.execute(
                "INSERT INTO audit_sets VALUES(?,?,?,?,?,?,?)",
                (
                    aid,
                    session_id,
                    s["library_fingerprint"],
                    chosen_seed,
                    int(holdout),
                    canonical_json(nums),
                    _now(),
                ),
            )
            for n in nums:
                row = conn.execute(
                    """SELECT facet,label,status FROM assignments WHERE session_id=? AND rec_number=?
                    AND taxonomy_hash=? AND ruleset_hash=? LIMIT 1""",
                    (session_id, n, s["taxonomy_hash"], s["ruleset_hash"]),
                ).fetchone()
                facet = row["facet"] if row else "custom_1"
                conn.execute(
                    "INSERT INTO audit_items VALUES(?,?,?,?,?,?,?)",
                    (
                        aid,
                        session_id,
                        n,
                        facet,
                        row["label"] if row else None,
                        row["status"] if row else "abstain",
                        None,
                    ),
                )
            cur = conn.execute(
                "UPDATE classification_sessions SET status='auditing',updated_at=? WHERE session_id=? AND status IN ('evaluated','auditing','audited')",
                (_now(), session_id),
            )
            if cur.rowcount != 1:
                raise RuntimeError("concurrent session update")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return {"audit_set_id": aid, "size": len(nums), "rec_numbers": nums}


def submit_audit_labels(conn: sqlite3.Connection, audit_set_id: str, labels: list[dict]) -> int:
    ensure_schema(conn)
    if not isinstance(labels, list) or not labels:
        raise ValueError("labels must be a non-empty list")
    seen: set[tuple[int, str | None]] = set()
    for item in labels:
        if not isinstance(item, dict):
            raise ValueError("each audit label must be an object")
        if isinstance(item.get("rec_number"), bool) or not isinstance(item.get("rec_number"), int):
            raise ValueError("audit rec_number must be an integer")
        if not isinstance(item.get("label"), str) or not item["label"].strip():
            raise ValueError("audit label must be a non-empty string")
        key = (item["rec_number"], item.get("facet"))
        if key in seen:
            raise ValueError("duplicate audit label in one submission")
        seen.add(key)
    with _lock(conn):
        conn.execute("BEGIN IMMEDIATE")
        try:
            for x in labels:
                if "facet" in x:
                    cur = conn.execute(
                        "UPDATE audit_items SET expected_label=? WHERE audit_set_id=? AND rec_number=? AND facet=? AND expected_label IS NULL",
                        (x["label"], audit_set_id, x["rec_number"], x["facet"]),
                    )
                else:
                    cur = conn.execute(
                        "UPDATE audit_items SET expected_label=? WHERE audit_set_id=? AND rec_number=? AND expected_label IS NULL",
                        (x["label"], audit_set_id, x["rec_number"]),
                    )
                if cur.rowcount != 1:
                    raise RuntimeError("audit item missing, duplicate, or concurrently labelled")
            aset = conn.execute(
                "SELECT session_id FROM audit_sets WHERE audit_set_id=?", (audit_set_id,)
            ).fetchone()
            remaining = conn.execute(
                "SELECT COUNT(*) FROM audit_items WHERE audit_set_id=? AND expected_label IS NULL",
                (audit_set_id,),
            ).fetchone()[0]
            if remaining == 0 and aset:
                cur = conn.execute(
                    "UPDATE classification_sessions SET status='audited',updated_at=? WHERE session_id=? AND status='auditing'",
                    (_now(), aset["session_id"]),
                )
                if cur.rowcount != 1:
                    raise RuntimeError("concurrent session update")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return len(labels)


def classification_report(conn: sqlite3.Connection, audit_set_id: str) -> dict:
    ensure_schema(conn)
    aset = conn.execute(
        "SELECT library_fingerprint,seed,holdout FROM audit_sets WHERE audit_set_id=?",
        (audit_set_id,),
    ).fetchone()
    if not aset:
        raise KeyError("unknown audit set")
    rows = conn.execute(
        "SELECT assigned_label,assigned_status,expected_label FROM audit_items WHERE audit_set_id=? AND expected_label IS NOT NULL",
        (audit_set_id,),
    ).fetchall()
    total = len(rows)
    correct = sum(1 for r in rows if r["assigned_label"] == r["expected_label"])
    covered = sum(1 for r in rows if r["assigned_status"] == "assigned")
    labels = sorted(
        {
            label
            for row in rows
            for label in (row["assigned_label"], row["expected_label"])
            if label is not None
        }
    )
    confusion = {
        expected: {
            assigned: sum(
                1
                for row in rows
                if row["expected_label"] == expected and row["assigned_label"] == assigned
            )
            for assigned in labels
        }
        for expected in labels
    }
    per_label = {}
    for label in labels:
        true_positive = sum(
            1 for row in rows if row["expected_label"] == label and row["assigned_label"] == label
        )
        expected_count = sum(1 for row in rows if row["expected_label"] == label)
        assigned_count = sum(1 for row in rows if row["assigned_label"] == label)
        per_label[label] = {
            "audited": expected_count,
            "correct": true_positive,
            "precision": true_positive / assigned_count if assigned_count else None,
            "recall": true_positive / expected_count if expected_count else None,
        }
    statuses = {
        k: sum(1 for r in rows if r["assigned_status"] == k)
        for k in ("conflict", "unresolved", "abstain")
    }
    return {
        "audit_set_id": audit_set_id,
        "library_fingerprint": aset["library_fingerprint"],
        "seed": aset["seed"],
        "holdout": bool(aset["holdout"]),
        "audited": total,
        "correct": correct,
        "accuracy": correct / total if total else None,
        "coverage": covered / total if total else None,
        **statuses,
        "per_label": per_label,
        "confusion_matrix": confusion,
        "scope_note": "Metrics describe this audit set only; no whole-library accuracy is inferred.",
    }
