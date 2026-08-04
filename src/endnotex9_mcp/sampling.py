"""Stable, bounded reference sampling for classification sessions."""

from __future__ import annotations

import hashlib
import json
import random
from collections import defaultdict
from typing import Any

from . import profile
from .classification import canonical_json

_DTO_FIELDS = (
    "rec_number",
    "title",
    "abstract",
    "keywords",
    "container_title",
    "year",
    "ref_type",
    "authors",
    "groups",
)


def library_fingerprint(conn) -> str:
    """Hash the indexed corpus and group membership deterministically."""
    errors: list[dict] = []
    rows = profile._rows(conn, errors)
    memberships: defaultdict[int, list[str]] = defaultdict(list)
    try:
        for row in conn.execute(
            "SELECT m.rec_number, g.name FROM group_members m JOIN groups g ON g.id=m.group_id"
        ):
            memberships[int(row["rec_number"])].append(str(row["name"]))
    except Exception:
        pass
    payload = []
    for row in rows:
        payload.append(
            {
                key: row.get(key, []) if key in ("keywords", "authors") else row.get(key, "")
                for key in (
                    "_rec_number",
                    "title",
                    "abstract",
                    "container_title",
                    "year",
                    "ref_type",
                    "keywords",
                    "authors",
                    "content_hash",
                )
            }
            | {"groups": sorted(memberships.get(row["_rec_number"], []), key=str.casefold)}
        )
    encoded = canonical_json(payload).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _groups(conn, rec_number: int) -> list[str]:
    try:
        return sorted(
            {
                str(row["name"])
                for row in conn.execute(
                    "SELECT g.name FROM group_members m JOIN groups g ON g.id=m.group_id "
                    "WHERE m.rec_number = ?",
                    (rec_number,),
                )
            },
            key=lambda x: x.casefold(),
        )
    except Exception:
        return []


def _dto(row: dict, conn, errors: list[dict]) -> dict:
    rec = int(row["_rec_number"])
    # Keep precisely the documented nine fields.  Truncation metadata is
    # added by the caller only when a field had to be shortened.
    return {
        "rec_number": rec,
        "title": row.get("title", ""),
        "abstract": row.get("abstract", ""),
        "keywords": list(row.get("keywords", [])),
        "container_title": row.get("container_title", ""),
        "year": row.get("year", ""),
        "ref_type": row.get("ref_type", ""),
        "authors": list(row.get("authors", [])),
        "groups": _groups(conn, rec),
    }


def _encoded_size(value: dict) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _fit_budget(item: dict, budget: int) -> tuple[dict, list[str]]:
    """Shorten verbose fields to fit one item's UTF-8 budget."""
    if budget <= 0:
        return item, []
    result = dict(item)
    result["truncated_fields"] = []
    truncated: list[str] = []
    if _encoded_size(result) <= budget:
        return result, truncated
    # Abstracts dominate payloads. Truncate by UTF-8 bytes while preserving
    # valid text and mark the field for the caller.
    for field in ("abstract", "title", "authors", "keywords"):
        if _encoded_size(result) <= budget:
            break
        value = result.get(field)
        if isinstance(value, str) and value:
            lo, hi = 0, len(value)
            while lo < hi:
                mid = (lo + hi + 1) // 2
                candidate = dict(result)
                candidate[field] = value[:mid] + "…"
                candidate["truncated_fields"] = sorted({*truncated, field})
                if _encoded_size(candidate) <= budget:
                    lo = mid
                else:
                    hi = mid - 1
            candidate = dict(result)
            candidate[field] = value[:lo] + "…" if lo < len(value) else value
            candidate["truncated_fields"] = sorted({*truncated, field})
            result = candidate
            truncated.append(field)
        elif isinstance(value, list) and value:
            result[field] = []
            truncated.append(field)
            result["truncated_fields"] = sorted(set(truncated))
    # Groups and other small fields can still exceed an unusually tiny
    # budget. Empty verbose values are preferable to violating the byte
    # contract; the metadata tells the caller what was discarded.
    if _encoded_size(result) > budget:
        for field in ("abstract", "title", "authors", "keywords", "groups"):
            if _encoded_size(result) <= budget:
                break
            if result.get(field):
                result[field] = [] if isinstance(result[field], list) else ""
                truncated.append(field)
                result["truncated_fields"] = sorted(set(truncated))
    return result, truncated


