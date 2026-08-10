"""The ref-clean skill's scripts.

These write to a real EndNote library, which makes them the least
forgiving code in the repository and, until now, the only part with no
tests at all. Every bug they have shipped was found by reading them, not by
running them: notes wiped on records nothing was recovered from, the wrong
duplicate chosen for deletion, a second run crashing on its own quarantine
folder, and — the reason a whole section below exists — CJK names and
journal titles being folded to nothing and then overwritten with whatever
CrossRef happened to say.

The scripts are imported by path rather than as a package: they are a
Claude skill, run as `python metadata.py`, and their own directory is what
puts `common`/`crossref` on the path at runtime.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / ".claude" / "skills" / "ref-clean" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import common  # noqa: E402
import crossref  # noqa: E402
import dedup  # noqa: E402
import files as files_script  # noqa: E402
import metadata  # noqa: E402

SEP = common.VALUE_SEPARATOR


def _row(rec: int, **fields) -> dict:
    """A `refs` row shaped the way `refstable.read_rows` yields them."""
    row: dict = {"id": rec}
    for key, value in fields.items():
        if isinstance(value, list):
            value = SEP.join(value)
        row[key] = value.encode("utf-8") if isinstance(value, str) else value
    return row


# --------------------------------------------------------------------------
# name and title folding
#
# `norm_name` used to fold to ASCII and nothing else, so any name without
# Latin letters became "" — and "" is a substring of every string, which
# made `same_surname` answer True for every pair. A CJK-named author
# therefore "matched" any CrossRef author, and the author list was
# overwritten wholesale.
# --------------------------------------------------------------------------


def test_a_cjk_name_does_not_match_an_unrelated_latin_one():
    assert not common.same_surname("张三, 三", "Smith")
    assert not common.same_surname("李四, 四", "Nakamura")


def test_a_cjk_name_still_matches_itself():
    assert common.same_surname("张三, 三", "张三")


def test_latin_surname_matching_tolerates_punctuation_and_accents():
    assert common.same_surname("Dall'Olio, Stefano", "DallOlio")
    assert common.same_surname("Žerovnik, Andrej", "Zerovnik")


def test_a_cjk_author_list_is_not_reported_as_truncated_against_latin_names():
    assert not common.author_list_is_truncated(
        ["张三, 三"], [("Smith", "John"), ("Doe", "Jane")]
    )


def test_cjk_titles_that_differ_do_not_fold_to_the_same_key():
    assert common.norm_title("退火对形状记忆合金 A 的影响") != common.norm_title(
        "时效对形状记忆合金 A 的影响"
    )


def test_latin_titles_still_fold_past_punctuation_and_case():
    assert common.norm_title("Elastocaloric Cooling!") == common.norm_title(
        "elastocaloric, cooling"
    )


# --------------------------------------------------------------------------
# author_list_is_truncated
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("authors", "crossref_names", "expected", "why"),
    [
        (["Zhu, Xuejie"], [("Zhu", "X"), ("Zhang", "X"), ("Qian", "M")], True, "full names, cut short"),
        (["Xu, Z.", "Ji, Y."], [("Xu", "Zhizhi"), ("Ji", "Yuanchao"), ("Liu", "C")], True, "initials, cut short"),
        (["Smith, J."], [("Wang", "Wei"), ("Li", "Ming")], False, "surnames disagree"),
        (["A, X", "B, Y", "C, Z"], [("A", "X"), ("B", "Y")], False, "never shorten a longer local list"),
        (["A, X", "B, Y"], [("A", "X"), ("B", "Y")], False, "same length is not truncation"),
        (["B, Y", "A, X"], [("A", "X"), ("B", "Y"), ("C", "Z")], False, "order disagrees"),
    ],
)
def test_truncation_judgement(authors, crossref_names, expected, why):
    assert common.author_list_is_truncated(authors, crossref_names) is expected, why


# --------------------------------------------------------------------------
# journal abbreviation detection
# --------------------------------------------------------------------------


@pytest.mark.parametrize("journal", ["材料科学", "金属学报", "机械工程学报"])
def test_a_cjk_journal_name_is_never_an_abbreviation(journal):
    """It is not abbreviated, it is just not English — and calling it one
    let CrossRef's longer English title overwrite it."""
    assert not common.looks_abbrev_journal(journal)


