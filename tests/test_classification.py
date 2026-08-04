from concurrent.futures import ThreadPoolExecutor

import pytest

from endnotex9_mcp import classification, db, sampling


def _conn(tmp_path):
    c = db.connect(tmp_path / "index.db")
    db.upsert_reference(
        c,
        {
            "rec_number": 1,
            "title": "Alpha",
            "content_hash": "x",
            "keywords": [],
            "authors": [],
        },
    )
    c.execute("INSERT INTO groups(name) VALUES('Keep')")
    c.commit()
    return c


def test_additive_migration_preserves_host_tables(tmp_path):
    conn = _conn(tmp_path)
    before_fts = conn.execute("SELECT COUNT(*) FROM refs_fts").fetchone()[0]
    classification.ensure_schema(conn)
    assert conn.execute("SELECT title FROM refs WHERE rec_number=1").fetchone()[0] == "Alpha"
    assert conn.execute("SELECT name FROM groups").fetchone()[0] == "Keep"
    assert conn.execute("SELECT COUNT(*) FROM refs_fts").fetchone()[0] == before_fts
    assert (
        conn.execute("PRAGMA user_version").fetchone()[0]
        == classification.CLASSIFICATION_DB_VERSION
    )
    assert conn.execute("SELECT state FROM migration_history").fetchone()[0] == "complete"


def test_rule_evaluation_is_fail_closed_for_conflict(tmp_path):
    conn = _conn(tmp_path)
    s = classification.create_session(conn, sampling.library_fingerprint(conn), seed=1)
    t = classification.save_taxonomy(
        conn,
        s["session_id"],
        {
            "facets": [
                {
                    "id": "topic",
                    "approved": True,
                    "slot": "custom_1",
                    "mode": "single",
                    "labels": ["A", "B"],
                }
            ]
        },
    )
    classification.approve_taxonomy(conn, s["session_id"], t["hash"])
    r = classification.save_ruleset(
        conn,
        s["session_id"],
        {
            "rules": [
                {
                    "id": "a",
                    "facet": "topic",
                    "label": "A",
                    "when": [{"op": "contains_any", "field": "title", "values": ["Alpha"]}],
                },
                {
                    "id": "b",
                    "facet": "topic",
                    "label": "B",
                    "when": [{"op": "contains_any", "field": "title", "values": ["Alpha"]}],
                },
            ]
        },
    )
    classification.freeze_ruleset(conn, s["session_id"], r["hash"])
    result = classification.evaluate_rules(conn, s["session_id"], None)
    assert result["conflict"] == 1 and result["assigned"] == 0


def test_unsupported_regex_and_future_migration_rejected(tmp_path):
    with pytest.raises(ValueError):
        classification.canonical_json({"x": float("nan")})
    conn = _conn(tmp_path)
    conn.execute("PRAGMA user_version = 999")
    with pytest.raises(RuntimeError):
        classification.ensure_schema(conn)
    assert conn.execute("SELECT title FROM refs WHERE rec_number=1").fetchone()[0] == "Alpha"


def test_rule_allowlist_and_explicit_approval_gate(tmp_path):
    conn = _conn(tmp_path)
    s = classification.create_session(conn, "fp")
    t = classification.save_taxonomy(
        conn,
        s["session_id"],
        {
            "facets": [
                {
                    "id": "topic",
                    "approved": True,
                    "slot": "custom_1",
                    "mode": "single",
                    "labels": ["A", "B", "X"],
                }
            ]
        },
    )
    with pytest.raises(RuntimeError):
        classification.save_ruleset(conn, s["session_id"], {"rules": []})
    classification.approve_taxonomy(conn, s["session_id"], t["hash"])
    with pytest.raises(ValueError):
        classification.save_ruleset(
            conn,
            s["session_id"],
            {
                "rules": [
                    {
                        "id": "x",
                        "facet": "topic",
                        "label": "X",
                        "when": [{"op": "contains_any", "field": "password", "values": ["x"]}],
                    }
                ]
            },
        )


