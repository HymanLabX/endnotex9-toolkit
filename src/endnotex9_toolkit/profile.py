"""Deterministic, domain-neutral profiling of an EndNote reference database.

The functions in this module deliberately read the normalized ``refs`` and
group tables rather than an XML export.  They are useful as the discovery
phase of a classification session: no field-specific rules or domain
vocabulary are baked into the implementation.
"""

from __future__ import annotations

import itertools
import json
import re
from collections import Counter, defaultdict
from typing import Any

from . import tokenizer


def _text(value: Any, rec_number: int, field: str, errors: list[dict]) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError as exc:
            errors.append(
                {
                    "rec_number": rec_number,
                    "field": field,
                    "error": "invalid_utf8",
                    "message": str(exc),
                }
            )
            return value.decode("utf-8", "replace")
    return str(value)


def _json_values(value: Any, rec_number: int, field: str, errors: list[dict]) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError as exc:
            errors.append(
                {
                    "rec_number": rec_number,
                    "field": field,
                    "error": "invalid_utf8",
                    "message": str(exc),
                }
            )
            value = value.decode("utf-8", "replace")
    if isinstance(value, (list, tuple)):
        raw = value
    else:
        try:
            raw = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            errors.append(
                {
                    "rec_number": rec_number,
                    "field": field,
                    "error": "invalid_json",
                    "message": str(exc),
                }
            )
            return []
    if not isinstance(raw, list):
        errors.append(
            {
                "rec_number": rec_number,
                "field": field,
                "error": "invalid_json",
                "message": "expected JSON array",
            }
        )
        return []
    result: list[str] = []
    for item in raw:
        if item is None:
            continue
        text = _text(item, rec_number, field, errors).strip()
        if text:
            result.append(text)
    return result


def _rows(conn, errors: list[dict] | None = None) -> list[dict]:
    errors = errors if errors is not None else []
    rows: list[dict] = []
    for raw in conn.execute("SELECT * FROM refs ORDER BY rec_number"):
        rec = int(raw["rec_number"])
        row = dict(raw)
        row["_rec_number"] = rec
        for field in ("title", "abstract", "container_title", "year", "ref_type", "custom_1"):
            row[field] = _text(row.get(field), rec, field, errors)
        for field in ("keywords", "authors"):
            row[field] = _json_values(row.get(field), rec, field, errors)
        rows.append(row)
    return rows


def _term_counts(rows: list[dict], field: str) -> tuple[Counter[str], Counter[str]]:
    """Return document frequency and occurrence counts for a field."""
    df: Counter[str] = Counter()
    occurrences: Counter[str] = Counter()
    for row in rows:
        values = (
            row[field]
            if field in ("keywords", "authors")
            else tokenizer.tokenize(row.get(field, ""))
        )
        terms = [str(v).strip().casefold() for v in values if str(v).strip()]
        df.update(set(terms))
        occurrences.update(terms)
    return df, occurrences


def _ranked(counter: Counter[str], total: int, limit: int) -> list[dict]:
    return [
        {"term": term, "df": count, "coverage": round(count / total, 6) if total else 0.0}
        for term, count in sorted(counter.items(), key=lambda item: (-item[1], item[0]))[:limit]
    ]


def _enrichment(
    rows: list[dict],
    key: str,
    terms: Counter[str],
    top: int,
    max_groups: int = 50,
    corpus_total: int | None = None,
) -> list[dict]:
    buckets: defaultdict[str, list[dict]] = defaultdict(list)
    for row in rows:
        name = row.get(key, "") or "(unspecified)"
        buckets[name].append(row)
    corpus_n = corpus_total if corpus_total is not None else len(rows)
    out: list[dict] = []
    for name, members in sorted(buckets.items(), key=lambda x: (-len(x[1]), x[0].casefold()))[
        :max_groups
    ]:
        local_df: Counter[str] = Counter()
        for row in members:
            vals = row["keywords"] + tokenizer.tokenize(
                f"{row.get('title', '')} {row.get('abstract', '')}"
            )
            local_df.update(set(v.casefold() for v in vals if v.strip()))
        ranked = []
        for term, count in local_df.items():
            corpus_df = terms.get(term, 0)
            if not corpus_df:
                continue
            lift = (count / len(members)) / (corpus_df / corpus_n) if corpus_n else 0.0
            ranked.append((lift, count, term))
        ranked.sort(key=lambda x: (-x[0], -x[1], x[2]))
        out.append(
            {
                "name": name,
                "size": len(members),
                "coverage": round(len(members) / corpus_n, 6) if corpus_n else 0.0,
                "top_terms": [
                    {"term": term, "df": count, "lift": round(lift, 6)}
                    for lift, count, term in ranked[:top]
                ],
            }
        )
    return out