@pytest.mark.parametrize("journal", ["Acta Mater", "J Appl Phys", "Scr Mater"])
def test_latin_abbreviations_are_still_detected(journal):
    assert common.looks_abbrev_journal(journal)


@pytest.mark.parametrize("journal", ["Journal of Applied Physics", "Nature Materials"])
def test_full_latin_titles_are_not_abbreviations(journal):
    assert not common.looks_abbrev_journal(journal)


# --------------------------------------------------------------------------
# has_pdf
# --------------------------------------------------------------------------


def test_has_pdf_reads_a_real_attachment():
    assert common.has_pdf(_row(1, link_to_pdf="internal-pdf://123/paper.pdf"))


@pytest.mark.parametrize("junk", ["", SEP, "   ", "https://example.com/paper.pdf"])
def test_has_pdf_rejects_junk_and_bare_urls(junk):
    """A raw truthy check on the column called all of these attachments,
    which made dedup tag the copy that actually held the PDF."""
    assert not common.has_pdf(_row(1, link_to_pdf=junk))


# --------------------------------------------------------------------------
# metadata.build_updates
# --------------------------------------------------------------------------


def test_journal_from_alternate_title_does_not_wipe_unrelated_notes():
    """`notes` is only cleared when authors were recovered *from* notes."""
    rows = {
        1: _row(
            1,
            author="Smith, John",
            secondary_title="",
            alternate_title="Journal of Testing Science",
            notes="My own note. Keep this.",
        )
    }
    updates, stats, _ = metadata.build_updates(rows, {})
    assert updates[1]["journal"] == "Journal of Testing Science"
    assert "notes" not in updates[1]
    assert stats["notes_cleared"] == 0


def test_a_truncated_author_list_is_completed_from_crossref():
    rows = {1: _row(1, author="Zhu, Xuejie", electronic_resource_number="10.1/x")}
    message = {"author": [{"family": "Zhu", "given": "Xuejie"}, {"family": "Zhang", "given": "Xuexi"}]}
    updates, stats, _ = metadata.build_updates(rows, {"10.1/x": message})
    assert updates[1]["author"] == ["Zhu, Xuejie", "Zhang, Xuexi"]
    assert stats["author_completed_from_crossref"] == 1


def test_a_cjk_authored_record_is_left_for_a_human():
    rows = {1: _row(1, author="张三, 三", electronic_resource_number="10.1/x")}
    message = {"author": [{"family": "Smith", "given": "John"}, {"family": "Doe", "given": "Jane"}]}
    updates, stats, review = metadata.build_updates(rows, {"10.1/x": message})
    assert "author" not in updates.get(1, {})
    assert stats["author_completed_from_crossref"] == 0
    assert review["author_count_mismatch"]


def test_a_cjk_journal_is_not_replaced_by_the_crossref_english_title():
    rows = {1: _row(1, secondary_title="金属学报", electronic_resource_number="10.1/x")}
    message = {"container-title": ["Acta Metallurgica Sinica"]}
    updates, _stats, _ = metadata.build_updates(rows, {"10.1/x": message})
    assert "journal" not in updates.get(1, {})


def test_a_latin_abbreviation_is_still_expanded():
    rows = {1: _row(1, secondary_title="Acta Mater", electronic_resource_number="10.1/x")}
    message = {"container-title": ["Acta Materialia"]}
    updates, stats, _ = metadata.build_updates(rows, {"10.1/x": message})
    assert updates[1]["journal"] == "Acta Materialia"
    assert stats["journal_from_crossref"] == 1


def test_blank_bibliographic_fields_are_filled_and_present_ones_left_alone():
    rows = {1: _row(1, volume="12", electronic_resource_number="10.1/x")}
    message = {"volume": "99", "issue": "3", "page": "1-10"}
    updates, _stats, _ = metadata.build_updates(rows, {"10.1/x": message})
    assert "volume" not in updates[1]  # already had one
    assert updates[1]["issue"] == "3"
    assert updates[1]["pages"] == "1-10"


def test_the_xmol_import_token_is_stripped_from_keywords():
    rows = {1: _row(1, keywords=["elastocaloric", "X-MOL", "NiTi"])}
    updates, stats, _ = metadata.build_updates(rows, {})
    assert updates[1]["keywords"] == ["elastocaloric", "NiTi"]
    assert stats["keywords_xmol"] == 1


