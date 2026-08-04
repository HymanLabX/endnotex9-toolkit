"""Cross-domain contract for the read-only classification workflow."""

from __future__ import annotations

import hashlib
from pathlib import Path

from endnotex9_mcp import (
    classification,
    db,
    indexer,
    profile,
    sampling,
    writeplan,
)


def _temporary_endnote_sources(tmp_path: Path) -> tuple[Path, Path, dict[Path, tuple[str, int]]]:
    rdb = tmp_path / "Synthetic.Data" / "rdb"
    rdb.mkdir(parents=True)
    enl = tmp_path / "Synthetic.enl"
    paths = (
        enl,
        rdb / "refs.MYD",
        rdb / "refs.MYI",
        rdb / "misc.MYD",
    )
    for number, path in enumerate(paths, 1):
        path.write_bytes(f"fixture-{number}".encode())
    state = {
        path: (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
        for path in paths
    }
    return rdb, enl, state


def test_three_domain_fixture_completes_read_only_workflow(tmp_path: Path, cross_domain_xml: Path):
    conn = db.connect(tmp_path / "classification.db")
    indexer.build_index(cross_domain_xml, conn, full=True)

    corpus = profile.profile_library(conn, min_df=1)
    assert corpus["total_references"] == 2
    assert corpus["coverage"]["abstract"]["documents"] == 2

    fingerprint = sampling.library_fingerprint(conn)
    session = classification.create_session(conn, fingerprint, seed=17)
    sample = sampling.sample_references(
        conn,
        session_id=session["session_id"],
        snapshot_fingerprint=fingerprint,
        size=2,
        seed=17,
        char_budget=8000,
    )
    assert sample["returned"] == 2
    assert all(
        set(item)
        >= {
            "rec_number",
            "title",
            "abstract",
            "keywords",
            "container_title",
            "year",
            "ref_type",
            "authors",
            "groups",
        }
        for item in sample["references"]
    )

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
                    "labels": ["In scope"],
                }
            ],
        },
    )
    classification.approve_taxonomy(conn, session["session_id"], taxonomy["hash"])
    ruleset = classification.save_ruleset(
        conn,
        session["session_id"],
        {
            "rules": [
                {
                    "id": "published-reference",
                    "facet": "topic",
                    "label": "In scope",
                    "when": [
                        {
                            "op": "year_range",
                            "field": "year",
                            "min": 1900,
                            "max": 2100,
                        }
                    ],
                }
            ],
        },
    )
    classification.freeze_ruleset(conn, session["session_id"], ruleset["hash"])
    evaluation = classification.evaluate_rules(conn, session["session_id"], fingerprint)
    assert evaluation["assigned"] == 2

    audit = classification.create_audit_sample(conn, session["session_id"], size=2, seed=23)
    classification.submit_audit_labels(
        conn,
        audit["audit_set_id"],
        [{"rec_number": rec_number, "label": "In scope"} for rec_number in audit["rec_numbers"]],
    )
    report = classification.classification_report(conn, audit["audit_set_id"])
    assert report["accuracy"] == report["coverage"] == 1.0
    assert report["per_label"]["In scope"]["precision"] == 1.0
    assert report["per_label"]["In scope"]["recall"] == 1.0
    assert report["confusion_matrix"]["In scope"]["In scope"] == 2

    rdb, enl, before = _temporary_endnote_sources(tmp_path)
    plan = writeplan.prepare_writeback(
        conn, session["session_id"], rdb, enl, f"e2e-{cross_domain_xml.stem}"
    )
    assert plan["changes"] == 2
    after = {
        path: (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
        for path in before
    }
    assert after == before
    conn.close()