def sample_references(
    conn,
    session_id: str | None = None,
    strategy: str = "random",
    size: int = 20,
    seed: int = 0,
    char_budget: int = 20000,
    group: str | None = None,
    journal: str | None = None,
    snapshot_fingerprint: str | None = None,
    evaluate: Any = None,
) -> dict:
    """Return a reproducible sample and its session/library snapshot.

    ``char_budget`` is measured in UTF-8 bytes.  The API keeps records sorted
    by ``rec_number`` after selecting them, independent of the random seed.
    """
    errors: list[dict] = []
    rows = profile._rows(conn, errors)
    fingerprint = library_fingerprint(conn)
    result = {
        "session_id": session_id,
        "library_fingerprint": fingerprint,
        "seed": seed,
        "strategy": strategy,
        "returned": 0,
        "truncated": False,
        "errors": errors,
        "references": [],
    }
    if snapshot_fingerprint and snapshot_fingerprint != fingerprint:
        result["errors"].append(
            {
                "code": "snapshot_mismatch",
                "expected": snapshot_fingerprint,
                "actual": fingerprint,
                "message": "library changed since the session snapshot",
            }
        )
        return result
    if strategy not in {"random", "group", "journal", "uncertain"}:
        result["errors"].append({"code": "invalid_strategy", "strategy": strategy})
        return result
    if not 0 <= size <= 500:
        result["errors"].append(
            {
                "code": "invalid_size",
                "message": "size must be between 0 and 500",
            }
        )
        return result
    if not 256 <= char_budget <= 200000:
        result["errors"].append(
            {
                "code": "invalid_budget",
                "message": "char_budget is a UTF-8 byte budget between 256 and 200000",
            }
        )
        return result
    if strategy == "uncertain" and evaluate is None:
        result["errors"].append(
            {
                "code": "precondition_failed",
                "precondition": "evaluate",
                "message": "uncertain sampling requires an evaluation result",
            }
        )
        return result

    eligible = rows
    if strategy == "group":
        if not group:
            result["errors"].append(
                {
                    "code": "precondition_failed",
                    "precondition": "group",
                    "message": "group strategy requires group",
                }
            )
            return result
        needle = group.casefold()
        eligible = [
            row
            for row in rows
            if needle in {g.casefold() for g in _groups(conn, row["_rec_number"])}
        ]
    elif strategy == "journal":
        if not journal:
            result["errors"].append(
                {
                    "code": "precondition_failed",
                    "precondition": "journal",
                    "message": "journal strategy requires journal",
                }
            )
            return result
        needle = journal.casefold()
        eligible = [row for row in rows if needle in row.get("container_title", "").casefold()]
    elif strategy == "uncertain":
        # Accept either a mapping rec_number -> score/label or an iterable of
        # record numbers.  This keeps the primitive useful to multiple
        # classifiers without embedding one scoring model here.
        if isinstance(evaluate, dict):
            ids = {int(k) for k, v in evaluate.items() if v is not None}
        else:
            ids = {int(v) for v in evaluate}
        eligible = [row for row in rows if row["_rec_number"] in ids]

    rng = random.Random(seed)
    if size < len(eligible):
        selected = rng.sample(eligible, size)
    else:
        selected = list(eligible)
    selected.sort(key=lambda row: row["_rec_number"])

    budget_used = 0
    for row in selected:
        item = _dto(row, conn, result["errors"])
        fitted, truncated_fields = _fit_budget(item, max(0, char_budget - budget_used))
        fitted["truncated_fields"] = sorted(set(truncated_fields))
        item_bytes = _encoded_size(fitted)
        if budget_used and budget_used + item_bytes > char_budget:
            result["truncated"] = True
            break
        if item_bytes > char_budget and not budget_used:
            # An individual record cannot fit even after shortening.  Do not
            # return an item that violates the byte budget.
            result["truncated"] = True
            break
        budget_used += item_bytes
        result["references"].append(fitted)
        if truncated_fields:
            result["truncated"] = True
    if len(result["references"]) < len(selected):
        result["truncated"] = True
    result["returned"] = len(result["references"])
    result["budget_bytes"] = budget_used
    return result


__all__ = ["library_fingerprint", "sample_references"]
