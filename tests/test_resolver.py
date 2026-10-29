"""User-facing reference selectors resolve safely and deterministically."""

from __future__ import annotations

import pytest

from endnotex9_toolkit import db, resolver


def test_resolves_by_record_number_title_and_doi(conn):
    assert resolver.resolve_reference(conn, rec_number=1)["rec_number"] == 1
    assert (
        resolver.resolve_reference(
            conn, title="Giant Elastocaloric Effect in Ni-Mn-Ga-Based Alloys"
        )["rec_number"]
        == 1
    )
    assert (
        resolver.resolve_reference(conn, doi="https://doi.org/10.1021/ACSAMI.1C21109")["rec_number"]
        == 1
    )


def test_selector_requires_exactly_one_identifier(conn):
    with pytest.raises(ValueError, match="exactly one"):
        resolver.resolve_reference(conn)
    with pytest.raises(ValueError, match="exactly one"):
        resolver.resolve_reference(conn, rec_number=1, title="anything")


def test_ambiguous_title_returns_candidates(conn):
    with pytest.raises(ValueError, match=r"Title matched 2 references.*rec_number"):
        resolver.resolve_reference(conn, title="shape memory")


def test_duplicate_doi_returns_candidates_instead_of_first_record(conn):
    original = db.get_reference(conn, 1)
    duplicate = dict(original)
    duplicate.update(rec_number=99, title="Duplicate DOI record")
    db.upsert_reference(conn, duplicate)
    conn.commit()

    with pytest.raises(ValueError, match=r"DOI matched 2 references.*99"):
        resolver.resolve_reference(conn, doi="doi:10.1021/acsami.1c21109")


def test_mixed_batch_preserves_requested_order(conn):
    refs = resolver.resolve_many(
        conn,
        [
            {"doi": "10.1016/j.actamat.2020.106545"},
            {"title": "Introduction to shape memory materials"},
            {"rec_number": 3},
        ],
    )
    assert [ref["rec_number"] for ref in refs] == [2, 4, 3]


def test_batch_error_identifies_the_bad_selector(conn):
    with pytest.raises(ValueError, match=r"references\[1\]"):
        resolver.resolve_many(conn, [{"rec_number": 1}, {"doi": "10.invalid/missing"}])
