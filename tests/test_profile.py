from endnotex9_toolkit import db, profile


def _conn(tmp_path):
    conn = db.connect(tmp_path / "profile.db")
    for rec, title, abstract, journal, keywords in (
        (1, "Alpha beta study", "alpha text", "Journal A", ["Alpha", "Shared"]),
        (2, "Alpha gamma study", "gamma text", "Journal A", ["Alpha", "Shared"]),
        (3, "History of gamma", "", "Journal B", ["Gamma"]),
    ):
        db.upsert_reference(
            conn,
            {
                "rec_number": rec,
                "title": title,
                "abstract": abstract,
                "container_title": journal,
                "year": "2020",
                "keywords": keywords,
                "authors": ["Author"],
                "ref_type": "Journal Article",
            },
        )
    conn.commit()
    return conn


def test_profile_has_coverage_terms_cooccurrence_and_noise(tmp_path):
    conn = _conn(tmp_path)
    group = conn.execute("INSERT INTO groups (name) VALUES ('Priority')")
    conn.execute(
        "INSERT INTO group_members (group_id, rec_number) VALUES (?, ?)",
        (group.lastrowid, 1),
    )
    conn.commit()
    result = profile.profile_library(conn, top=5, min_df=2, max_terms=20)
    assert result["total_references"] == 3
    assert result["coverage"]["abstract"]["documents"] == 2
    assert result["document_frequency"][0]["term"] == "alpha"
    assert {"left": "alpha", "right": "shared", "count": 2} in result["keyword_cooccurrence"]
    assert result["journal_enrichment"][0]["name"] == "Journal A"
    assert result["group_enrichment"][0]["name"] == "Priority"
    assert result["group_enrichment"][0]["coverage"] == round(1 / 3, 6)
    assert result["errors"] == []
    conn.close()


def test_profile_bad_json_is_a_row_error_not_a_failure(tmp_path):
    conn = db.connect(tmp_path / "bad.db")
    conn.execute(
        "INSERT INTO refs (rec_number, title, keywords) VALUES (?, ?, ?)", (1, "Title", "{bad json")
    )
    conn.commit()
    result = profile.profile_library(conn)
    assert result["total_references"] == 1
    assert result["document_frequency"] == []
    assert result["errors"][0]["error"] == "invalid_json"
    conn.close()
