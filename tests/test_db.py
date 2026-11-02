"""Index-level plumbing: the transaction lock that serialises writers.

The watcher reindexes on its own connection from a background thread while
MCP tool handlers write on the shared one. Only a lock scoped to the
*database file* makes those two mutually exclusive.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from endnotex9_toolkit import db


def test_exact_title_and_doi_queries_use_indexes(tmp_path):
    conn = db.connect(tmp_path / "index.db")
    try:
        for sql, value, expected in [
            (
                "SELECT * FROM refs WHERE endnote_title_key(title) = ?",
                "a title",
                "idx_refs_normalized_title",
            ),
            ("SELECT * FROM refs WHERE doi = ? COLLATE NOCASE", "10.1/a", "idx_refs_doi"),
        ]:
            plan = conn.execute("EXPLAIN QUERY PLAN " + sql, (value,)).fetchall()
            assert expected in " ".join(str(tuple(row)) for row in plan)
    finally:
        conn.close()


def test_two_connections_to_one_database_share_a_lock(tmp_path: Path):
    path = tmp_path / "index.db"
    first = db.connect(path)
    second = db.connect(path)
    try:
        assert db.connection_lock(first) is db.connection_lock(second)
    finally:
        first.close()
        second.close()


def test_separate_databases_do_not_block_each_other(tmp_path: Path):
    first = db.connect(tmp_path / "one.db")
    second = db.connect(tmp_path / "two.db")
    try:
        assert db.connection_lock(first) is not db.connection_lock(second)
    finally:
        first.close()
        second.close()


def test_the_same_file_spelled_differently_still_shares_a_lock(tmp_path: Path):
    path = tmp_path / "index.db"
    first = db.connect(path)
    second = db.connect(tmp_path / "sub" / ".." / "index.db")
    try:
        assert db.connection_lock(first) is db.connection_lock(second)
    finally:
        first.close()
        second.close()


def test_a_foreign_connection_resolves_to_its_files_lock(tmp_path: Path):
    """A raw sqlite3 handle carries no lock attribute, so the file it has
    open is what identifies it."""
    path = tmp_path / "index.db"
    ours = db.connect(path)
    foreign = sqlite3.connect(str(path))
    try:
        assert db.connection_lock(foreign) is db.connection_lock(ours)
    finally:
        ours.close()
        foreign.close()


def test_a_reopened_connection_does_not_inherit_a_stale_lock(tmp_path: Path):
    """Keying on `id(conn)` used to alias a recycled id onto a dead
    connection's lock — and the watcher opens and closes a connection per
    reindex, so ids really do get recycled."""
    path = tmp_path / "index.db"
    keeper = db.connect(path)
    expected = db.connection_lock(keeper)
    try:
        for _ in range(50):
            transient = db.connect(path)
            assert db.connection_lock(transient) is expected
            transient.close()
    finally:
        keeper.close()


def test_the_lock_actually_excludes_a_second_writer(tmp_path: Path):
    path = tmp_path / "index.db"
    first = db.connect(path)
    second = db.connect(path)
    order: list[str] = []

    def wait_for_the_lock() -> None:
        with db.connection_lock(second):
            order.append("second")

    try:
        with db.connection_lock(first):
            worker = threading.Thread(target=wait_for_the_lock)
            worker.start()
            # Give the thread a chance to reach the lock and block on it.
            worker.join(timeout=0.2)
            order.append("first")
        worker.join(timeout=5)
        assert not worker.is_alive()
        assert order == ["first", "second"]
    finally:
        first.close()
        second.close()
