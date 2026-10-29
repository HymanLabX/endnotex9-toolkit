from endnotex9_toolkit import db, sampling


def _conn(tmp_path):
    conn = db.connect(tmp_path / "sampling.db")
    for rec in range(1, 6):
        db.upsert_reference(
            conn,
            {
                "rec_number": rec,
                "title": f"Title {rec}",
                "abstract": "A" * 100,
                "container_title": "Journal A" if rec < 4 else "Journal B",
                "year": str(2020 + rec),
                "keywords": [f"k{rec}"],
                "authors": [f"Author {rec}"],
                "ref_type": "Journal Article",
            },
        )
    conn.commit()
    return conn


def test_random_sampling_is_reproducible_and_sorted(tmp_path):
    conn = _conn(tmp_path)
    a = sampling.sample_references(conn, size=3, seed=42)
    b = sampling.sample_references(conn, size=3, seed=42)
    assert [x["rec_number"] for x in a["references"]] == [x["rec_number"] for x in b["references"]]
    assert [x["rec_number"] for x in a["references"]] == sorted(
        x["rec_number"] for x in a["references"]
    )
    assert set(a["references"][0]) >= {
        "rec_number",
        "title",
        "abstract",
        "keywords",
        "container_title",
        "year",
        "ref_type",
        "authors",
        "groups",
        "truncated_fields",
    }
    conn.close()


def test_group_and_journal_sampling_and_uncertain_precondition(tmp_path):
    conn = _conn(tmp_path)
    cur = conn.execute("INSERT INTO groups (name) VALUES ('Reading')")
    conn.execute(
        "INSERT INTO group_members (group_id, rec_number) VALUES (?, ?)", (cur.lastrowid, 1)
    )
    conn.commit()
    assert (
        sampling.sample_references(conn, strategy="group", group="reading", size=10)["returned"]
        == 1
    )
    assert (
        sampling.sample_references(conn, strategy="journal", journal="journal b", size=10)[
            "returned"
        ]
        == 2
    )
    result = sampling.sample_references(conn, strategy="uncertain")
    assert result["errors"][0]["code"] == "precondition_failed"
    conn.close()


def test_budget_sets_truncation_marker(tmp_path):
    conn = _conn(tmp_path)
    result = sampling.sample_references(conn, size=5, char_budget=700)
    assert result["truncated"]
    assert result["returned"] < 5 or any(item["truncated_fields"] for item in result["references"])
    conn.close()


def test_sampling_limits_are_bounded(tmp_path):
    conn = _conn(tmp_path)
    assert sampling.sample_references(conn, size=501)["errors"][0]["code"] == "invalid_size"
    assert sampling.sample_references(conn, char_budget=10)["errors"][0]["code"] == "invalid_budget"
    conn.close()