def test_rule_schema_and_declared_labels_fail_closed(tmp_path):
    conn = _conn(tmp_path)
    session = classification.create_session(conn, sampling.library_fingerprint(conn))
    taxonomy = classification.save_taxonomy(
        conn,
        session["session_id"],
        {
            "facets": [
                {
                    "id": "topic",
                    "approved": True,
                    "slot": "custom_1",
                    "mode": "single",
                    "labels": ["A"],
                }
            ],
        },
    )
    classification.approve_taxonomy(conn, session["session_id"], taxonomy["hash"])
    with pytest.raises(ValueError, match="priority"):
        classification.save_ruleset(
            conn,
            session["session_id"],
            {
                "rules": [
                    {
                        "id": "bad",
                        "facet": "topic",
                        "label": "A",
                        "priority": "high",
                        "when": [{"op": "contains_any", "field": "title", "values": ["Alpha"]}],
                    }
                ]
            },
        )
    with pytest.raises(ValueError, match="year_range"):
        classification.save_ruleset(
            conn,
            session["session_id"],
            {
                "rules": [
                    {
                        "id": "bad-year",
                        "facet": "topic",
                        "label": "A",
                        "when": [{"op": "year_range", "field": "year", "min": "2020"}],
                    }
                ]
            },
        )
    with pytest.raises(ValueError, match="not declared"):
        classification.save_ruleset(
            conn,
            session["session_id"],
            {
                "rules": [
                    {
                        "id": "bad-label",
                        "facet": "topic",
                        "label": "Unknown",
                        "when": [{"op": "contains_any", "field": "title", "values": ["Alpha"]}],
                    }
                ]
            },
        )


def test_higher_priority_rule_wins_and_evidence_is_auditable(tmp_path):
    conn = _conn(tmp_path)
    session = classification.create_session(conn, sampling.library_fingerprint(conn))
    taxonomy = classification.save_taxonomy(
        conn,
        session["session_id"],
        {
            "facets": [
                {
                    "id": "topic",
                    "approved": True,
                    "slot": "custom_1",
                    "mode": "single",
                    "labels": ["A", "B"],
                }
            ],
        },
    )
    classification.approve_taxonomy(conn, session["session_id"], taxonomy["hash"])
    rules = classification.save_ruleset(
        conn,
        session["session_id"],
        {
            "rules": [
                {
                    "id": "weak",
                    "facet": "topic",
                    "label": "A",
                    "priority": 1,
                    "when": [{"op": "contains_any", "field": "title", "values": ["Alpha"]}],
                },
                {
                    "id": "strong",
                    "facet": "topic",
                    "label": "B",
                    "priority": 10,
                    "when": [{"op": "contains_any", "field": "title", "values": ["Alpha"]}],
                },
            ]
        },
    )
    classification.freeze_ruleset(conn, session["session_id"], rules["hash"])
    result = classification.evaluate_rules(conn, session["session_id"], None)
    assert result["assigned"] == 1 and result["conflict"] == 0
    row = conn.execute("SELECT label, rule_id, evidence_json FROM assignments").fetchone()
    assert row["label"] == "B" and row["rule_id"] == "strong"
    assert '"decisive_priority":10' in row["evidence_json"]
    assert '"fields":["title"]' in row["evidence_json"]


def test_taxonomy_approval_is_concurrency_safe(tmp_path):
    conn = _conn(tmp_path)
    session = classification.create_session(conn, sampling.library_fingerprint(conn))
    taxonomy = classification.save_taxonomy(
        conn,
        session["session_id"],
        {
            "facets": [
                {
                    "id": "topic",
                    "approved": True,
                    "slot": "custom_1",
                    "mode": "single",
                    "labels": ["A", "B"],
                }
            ],
        },
    )

    def approve():
        return classification.approve_taxonomy(conn, session["session_id"], taxonomy["hash"])

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(approve) for _ in range(2)]
    successes = sum(future.exception() is None for future in futures)
    assert successes == 1
    assert classification.get_session(conn, session["session_id"])["status"] == "taxonomy_approved"


def test_canonical_json_normalizes_unicode():
    assert classification.content_hash({"label": "é"}) == classification.content_hash(
        {"label": "e\u0301"}
    )


@pytest.mark.parametrize("size", [0, -1, 501])
def test_audit_size_is_bounded(tmp_path, size):
    conn = _conn(tmp_path)
    session = classification.create_session(conn, sampling.library_fingerprint(conn))
    with pytest.raises(ValueError, match="size"):
        classification.create_audit_sample(conn, session["session_id"], size=size)


def test_supported_host_schema_tracks_the_index_schema():
    """`ensure_schema` fails closed on a host layout it has not read.

    It deliberately does not follow `db.SCHEMA_VERSION` — that would make any
    future layout silently acceptable. This assertion is the prompt instead:
    bumping the index schema breaks here, so someone has to go and decide
    whether the classification tables really do carry over.
    """
    assert classification.SUPPORTED_HOST_SCHEMA == db.SCHEMA_VERSION


def test_ensure_schema_refuses_an_unknown_host_layout(tmp_path):
    conn = db.connect(tmp_path / "index.db")
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
        (str(db.SCHEMA_VERSION + 1),),
    )
    conn.commit()
    with pytest.raises(RuntimeError, match="unsupported host schema version"):
        classification.ensure_schema(conn)
    conn.close()
