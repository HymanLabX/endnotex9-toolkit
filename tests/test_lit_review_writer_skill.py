"""The lit-review-writer skill's citation gate.

The one failure a literature review cannot survive is a citation to a paper
that is not in the corpus -- a digit typed wrong, a record number carried
over from an earlier session, or a paper the model knows from the field and
attaches a plausible number to. All three read perfectly in the draft, and
`cite_audit.py` is the only thing standing between them and the user, so it
gets the same suspicion as the lit-cleaner scripts.

Everything here is a pure function of (draft text, corpus list): no EndNote
library, no index, no network. The script is imported by path rather than as
a package, because it is a skill script run as `python cite_audit.py`.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "lit-review-writer"
    / "scripts"
    / "cite_audit.py"
)
sys.path.insert(0, str(SCRIPT.parent))

import cite_audit  # noqa: E402

CORPUS = [
    {"rec_number": 1204, "title": "Fatigue life of NiTi", "year": "2021", "depth": "fulltext"},
    {"rec_number": 88, "title": "Composition effects", "year": "2018", "depth": "abstract"},
    {"rec_number": 7, "title": "A conference abstract", "year": "2015", "depth": "metadata"},
]


def _records(entries=CORPUS):
    records, _ = cite_audit.parse_corpus(entries)
    return records


# --------------------------------------------------------------------------
# the corpus file -- three shapes, because all three end up on disk
# --------------------------------------------------------------------------


def test_the_list_references_reply_is_accepted_as_saved():
    # The convenient case: one page of results, saved with no editing.
    reply = {"total_matching": 2, "offset": 0, "references": [{"rec_number": 1}, {"rec_number": 2}]}
    records, duplicates = cite_audit.parse_corpus(reply)
    assert sorted(records) == [1, 2]
    assert duplicates == []


def test_a_hand_assembled_list_and_bare_numbers_both_work():
    records, _ = cite_audit.parse_corpus([{"rec_number": 5, "title": "t"}, 6, "7"])
    assert sorted(records) == [5, 6, 7]
    assert records[5]["title"] == "t"


def test_duplicates_are_reported_not_raised_and_counted_once():
    # A corpus unioned from one call per lab member collects duplicates
    # honestly; keeping both would inflate every count in the ledger.
    records, duplicates = cite_audit.parse_corpus([{"rec_number": 5}, {"rec_number": 5}])
    assert list(records) == [5]
    assert duplicates == [5]


@pytest.mark.parametrize(
    "bad",
    [
        {"total_matching": 3},
        [],
        [{"title": "no record number"}],
        [{"rec_number": "twelve"}],
        "a string",
    ],
)
def test_an_unreadable_corpus_is_refused_rather_than_half_parsed(bad):
    with pytest.raises(cite_audit.AuditError):
        cite_audit.parse_corpus(bad)


# --------------------------------------------------------------------------
# markers
# --------------------------------------------------------------------------


def test_every_marker_form_in_the_reference_doc_parses():
    draft = "a [#1204] b [#1204, #88] c [#88; #7] d [# 7 ]"
    assert cite_audit.cited_order(draft) == [1204, 88, 7]


def test_a_bibliography_number_is_not_a_record_number():
    # Without the `#`, a renumbered draft fed back in would report every
    # bibliography entry as an unknown record.
    assert cite_audit.cited_order("see [1] and [12-14] and [2,3]") == []


def test_markers_carry_the_line_they_were_written_on():
    citations = cite_audit.find_citations("first\n\nsecond [#88]\n")
    assert citations[0]["line"] == 3


# --------------------------------------------------------------------------
# the audit
# --------------------------------------------------------------------------


def test_a_citation_outside_the_corpus_fails_the_audit():
    report = cite_audit.audit("## T\n\nClaim [#1204] and claim [#999].\n", _records())
    assert report["ok"] is False
    assert report["unknown"] == [{"rec_number": 999, "line": 3}]
    assert report["cited"] == [1204]


def test_a_clean_draft_passes_and_reports_the_ledger():
    report = cite_audit.audit("## T\n\nClaim [#1204] and [#88].\n", _records())
    assert report["ok"] is True
    assert report["uncited"] == [
        {"rec_number": 7, "title": "A conference abstract", "year": "2015", "depth": "metadata"}
    ]
    assert report["coverage"] == {
        "corpus": 3,
        "cited": 2,
        "uncited": 1,
        "corpus_by_depth": {"fulltext": 1, "abstract": 1, "metadata": 1},
        "cited_by_depth": {"fulltext": 1, "abstract": 1},
    }


def test_a_missing_depth_is_reported_as_unrecorded_rather_than_assumed():
    # Silently defaulting to "fulltext" would let an unread corpus look read.
    report = cite_audit.audit("## T\n\n[#1]\n", _records([{"rec_number": 1}]))
    assert report["coverage"]["cited_by_depth"] == {"unrecorded": 1}


# --------------------------------------------------------------------------
# the uncited-block heuristic
# --------------------------------------------------------------------------


DRAFT = """# A review

**Scope:** group "X" · 3 records

## Theme A

