"""The lit-downloader collection pipeline: search export -> verified PDFs -> EndNote import file.

The scripts are imported by path, the way they run (`python <script>.py <project>`). Everything
here works on synthetic projects in temporary folders; nothing opens a browser or a network
connection, and the library is faked at the one function that reads it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS = (
    Path(__file__).resolve().parents[1] / "skills" / "lit-downloader" / "scripts" / "collection"
)
sys.path.insert(0, str(SCRIPTS))

import build_import  # noqa: E402
import collection_common as cc  # noqa: E402
import merge  # noqa: E402
import oa  # noqa: E402
import open_links  # noqa: E402
import status  # noqa: E402
import trim  # noqa: E402
import verify_import  # noqa: E402
import watch  # noqa: E402

from endnotex9_toolkit.pdf_identity import MissingDoiError  # noqa: E402

RULES = SCRIPTS / "examples" / "rules_ti_sma.py"


def _rec(**tags) -> list[list[str]]:
    rec = [["TY", tags.pop("TY", "JOUR")]]
    for tag, value in tags.items():
        for v in value if isinstance(value, list) else [value]:
            rec.append([tag, v])
    return [*rec, ["ER", ""]]


def _project(tmp_path: Path, recs, **cfg) -> cc.Project:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "project.json").write_text(json.dumps(cfg), encoding="utf-8")
    cc.write_ris(root / "list.ris", recs)
    return cc.load_project(root)


# --- RIS ----------------------------------------------------------------------------------------


def test_ris_round_trip_joins_continuation_lines_and_drops_the_bom(tmp_path):
    text = "﻿TY  - JOUR\r\nTI  - A long\r\n  title\r\nDO  - 10.1/X\r\nER  - \r\n"
    recs = cc.parse_ris(text)
    assert cc.rec_title(recs[0]) == "A long title"
    assert cc.rec_doi(recs[0]) == "10.1/x"
    cc.write_ris(tmp_path / "out.ris", recs)
    raw = (tmp_path / "out.ris").read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert raw.count(b"\n") == raw.count(b"\r\n")  # CRLF only, as EndNote's RIS filter expects
    assert raw.startswith(b"TY  - JOUR\r\nTI  - A long title\r\n")


def test_unknown_project_keys_are_refused(tmp_path):
    (tmp_path / "project.json").write_text('{"lsit": "x.ris"}', encoding="utf-8")
    with pytest.raises(SystemExit, match="unknown keys"):
        cc.load_project(tmp_path)


def test_folder_numbers_are_stable_ten_digit_and_vary_by_attempt():
    first = cc.folder_number("10.1016/j.actamat.2020.01.001")
    assert first == cc.folder_number("10.1016/j.actamat.2020.01.001")
    assert len(first) == 10 and first.isdigit()
    assert cc.folder_number("10.1016/j.actamat.2020.01.001", 1) != first


def test_chinese_and_unknown_titles_are_not_silently_dropped():
    recs = [_rec(TI="退火对材料性能的影响"), _rec(TI="时效对材料性能的影响"), _rec(), _rec()]
    assert len(merge.dedupe(recs)) == 4
    assert cc.norm_title("退火对材料性能的影响")


@pytest.mark.parametrize(
    "message", ["not a PDF", "could not be parsed", "title similarity is too low"]
)
def test_jstage_url_never_bypasses_pdf_validation(message):
    def reject(*args):
        raise RuntimeError(message)

    with pytest.raises(RuntimeError, match=message):
        oa.identify(
            reject,
            b"%PDF-invalid",
            "10.2320/matertrans.47.1605",
            "A title",
            "https://www.jstage.jst.go.jp/article/matertrans/47/6/47_6_1605/_pdf",
        )


def test_author_version_still_requires_parsing_and_near_exact_title():
    def validate(data, doi, title):
        if doi:
            raise MissingDoiError("DOI was not found")
        return {"title_similarity": 0.95, "pages": 4, "sha256": "synthetic"}

    info, suffix = oa.identify(validate, b"synthetic", "10.1/a", "A title", "https://example.org/a")
    assert suffix == "-author-version" and info["doi_match"] is False


# --- merge and trim -----------------------------------------------------------------------------


def test_merge_drops_batch_duplicates_and_records_already_held():
    raw = [
        _rec(TI="Superelasticity of Ti-Nb alloys", DO="10.1/a"),
        _rec(TI="Superelasticity of Ti-Nb alloys", DO="10.1/A"),
        _rec(TI="Shape memory in Ti-Zr-Nb", DO="10.1/b"),
    ]
    rows = merge.merge(raw, merge.load_rules(None), {"10.1/b"}, set())
    assert [r["tier"] for r in rows] == ["A", "DUP"]


@pytest.mark.parametrize(
    "title, tier",
    [
        ("Superelasticity of a Ni-free Ti-24Nb-4Zr alloy", "A"),
        ("Shape memory effect in NiTi wires", "X"),
        ("Fatigue of Ti-6Al-4V welds", "X"),
        # Needs the word boundary: an earlier copy of these rules had a stray backspace there.
        ("Abnormal martensitic transformation of high Zr-containing Ti alloys", "B"),
    ],
)
def test_example_rules_sort_titles_into_tiers(title, tier):
    classify = merge.load_rules(str(RULES))
    assert classify({"TI": [title]})[0] == tier


def _trim_args(**kw):
    defaults = {"journal_only": False, "languages": set(), "old_lowcite": "", "drop_errata": False}
    return type("Args", (), {**defaults, **kw})


def test_trim_reasons_cover_type_language_old_lowcite_and_errata():
    note = "Times Cited in Web of Science Core Collection:  3"
    args = _trim_args(
        journal_only=True, languages={"english", "chinese"}, old_lowcite="2015:5", drop_errata=True
    )
    assert trim.reasons(_rec(TY="CPAPER", TI="x"), args) == ["type CPAPER"]
    assert trim.reasons(_rec(TI="x", LA="Japanese"), args) == ["language Japanese"]
    assert trim.reasons(_rec(TI="x", PY="2012", N1=note), args) == ["2012, cited 3"]
    assert trim.reasons(_rec(TI="x", PY="2016", N1=note), args) == []
    errata = "Superelasticity in metastable Ti alloys (vol 853, 157090, 2021)"
    assert trim.reasons(_rec(TI=errata), args) == ["erratum"]
    assert trim.reasons(_rec(TI="A study of (vol) fractions in Ti alloys"), args) == []


# --- opener -------------------------------------------------------------------------------------


def test_elsevier_links_need_a_real_pii():
    good = {"alternative-id": ["S1359645420300011"]}
    url = open_links.url_for("10.1016/j.actamat.2020.01.001", good, [], [])
    assert url == "https://www.sciencedirect.com/science/article/pii/S1359645420300011/pdf"
    moved = {"alternative-id": ["10.1080/14686996.2020.1"]}
    assert open_links.url_for("10.1016/j.x.2020.1", moved, [], []) == ""


def test_repository_copies_come_first_unless_blocked_or_fully_open_access():
    repo = ["https://hal.science/hal-01/document"]
    msg = {"link": [{"content-type": "application/pdf", "URL": "http://pub.example/x.pdf"}]}
    assert open_links.url_for("10.1007/s1", msg, repo, []) == repo[0]
    assert (
        open_links.url_for("10.1007/s1", msg, repo, ["hal.science"]) == "https://pub.example/x.pdf"
    )
    mdpi = {"resource": {"primary": {"URL": "https://www.mdpi.com/1996-1944/1/1/1"}}}
    assert open_links.url_for("10.3390/ma1", mdpi, repo, []).endswith("/1/1/1/pdf")


@pytest.mark.parametrize(
    "title, blocked",
    [
        ("Just a moment... - Google Chrome", True),
        ("请稍候… - Google Chrome", True),
        ("journals.sagepub.com - Google Chrome", True),  # a challenge that never set a title
        ("Superelastic Ti alloys | Acta Materialia - Google Chrome", False),
    ],
)
def test_block_pages_are_recognised(title, blocked):
    assert (
        open_links.is_block_page(title, "https://journals.sagepub.com/doi/pdf/10.1177/1") is blocked
    )


def test_daily_rounds_skip_failures_and_retries_never_open_a_third_time():
    dois = ["10.1016/a", "10.1016/b", "10.1007/c", "10.1080/d"]
    misses = {"10.1016/b": 1, "10.1007/c": 2}
    base = {"exclude": ["10.1080"], "include_untried": False, "max_tries": 2, "interleave": False}
    daily = type("A", (), {**base, "skip_missed": True, "only_missed": False})
    retry = type("A", (), {**base, "skip_missed": False, "only_missed": True})
    assert open_links.select(dois, set(), misses, daily) == ["10.1016/a"]
    assert open_links.select(dois, set(), misses, retry) == ["10.1016/b"]


def test_interleaving_alternates_publishers():
    args = type(
        "A", (), {"exclude": [], "skip_missed": False, "only_missed": False, "interleave": True}
    )
    out = open_links.select(["10.1016/a", "10.1016/b", "10.1007/c"], set(), {}, args)
    assert out == ["10.1016/a", "10.1007/c", "10.1016/b"]


# --- OA stage -----------------------------------------------------------------------------------


def test_oa_skips_urls_already_tried_and_publisher_hosts():
    found = {
        "10.1/a": [
            ("openalex", "https://repo.example/a.pdf"),
            ("openalex", "https://www.sciencedirect.com/x"),
        ],
        "10.1/b": [("semanticscholar", "https://arxiv.org/pdf/2001.00001")],
    }
    state = {"10.1/b": {"status": "oa_failed", "tried": ["arxiv.org/pdf/2001.00001"]}}
    assert oa.new_candidates(found, state, {}) == {
        "10.1/a": [("openalex", "https://repo.example/a.pdf")]
    }


def test_oa_treats_urls_cached_by_an_older_run_as_tried():
    found = {"10.1/a": [("openalex", "https://repo.example/a.pdf")]}
    state = {"10.1/a": {"status": "oa_failed"}}  # older runs kept no `tried` list
    caches = {"openalex_urls.json": {"10.1/a": ["https://repo.example/a.pdf"]}}
    assert oa.new_candidates(found, state, caches) == {}


def test_oa_fetcher_refuses_a_private_host_before_connecting():
    # A landing page's <meta citation_pdf_url> may name any host, the local network included.
    with pytest.raises(ValueError):
        oa.Fetcher(host_gap=0)("http://127.0.0.1/paper.pdf")


# --- watcher ------------------------------------------------------------------------------------


def test_watcher_names_files_by_pii_doi_suffix_and_arxiv_id(tmp_path):
    project = _project(
        tmp_path,
        [
            _rec(TI="One", DO="10.1016/j.actamat.2020.01.001"),
            _rec(TI="Two", DO="10.1007/s11661-019-05123-4"),
            _rec(TI="Three", DO="10.1103/physrevb.1.1"),
        ],
    )
    project.cache_dir.mkdir()
    (project.cache_dir / "10.1016_j.actamat.2020.01.001.json").write_text(
        '{"alternative-id": ["S1359645420300011"]}', encoding="utf-8"
    )
    (project.dir / "s2_urls.json").write_text(
        '{"10.1103/physrevb.1.1": "https://arxiv.org/pdf/2001.01234"}', encoding="utf-8"
    )
    m = watch.Matcher(project)
    assert m.from_name("1-s2.0-S1359645420300011-main.pdf") == (
        "10.1016/j.actamat.2020.01.001",
        False,
    )
    assert m.from_name("s11661-019-05123-4 (1).pdf") == ("10.1007/s11661-019-05123-4", False)
    assert m.from_name("2001.01234v2.pdf") == ("10.1103/physrevb.1.1", True)
    assert m.from_name("download.pdf") == (None, False)


# --- status, import file, verification ----------------------------------------------------------


def _with_pdf(project: cc.Project, doi: str, data: bytes = b"%PDF-1.4 x") -> str:
    project.pdf_dir.mkdir(exist_ok=True)
    name = cc.safe_name(doi) + ".pdf"
    (project.pdf_dir / name).write_bytes(data)
    state = cc.read_json(project.dir / cc.STATE_BROWSER)
    state[doi] = {"status": "ok", "file": name, "source": "manual-browser"}
    cc.write_json_atomic(project.dir / cc.STATE_BROWSER, state)
    return name


def test_status_sorts_missing_records_by_cause(tmp_path):
    recs = [
        _rec(TI="has pdf", DO="10.1016/a"),
        _rec(TI="failed once", DO="10.1016/b"),
        _rec(TI="no link", DO="10.1103/c"),
        _rec(TI="no doi"),
    ]
    project = _project(tmp_path, recs)
    _with_pdf(project, "10.1016/a")
    (project.dir / cc.OPEN_LOG).write_text(
        json.dumps(
            {
                "doi": "10.1016/b",
                "url": "https://www.sciencedirect.com/x",
                "got": False,
                "date": "2026-01-01",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    s = status.summarize(project)
    assert s["with_pdf"] == 1
    assert s["missing"] == {
        "no DOI": 1,
        "no link": 1,
        "never tried": 0,
        "failed once": 1,
        "failed twice+": 0,
    }


@pytest.fixture
def fake_library(tmp_path, monkeypatch):
    """A library folder plus the records `library_references` would read from it."""
    enl = tmp_path / "Lib.enl"
    enl.write_bytes(b"")
    pdf_root = tmp_path / "Lib.Data" / "PDF"
    pdf_root.mkdir(parents=True)
    refs: list[dict] = []
    for module in (build_import, verify_import):
        monkeypatch.setattr(module, "data_dir", lambda _enl: tmp_path / "Lib.Data")
        monkeypatch.setattr(module, "library_references", lambda _enl: refs)
    return enl, pdf_root, refs


def test_import_file_links_copied_pdfs_and_leaves_held_records_out(tmp_path, fake_library):
    enl, pdf_root, refs = fake_library
    recs = [
        _rec(TI="New with PDF", DO="10.1/new", T2="ACTA MATERIALIA"),
        _rec(TI="Already in the library", DO="10.1/old"),
        _rec(TI="A record without any DOI at all"),
    ]
    project = _project(tmp_path, recs, label="Batch-1")
    name = _with_pdf(project, "10.1/new")
    project.cache_dir.mkdir()
    (project.cache_dir / "10.1_new.json").write_text(
        '{"container-title": ["Acta Materialia"]}', encoding="utf-8"
    )
    refs.append({"rec_number": 7, "doi": "10.1/old", "title": "x", "pdf_files": []})
    # The DOI's own folder number is taken by an unrelated attachment: the next one is used.
    taken = cc.folder_number("10.1/new")
    (pdf_root / taken).mkdir()
    (pdf_root / taken / "someone-else.pdf").write_bytes(b"%PDF")

    assert build_import.main([str(project.dir), "--library", str(enl), "--journal-case"]) == 0
    out = cc.read_ris(project.dir / "import.ris")
    assert [cc.rec_title(r) for r in out] == ["New with PDF", "A record without any DOI at all"]
    folder = cc.folder_number("10.1/new", 1)
    assert cc.get(out[0], "L1") == [f"internal-pdf://{folder}/{name}"]
    assert cc.get(out[0], "LB") == ["Batch-1"] and cc.get(out[0], "T2") == ["Acta Materialia"]
    assert (pdf_root / folder / name).read_bytes() == b"%PDF-1.4 x"
    manifest = json.loads((project.dir / "import_manifest.json").read_text(encoding="utf-8"))
    assert manifest["attached"] == {"10.1/new": f"{folder}/{name}"}
    assert manifest["left_out_held"] == ["10.1/old"]

    # After the user's import: the record is there with its link, and nothing is doubled.
    refs.append(
        {
            "rec_number": 8,
            "doi": "10.1/new",
            "title": "New with PDF",
            "pdf_files": [f"{folder}/{name}"],
        }
    )
    result = verify_import.check(project, str(enl))
    assert (result["present"], result["expected"]) == (1, 1)
    assert result["attached_linked"] == 1 and not result["imported_twice"]


def test_a_second_build_after_import_adds_nothing(tmp_path, fake_library):
    enl, _, refs = fake_library
    project = _project(tmp_path, [_rec(TI="A record without any DOI at all")])
    refs.append(
        {"rec_number": 9, "doi": "", "title": "A record without any DOI at all", "pdf_files": []}
    )
    assert build_import.main([str(project.dir), "--library", str(enl)]) == 0
    assert cc.read_ris(project.dir / "import.ris") == []


def test_orphan_folders_are_only_this_projects_unreferenced_copies(tmp_path, fake_library):
    _, pdf_root, _ = fake_library
    project = _project(tmp_path, [])
    name = _with_pdf(project, "10.1/removed")
    ours = pdf_root / cc.folder_number("10.1/removed")
    ours.mkdir()
    (ours / name).write_bytes(b"%PDF")
    referenced = pdf_root / "1234567890"
    referenced.mkdir()
    stale = build_import.orphans(project, pdf_root, {"1234567890"}, set())
    assert stale == [{"folder": ours.name, "doi": "10.1/removed", "file": name}]