def profile_library(
    conn,
    top: int = 15,
    min_df: int = 2,
    max_terms: int = 50,
) -> dict:
    """Build a deterministic corpus profile suitable for proposing a taxonomy."""
    if not 1 <= top <= 100:
        raise ValueError("top must be between 1 and 100")
    if min_df < 1:
        raise ValueError("min_df must be at least 1")
    if not 1 <= max_terms <= 500:
        raise ValueError("max_terms must be between 1 and 500")
    errors: list[dict] = []
    rows = _rows(conn, errors)
    total = len(rows)
    coverage_fields = ("title", "abstract", "keywords", "authors", "container_title", "year")
    coverage = {}
    for field in coverage_fields:
        present = sum(bool(row[field]) for row in rows)
        coverage[field] = {
            "documents": present,
            "coverage": round(present / total, 6) if total else 0.0,
        }

    keyword_df, keyword_occ = _term_counts(rows, "keywords")
    text_df: Counter[str] = Counter()
    for row in rows:
        text_df.update(set(tokenizer.tokenize(f"{row['title']} {row['abstract']}")))
    candidate_df: Counter[str] = Counter()
    for row in rows:
        tokens = tokenizer.tokenize(f"{row['title']} {row['abstract']}")
        for n in (2, 3):
            candidate_df.update(
                set(" ".join(tokens[i : i + n]) for i in range(len(tokens) - n + 1))
            )
    candidate_ngrams = [
        {
            "term": term,
            "n": len(term.split()),
            "df": count,
            "coverage": round(count / total, 6) if total else 0.0,
        }
        for term, count in sorted(candidate_df.items(), key=lambda x: (-x[1], x[0]))
        if count >= min_df
    ][:max_terms]

    cooccur: Counter[tuple[str, str]] = Counter()
    for row in rows:
        kws = sorted(set(k.casefold() for k in row["keywords"] if k.strip()))
        cooccur.update(itertools.combinations(kws, 2))
    keyword_cooccurrence = [
        {"left": a, "right": b, "count": count}
        for (a, b), count in sorted(cooccur.items(), key=lambda x: (-x[1], x[0]))[:max_terms]
        if count >= min_df
    ]

    # Very frequent/import-trace terms are useful to flag before taxonomy design.
    noise: list[dict] = []
    for term, count in sorted(keyword_df.items(), key=lambda x: (-x[1], x[0])):
        cov = count / total if total else 0.0
        reason = None
        if cov >= 0.30:
            reason = "high_document_frequency"
        elif len(term) <= 2 or re.fullmatch(r"x[- ]?mol|import|unknown|misc", term):
            reason = "short_or_import_like"
        if reason:
            noise.append({"term": term, "df": count, "coverage": round(cov, 6), "reason": reason})
    noise = noise[:max_terms]

    journal_terms = keyword_df + text_df
    journal = _enrichment(rows, "container_title", journal_terms, top)
    # Materialize group names without relying on XML or cached JSON.
    memberships: defaultdict[int, list[str]] = defaultdict(list)
    try:
        for item in conn.execute(
            "SELECT m.rec_number, g.name FROM group_members m JOIN groups g ON g.id=m.group_id"
        ):
            memberships[int(item["rec_number"])].append(str(item["name"]))
    except Exception as exc:  # an older index may not have group tables
        errors.append({"error": "groups_unavailable", "message": str(exc)})
    group_rows: list[dict] = []
    for row in rows:
        for name in memberships.get(row["_rec_number"], []):
            clone = dict(row)
            clone["container_title"] = name
            group_rows.append(clone)
    group = (
        _enrichment(group_rows, "container_title", journal_terms, top, corpus_total=total)
        if group_rows
        else []
    )

    return {
        "total_references": total,
        "coverage": coverage,
        "document_frequency": _ranked(keyword_df, total, max_terms),
        "keyword_occurrence": _ranked(keyword_occ, total, max_terms),
        "candidate_ngrams": candidate_ngrams,
        "keyword_cooccurrence": keyword_cooccurrence,
        "journal_enrichment": journal,
        "group_enrichment": group,
        "noise_candidates": noise,
        "errors": errors,
    }