A claim resting on two records [#1204, #88].

A paragraph with no citation at all.

```text
code [#999] is not prose
```

---

| rec | year |
|---|---|
| [#7] | 2015 |

## References

1. Zhang Y. 2021.

### 中文文献

2. 李明. 2018.
"""


def test_only_real_prose_blocks_are_flagged():
    flagged = cite_audit.blocks_without_citation(DRAFT)
    assert [item["line"] for item in flagged] == [9]
    assert flagged[0]["heading"] == "Theme A"
    assert flagged[0]["preview"].startswith("A paragraph with no citation")


def test_the_title_block_fenced_code_and_rules_are_not_prose():
    flagged = cite_audit.blocks_without_citation(DRAFT)
    previews = " ".join(item["preview"] for item in flagged)
    assert "Scope:" not in previews, (
        "front matter — before the second heading — has nothing to cite"
    )
    assert "code" not in previews, "fenced code is not prose"
    assert "---" not in previews, "a horizontal rule has nothing to cite"


def test_an_exempt_section_carries_its_subheadings_with_it():
    # `### 中文文献` sits under `## References`; a bibliography entry has no
    # business carrying a marker, and neither does its Chinese half.
    flagged = cite_audit.blocks_without_citation(DRAFT)
    assert not any("Zhang Y" in item["preview"] or "李明" in item["preview"] for item in flagged)


def test_a_table_row_with_a_marker_counts_as_cited():
    assert not any(
        item["line"] >= 17 and "rec" in item["preview"]
        for item in cite_audit.blocks_without_citation(DRAFT)
    )


def test_custom_exempt_patterns_replace_the_defaults():
    draft = "# Review\n\n## Notes\n\nfree text\n\n## References\n\n1. Zhang Y.\n"
    flagged = cite_audit.blocks_without_citation(draft, exempt=(r"notes",))
    # `Notes` is now exempt and `References` is not, which is the point of
    # the flag: the defaults are a convenience, not a policy.
    assert [item["heading"] for item in flagged] == ["References"]


# --------------------------------------------------------------------------
# renumbering
# --------------------------------------------------------------------------


def test_renumbering_follows_first_appearance_and_reports_the_order():
    text, order = cite_audit.renumber("b [#88] then a [#1204] then b again [#88]")
    assert order == [88, 1204]
    assert text == "b [1] then a [2] then b again [1]"


def test_a_multi_record_marker_is_sorted_and_runs_are_collapsed():
    draft = "[#1] [#2] [#3] [#4] all at once [#4, #2, #1, #3] and a pair [#1, #3]"
    text, _ = cite_audit.renumber(draft)
    assert text.endswith("all at once [1-4] and a pair [1,3]")


def test_renumbering_is_stable_on_its_own_output():
    once, _ = cite_audit.renumber("a [#88] b [#1204]")
    twice, order = cite_audit.renumber(once)
    assert twice == once
    assert order == [], "numbered markers must not be re-read as record numbers"


def test_cjk_text_around_the_markers_survives_renumbering():
    # Fullwidth punctuation on purpose (hence the noqa): it is what a Chinese
    # review actually contains, and it is the half most likely to be mangled.
    draft = "在 4% 应变下测得 ΔT_ad 为 12.5 K［见 PDF 第 5 页］[#1204]。"  # noqa: RUF001
    text, order = cite_audit.renumber(draft)
    assert order == [1204]
    assert text == "在 4% 应变下测得 ΔT_ad 为 12.5 K［见 PDF 第 5 页］[1]。"  # noqa: RUF001


# --------------------------------------------------------------------------
# the CLI
# --------------------------------------------------------------------------


def _run(tmp_path: Path, draft: str, *extra: str, corpus=CORPUS):
    draft_path = tmp_path / "review.md"
    corpus_path = tmp_path / "corpus.json"
    draft_path.write_text(draft, encoding="utf-8")
    corpus_path.write_text(json.dumps(corpus), encoding="utf-8")
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--draft",
            str(draft_path),
            "--corpus",
            str(corpus_path),
            *extra,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def test_cli_exits_zero_and_emits_the_report_on_a_clean_draft(tmp_path):
    proc = _run(tmp_path, "## T\n\nA claim [#1204] and [#88].\n", "--json")
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout)
    assert report["ok"] is True
    assert report["unknown"] == []
    assert report["coverage"]["uncited"] == 1


def test_cli_exits_nonzero_when_a_citation_leaves_the_corpus(tmp_path):
    proc = _run(tmp_path, "## T\n\nA claim [#999].\n")
    assert proc.returncode == 1
    assert "outside the corpus" in proc.stdout + proc.stderr


def test_cli_refuses_to_renumber_a_draft_that_fails_the_audit(tmp_path):
    out = tmp_path / "numbered.md"
    proc = _run(tmp_path, "## T\n\n[#1204] and [#999]\n", "--renumber", str(out))
    assert proc.returncode != 0
    assert not out.exists(), "a failing draft must not leave a numbered copy behind"


def test_cli_writes_the_renumbered_copy_and_the_bibliography_order(tmp_path):
    out = tmp_path / "numbered.md"
    proc = _run(tmp_path, "## T\n\n[#88] then [#1204]\n", "--renumber", str(out), "--json")
    assert proc.returncode == 0, proc.stderr
    assert out.read_text(encoding="utf-8") == "## T\n\n[1] then [2]\n"
    assert json.loads(proc.stdout)["renumbered"]["order"] == [88, 1204]


def test_cli_strict_turns_uncited_blocks_into_a_failure(tmp_path):
    draft = "# Review\n\n## T\n\nA claim [#1204].\n\nAn unsupported paragraph.\n"
    assert _run(tmp_path, draft).returncode == 0
    assert _run(tmp_path, draft, "--strict").returncode == 1


def test_cli_reports_a_broken_corpus_file_instead_of_traversing_it(tmp_path):
    draft_path = tmp_path / "review.md"
    corpus_path = tmp_path / "corpus.json"
    draft_path.write_text("## T\n\n[#1]\n", encoding="utf-8")
    corpus_path.write_text("{not json", encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--draft", str(draft_path), "--corpus", str(corpus_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert proc.returncode != 0
    assert "not valid JSON" in proc.stderr