def test_a_failed_crossref_lookup_is_ignored_rather_than_written():
    rows = {1: _row(1, author="Zhu, X.", electronic_resource_number="10.1/x")}
    updates, _stats, _ = metadata.build_updates(rows, {"10.1/x": "ERROR"})
    assert updates == {}


# --------------------------------------------------------------------------
# dedup
# --------------------------------------------------------------------------


def test_the_copy_holding_the_real_pdf_is_the_one_kept():
    rows = {
        1: _row(1, link_to_pdf=SEP),  # residue, not an attachment
        2: _row(2, link_to_pdf="internal-pdf://9/paper.pdf"),
    }
    assert dedup.classify(rows, [1, 2]) == {"keep": 2, "delete": 1}


@pytest.mark.parametrize(
    "rows_spec",
    [
        {1: "internal-pdf://9/a.pdf", 2: "internal-pdf://9/b.pdf"},  # both
        {1: "", 2: ""},  # neither
    ],
)
def test_an_ambiguous_pair_is_left_for_a_human(rows_spec):
    rows = {rec: _row(rec, link_to_pdf=v) for rec, v in rows_spec.items()}
    assert dedup.classify(rows, list(rows)) is None


def test_three_way_groups_are_never_auto_resolved():
    rows = {
        1: _row(1, link_to_pdf="internal-pdf://9/a.pdf"),
        2: _row(2, link_to_pdf=""),
        3: _row(3, link_to_pdf=""),
    }
    assert dedup.classify(rows, [1, 2, 3]) is None


def test_doi_duplicates_are_confirmed_and_titles_only_are_merely_likely():
    rows = {
        1: _row(1, electronic_resource_number="10.1/x", title="A", year="2020"),
        2: _row(2, electronic_resource_number="10.1/x", title="A", year="2020"),
        3: _row(3, title="Shared Title", year="2019", author="Li, D."),
        4: _row(4, title="Shared Title", year="2019", author="Li, D."),
    }
    confirmed, likely = dedup.detect_groups(rows)
    assert [sorted(g) for g in confirmed] == [[1, 2]]
    assert [sorted(g) for g in likely] == [[3, 4]]


def test_merge_takes_fields_the_survivor_lacks_but_never_notes():
    rows = {
        1: _row(1, volume="", keywords=["a"]),
        2: _row(2, volume="7", notes="import junk", keywords=["a", "b"]),
    }
    fields = dedup.build_merge(rows, keep_rec=1, delete_rec=2)
    assert fields["volume"] == "7"
    assert fields["keywords"] == ["a", "b"]
    assert "notes" not in fields


# --------------------------------------------------------------------------
# files
# --------------------------------------------------------------------------


def test_orphaned_pdfs_are_found_and_referenced_ones_are_not(tmp_path: Path):
    pdf_dir = tmp_path / "PDF"
    (pdf_dir / "kept").mkdir(parents=True)
    (pdf_dir / "kept" / "referenced.pdf").write_bytes(b"keep")
    (pdf_dir / "orphan.pdf").write_bytes(b"junk")

    rows = {1: _row(1, link_to_pdf="internal-pdf://kept/referenced.pdf")}
    orphans, on_disk, referenced = files_script.find_orphaned_pdfs(rows, pdf_dir)

    assert [p.name for p in orphans] == ["orphan.pdf"]
    assert (on_disk, referenced) == (2, 1)


def test_a_second_run_does_not_re_flag_its_own_quarantine(tmp_path: Path):
    """Left unexcluded, run two moved files onto themselves and crashed."""
    pdf_dir = tmp_path / "PDF"
    pdf_dir.mkdir()
    (pdf_dir / "orphan.pdf").write_bytes(b"junk")
    rows: dict = {}

    orphans, _, _ = files_script.find_orphaned_pdfs(rows, pdf_dir)
    quarantine = pdf_dir / files_script.QUARANTINE_DIRNAME
    quarantine.mkdir()
    for p in orphans:
        shutil.move(str(p), str(quarantine / p.name))

    assert files_script.find_orphaned_pdfs(rows, pdf_dir)[0] == []


def test_backups_beyond_the_recency_count_and_age_are_prunable(tmp_path: Path):
    library = tmp_path / "Lib.enl"
    library.write_bytes(b"")
    backups = tmp_path / "backups"
    backups.mkdir()
    for i in range(4):
        (backups / f"Lib-2026010{i}T000000Z").mkdir()

    prunable, kept = files_script.find_excess_backups(library, keep_recent=2, age_days=0)

    assert len(kept) == 2 and len(prunable) == 2
    assert [p.name for p in kept] == ["Lib-20260103T000000Z", "Lib-20260102T000000Z"]


