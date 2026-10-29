import hashlib
import os
import shutil
from pathlib import Path

import pytest

from endnotex9_toolkit import classification, db, sampling, writeback, writeplan


def _source_files(tmp_path):
    rdb = tmp_path / "Library.Data" / "rdb"
    rdb.mkdir(parents=True)
    enl = tmp_path / "Library.enl"
    for path, body in (
        (enl, b"enl"),
        (rdb / "refs.MYD", b"records"),
        (rdb / "refs.MYI", b"index"),
        (rdb / "misc.MYD", b"groups"),
    ):
        path.write_bytes(body)
    return rdb, enl


def _state(paths):
    return {
        path: (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
        for path in paths
    }


def _classified_conn(tmp_path):
    conn = db.connect(tmp_path / "index.db")
    for rec, old in ((1, ""), (2, "Old")):
        conn.execute(
            "INSERT INTO refs(rec_number,title,custom_1) VALUES(?,?,?)", (rec, f"Title {rec}", old)
        )
    conn.commit()
    session = classification.create_session(conn, sampling.library_fingerprint(conn), seed=1)
    sid = session["session_id"]
    saved = classification.save_taxonomy(
        conn,
        sid,
        {
            "facets": [
                {
                    "id": "topic",
                    "approved": True,
                    "slot": "custom_1",
                    "mode": "single",
                    "labels": ["Alpha", "Old", "A"],
                }
            ],
        },
    )
    classification.approve_taxonomy(conn, sid, saved["hash"])
    saved_rules = classification.save_ruleset(conn, sid, {"rules": []})
    classification.freeze_ruleset(conn, sid, saved_rules["hash"])
    now = "2026-01-01T00:00:00+00:00"
    s = conn.execute("SELECT * FROM classification_sessions WHERE session_id=?", (sid,)).fetchone()
    evaluation_hash = classification.content_hash(
        {
            "session": sid,
            "fingerprint": s["library_fingerprint"],
            "taxonomy": s["taxonomy_hash"],
            "ruleset": s["ruleset_hash"],
        }
    )
    for rec, label in ((1, "Alpha"), (2, "Old")):
        conn.execute(
            """INSERT INTO assignments(
                 session_id,evaluation_hash,rec_number,facet,label,status,
                 taxonomy_hash,ruleset_hash,created_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                sid,
                evaluation_hash,
                rec,
                "topic",
                label,
                "assigned",
                s["taxonomy_hash"],
                s["ruleset_hash"],
                now,
            ),
        )
    conn.commit()
    conn.execute("UPDATE classification_sessions SET status='evaluated' WHERE session_id=?", (sid,))
    conn.commit()
    audit = classification.create_audit_sample(conn, sid, size=2, seed=1)
    classification.submit_audit_labels(
        conn,
        audit["audit_set_id"],
        [
            {"rec_number": 1, "label": "Alpha"},
            {"rec_number": 2, "label": "Old"},
        ],
    )
    return conn, sid


def test_prepare_is_endnote_read_only_and_returns_diff(tmp_path, monkeypatch):
    conn, sid = _classified_conn(tmp_path)
    rdb, enl = _source_files(tmp_path)
    paths = [enl, rdb / "refs.MYD", rdb / "refs.MYI", rdb / "misc.MYD"]
    before = _state(paths)

    def forbidden(*args, **kwargs):
        raise AssertionError("EndNote write operation was called")

    monkeypatch.setattr(writeback, "set_column", forbidden)
    monkeypatch.setattr(shutil, "copy", forbidden)
    monkeypatch.setattr(os, "replace", forbidden)
    monkeypatch.setattr(Path, "write_bytes", forbidden)

    result = writeplan.prepare_writeback(conn, sid, rdb, enl, "request-1")
    assert result["status"] == "prepared"
    assert result["field"] == "custom_1"
    assert result["changes"] == 1 and result["unchanged"] == 1
    assert result["diffs"] == [
        {
            "rec_number": 1,
            "field": "custom_1",
            "old_value": "",
            "new_value": "Alpha",
        }
    ]
    assert len(result["manifest"]) == 4
    assert not Path(result["backup_preview_path"]).exists()
    assert _state(paths) == before
    conn.close()


def test_prepare_requires_nonempty_holdout_audit(tmp_path):
    conn, sid = _classified_conn(tmp_path)
    rdb, enl = _source_files(tmp_path)
    conn.execute("UPDATE audit_sets SET holdout=0 WHERE session_id=?", (sid,))
    conn.commit()
    with pytest.raises(writeplan.WritePlanError, match="completed audit"):
        writeplan.prepare_writeback(conn, sid, rdb, enl, "non-holdout")
    conn.close()


def test_idempotency_reuses_same_payload_and_rejects_different_payload(tmp_path):
    conn, sid = _classified_conn(tmp_path)
    rdb, enl = _source_files(tmp_path)
    first = writeplan.prepare_writeback(conn, sid, rdb, enl, "same-key")
    second = writeplan.prepare_writeback(conn, sid, rdb, enl, "same-key")
    assert second["plan_id"] == first["plan_id"] and second["reused"]
    conn.execute(
        "UPDATE assignments SET label='Changed' WHERE session_id=? AND rec_number=1", (sid,)
    )
    conn.commit()
    with pytest.raises(writeplan.IdempotencyConflict):
        writeplan.prepare_writeback(conn, sid, rdb, enl, "same-key")
    conn.close()


def test_source_drift_expires_existing_plan(tmp_path):
    conn, sid = _classified_conn(tmp_path)
    rdb, enl = _source_files(tmp_path)
    first = writeplan.prepare_writeback(conn, sid, rdb, enl, "drift-key")
    with (rdb / "refs.MYD").open("ab") as target:
        target.write(b"-changed")
    with pytest.raises(writeplan.SourceFingerprintChanged):
        writeplan.prepare_writeback(conn, sid, rdb, enl, "drift-key")
    status = conn.execute(
        "SELECT status FROM writeback_plans WHERE plan_id=?", (first["plan_id"],)
    ).fetchone()[0]
    assert status == "expired"
    conn.close()


@pytest.mark.parametrize("failure", ["conflict", "abstain", "multifacet"])
def test_unsafe_assignment_states_fail_closed(tmp_path, failure):
    conn, sid = _classified_conn(tmp_path)
    rdb, enl = _source_files(tmp_path)
    if failure == "conflict":
        conn.execute(
            "UPDATE assignments SET status='conflict' WHERE session_id=? AND rec_number=1", (sid,)
        )
    elif failure == "abstain":
        conn.execute("DELETE FROM assignments WHERE session_id=? AND rec_number=1", (sid,))
    else:
        conn.execute(
            "UPDATE assignments SET facet='method' WHERE session_id=? AND rec_number=1", (sid,)
        )
    conn.commit()
    with pytest.raises(writeplan.WritePlanError):
        writeplan.prepare_writeback(conn, sid, rdb, enl, f"unsafe-{failure}")
    assert conn.execute("SELECT COUNT(*) FROM writeback_plans").fetchone()[0] == 0
    conn.close()
