"""Resolve stable user-facing selectors to one indexed EndNote record."""

from __future__ import annotations

import sqlite3
from typing import TypedDict

from . import db, parser


class ReferenceSelector(TypedDict, total=False):
    """Exactly one of these keys must be supplied."""

    rec_number: int
    title: str
    doi: str


def _candidate(ref: dict) -> dict:
    return {
        "rec_number": ref.get("rec_number"),
        "title": ref.get("title"),
        "year": ref.get("year"),
        "container_title": ref.get("container_title"),
        "doi": ref.get("doi"),
    }


def _ambiguous(kind: str, value: str, matches: list[dict]) -> ValueError:
    candidates = [_candidate(ref) for ref in matches]
    return ValueError(
        f"{kind} matched {len(matches)} references; choose one by rec_number. "
        f"Candidates: {candidates}"
    )


def resolve_reference(
    conn: sqlite3.Connection,
    *,
    rec_number: int | None = None,
    title: str | None = None,
    doi: str | None = None,
) -> dict:
    """Resolve exactly one record number, title, or DOI.

    Titles prefer a normalized exact match and then fall back to a normalized
    substring. DOI URLs and ``doi:`` prefixes are accepted. Duplicate records
    fail closed and report candidates instead of silently choosing one.
    """
    clean_title = title.strip() if isinstance(title, str) else ""
    clean_doi = parser.normalize_doi(doi) if isinstance(doi, str) else ""
    provided = [rec_number is not None, bool(clean_title), bool(clean_doi)]
    if sum(provided) != 1:
        raise ValueError("Provide exactly one of rec_number, title, or doi.")

    if rec_number is not None:
        ref = db.get_reference(conn, rec_number)
        if ref is None:
            raise ValueError(f"No reference with rec_number={rec_number}")
        return ref

    if clean_title:
        matches = db.find_references_by_title(conn, clean_title, limit=50)
        if not matches:
            raise ValueError(f"No reference title matches {clean_title!r}.")
        if len(matches) != 1:
            raise _ambiguous("Title", clean_title, matches)
        return matches[0]

    matches = db.find_references_by_doi(conn, clean_doi)
    if not matches:
        raise ValueError(f"No reference DOI matches {clean_doi!r}.")
    if len(matches) != 1:
        raise _ambiguous("DOI", clean_doi, matches)
    return matches[0]


def resolve_many(conn: sqlite3.Connection, selectors: list[ReferenceSelector]) -> list[dict]:
    """Resolve an ordered list of mixed record-number/title/DOI selectors."""
    if not selectors:
        raise ValueError("references must contain at least one selector.")
    resolved = []
    for index, selector in enumerate(selectors):
        if not isinstance(selector, dict):
            raise ValueError(f"references[{index}] must be an object.")
        try:
            resolved.append(
                resolve_reference(
                    conn,
                    rec_number=selector.get("rec_number"),
                    title=selector.get("title"),
                    doi=selector.get("doi"),
                )
            )
        except ValueError as exc:
            raise ValueError(f"references[{index}]: {exc}") from exc
    return resolved