def test_recent_backups_are_kept_even_beyond_the_count(tmp_path: Path):
    library = tmp_path / "Lib.enl"
    library.write_bytes(b"")
    backups = tmp_path / "backups"
    backups.mkdir()
    for i in range(4):
        (backups / f"Lib-2026010{i}T000000Z").mkdir()

    prunable, kept = files_script.find_excess_backups(library, keep_recent=1, age_days=3650)

    assert prunable == [] and len(kept) == 4


# --------------------------------------------------------------------------
# crossref caching
#
# Without a cache, a dry run and its --commit are two independent sweeps of
# the API, so what gets written is not necessarily what was approved.
# --------------------------------------------------------------------------


def test_cached_answers_are_reused_without_touching_the_network(tmp_path: Path, monkeypatch):
    cache = tmp_path / "crossref_cache.json"
    cache.write_text(json.dumps({"10.1/x": {"volume": "7"}}), encoding="utf-8")

    def explode(doi, **kw):
        raise AssertionError(f"fetched {doi} despite a cache hit")

    monkeypatch.setattr(crossref, "fetch_one", explode)
    assert crossref.fetch_many(["10.1/x"], cache_path=cache) == {"10.1/x": {"volume": "7"}}


def test_only_the_missing_dois_are_fetched(tmp_path: Path, monkeypatch):
    cache = tmp_path / "crossref_cache.json"
    cache.write_text(json.dumps({"10.1/known": {"volume": "7"}}), encoding="utf-8")
    asked = []
    monkeypatch.setattr(crossref, "fetch_one", lambda doi, **kw: (asked.append(doi), (doi, {"volume": "9"}))[1])

    result = crossref.fetch_many(["10.1/known", "10.1/new"], cache_path=cache)

    assert asked == ["10.1/new"]
    assert result["10.1/known"] == {"volume": "7"}
    assert json.loads(cache.read_text(encoding="utf-8"))["10.1/new"] == {"volume": "9"}


def test_failures_are_not_cached(tmp_path: Path, monkeypatch):
    """A network failure is not an answer; caching it would make one bad
    afternoon permanent."""
    cache = tmp_path / "crossref_cache.json"
    monkeypatch.setattr(crossref, "fetch_one", lambda doi, **kw: (doi, "ERROR"))

    assert crossref.fetch_many(["10.1/x"], cache_path=cache) == {"10.1/x": "ERROR"}
    assert "10.1/x" not in json.loads(cache.read_text(encoding="utf-8"))


def test_refresh_ignores_the_cache(tmp_path: Path, monkeypatch):
    cache = tmp_path / "crossref_cache.json"
    cache.write_text(json.dumps({"10.1/x": {"volume": "old"}}), encoding="utf-8")
    monkeypatch.setattr(crossref, "fetch_one", lambda doi, **kw: (doi, {"volume": "new"}))

    result = crossref.fetch_many(["10.1/x"], cache_path=cache, refresh=True)

    assert result == {"10.1/x": {"volume": "new"}}


def test_a_damaged_cache_is_ignored_rather_than_fatal(tmp_path: Path, monkeypatch):
    cache = tmp_path / "crossref_cache.json"
    cache.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(crossref, "fetch_one", lambda doi, **kw: (doi, {"volume": "9"}))

    assert crossref.fetch_many(["10.1/x"], cache_path=cache) == {"10.1/x": {"volume": "9"}}


def test_a_final_failed_attempt_does_not_sleep(monkeypatch):
    slept = []
    monkeypatch.setattr(crossref.time, "sleep", slept.append)
    monkeypatch.setattr(
        crossref.urllib.request,
        "urlopen",
        lambda *a, **kw: (_ for _ in ()).throw(crossref.urllib.error.URLError("down")),
    )

    assert crossref.fetch_one("10.1/x", retries=3) == ("10.1/x", "ERROR")
    assert len(slept) == 2  # between the three attempts, not after the last


def test_a_made_up_contact_address_is_not_sent():
    """CrossRef's polite pool wants a real address; a fake one is worse
    than none, so the default sends no mailto at all."""
    assert "example.com" not in crossref.HEADERS["User-Agent"]
