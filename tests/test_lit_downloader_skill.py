from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "lit-downloader"
    / "scripts"
    / "download_selected.py"
)
SPEC = importlib.util.spec_from_file_location("download_selected", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)

import retrieval  # noqa: E402


def test_normalize_doi_accepts_common_forms():
    expected = "10.1016/j.jmst.2025.01.054"
    assert MODULE.normalize_doi("DOI: 10.1016/J.JMST.2025.01.054") == expected
    assert MODULE.normalize_doi("https://doi.org/10.1016/j.jmst.2025.01.054") == expected
    assert MODULE.normalize_doi("ARTN 101054\r10.1016/J.JMST.2025.01.054") == expected


def test_row_texts_never_ask_for_a_column_the_list_does_not_have():
    # EndNote X9 crashes when asked for a cell past its last column.
    class Cell:
        def __init__(self, value):
            self.value = value

        def text(self):
            return self.value

    class ListView:
        def column_count(self):
            return 3

        def get_item(self, index, subitem=0):
            assert subitem < 3, f"asked EndNote for column {subitem}"
            return Cell(["Li, D.", "2022", "Elastocaloric cooling"][subitem])

    assert MODULE._list_row_texts(ListView(), 0) == {"Li, D.", "2022", "Elastocaloric cooling"}


@pytest.mark.parametrize(
    "stored, shown",
    [
        # narrow no-break space in EndNote, plain space on Web of Science
        ("with 2 and 4" + chr(0x202F) + "at% Fe content", "with 2 and 4 at% Fe content"),
        # en dash in EndNote, hyphen on Web of Science
        ("Ti" + chr(0x2013) + "Ni-Based Alloys", "Ti-Ni-based alloys"),
        ("Ni$_2$Mn$_{1.4}$In$_{0.6}$ alloys", "Ni2Mn1.4In0.6 alloys"),
        ("Development of shape memory alloys", "DEVELOPMENT OF SHAPE MEMORY ALLOYS"),
    ],
)
def test_title_pattern_tolerates_how_wos_renders_a_title(stored, shown):
    assert MODULE.title_pattern(stored).search(shown)


def test_title_pattern_still_rejects_a_different_title():
    pattern = MODULE.title_pattern("Elastocaloric cooling of NiTi wires")
    assert not pattern.search("Elastocaloric heating of NiTi wires")


@pytest.mark.parametrize(
    "url, arrived",
    [
        ("about:blank", False),
        ("", False),
        ("https://webofscience.clarivate.cn/wos/woscc/full-record/WOS:000", False),
        ("https://links.clarivate.com/api/linkout?x=1", False),
        ("https://doi.org/10.1063/5.0006301", False),
        ("https://pubs.aip.org/aip/apl/article/116/1/1/12345", True),
        ("https://onlinelibrary.wiley.com/doi/10.1002/pssb.201700299", True),
    ],
)
def test_the_publisher_is_reached_only_past_the_wos_link_out_hops(url, arrived):
    assert MODULE.on_publisher_page(url) is arrived


def test_aip_and_rsc_pdf_links_have_their_own_selectors():
    assert "a.article-pdfLink" in MODULE.publisher_selectors("https://pubs.aip.org/aip/apl/x")
    assert 'a[href*="/articlepdf/" i]' in MODULE.publisher_selectors(
        "https://pubs.rsc.org/en/content/articlelanding/2023/sm/d2sm01291j"
    )


def test_a_closed_browser_is_recognised():
    error = RuntimeError("BrowserContext.new_page: Target page, context or browser has been closed")
    assert MODULE.browser_was_closed(error)
    assert not MODULE.browser_was_closed(RuntimeError("HTTP Error 403: Forbidden"))


class _FakeList:
    """A reference list: one (author, year, title) tuple per row, three columns."""

    def __init__(self, rows, selected=()):
        self.rows, self.selected = rows, list(selected)

    def item_count(self):
        return len(self.rows)

    def column_count(self):
        return 3

    def get_item(self, index, subitem=0):
        value = self.rows[index][subitem]

        class Cell:
            def text(self):
                return value

        return Cell()

    def send_message(self, message, start, flags):
        later = [i for i in self.selected if i > start]
        return later[0] if later else -1


_RECORDS = [
    {"id": 10, "title": "Elastocaloric cooling", "year": "2020"},
    {"id": 11, "title": "Barocaloric heating", "year": "2021"},
    {"id": 12, "title": "Magnetocaloric pumps", "year": "2022"},
]


def test_a_record_that_moved_after_an_attachment_is_found_at_its_new_row():
    view = _FakeList(
        [
            ("Li", "2021", "Barocaloric heating"),
            ("Wu", "2022", "Magnetocaloric pumps"),
            ("Li", "2020", "Elastocaloric cooling"),
        ]
    )
    # Selected at row 0; after the list re-sorted it sits at row 2.
    assert MODULE.locate_row(view, 0, _RECORDS[0], _RECORDS) == 2


def test_a_record_missing_from_the_list_is_never_guessed():
    view = _FakeList([("Li", "2021", "Barocaloric heating")])
    with pytest.raises(RuntimeError, match="no longer shown"):
        MODULE.locate_row(view, 0, _RECORDS[0], _RECORDS)


def test_selected_rows_reads_every_selected_index():
    assert MODULE.selected_rows(_FakeList(_RECORDS, selected=[1, 2])) == [1, 2]
    assert MODULE.selected_rows(_FakeList(_RECORDS)) == []


def test_title_similarity_ignores_case_and_punctuation():
    left = "Microstructure evolution: a study"
    right = "MICROSTRUCTURE EVOLUTION — A STUDY"
    assert MODULE.title_similarity(left, right) == 1.0


def test_title_similarity_finds_title_inside_first_page_text():
    title = "Microstructure evolution in a titanium alloy"
    body = "Journal header authors affiliations " + title + " Abstract much more text " * 30
    assert MODULE.title_similarity_in_text(title, body) == 1.0


def test_record_doi_reads_endnote_electronic_resource_number():
    row = {"electronic_resource_number": b"10.1000/EXAMPLE"}
    assert MODULE.record_doi(row) == "10.1000/example"


def test_filename_from_pdf_url_decodes_and_sanitizes():
    url = "https://publisher.test/path/My%20Paper%3F.pdf?token=secret"
    assert MODULE._filename_from_url(url, 7) == "My Paper_.pdf"


def test_filename_from_non_pdf_url_uses_record_number():
    assert MODULE._filename_from_url("https://publisher.test/download", 42) == "endnote-42.pdf"


def test_validate_pdf_rejects_html_before_importing_pdf_backend():
    try:
        MODULE.validate_pdf(b"<html>Access denied</html>", "10.1/test", "A title")
    except RuntimeError as exc:
        assert "%PDF-" in str(exc)
    else:
        raise AssertionError("HTML must not be accepted as a PDF")


def test_parser_rejects_no_modes_and_defaults_to_download():
    args = MODULE.build_parser().parse_args([])
    assert args.status is False
    assert args.timeout == 300
    assert args.limit == 20
    assert args.delay == 10


def test_publisher_selectors_put_specialized_before_generic():
    selectors = MODULE.publisher_selectors("https://www.sciencedirect.com/science/article/pii/x")
    assert selectors[0] == 'a[href*="/pdfft" i]'
    assert 'a[href*=".pdf" i]' in selectors


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("HTTP 429 Too Many Requests", "rate_limited"),
        ("Institutional SSO login required", "needs_user_auth"),
        ("PDF identity check failed", "identity_mismatch"),
        ("publisher selector changed", "retryable_failed"),
    ],
)
def test_classify_failure(message, expected):
    assert MODULE.classify_failure(message) == expected


def test_match_row_uses_year_to_disambiguate_duplicate_titles():
    rows = [
        {"title": b"Same title", "year": b"2024", "id": 1},
        {"title": b"Same title", "year": b"2025", "id": 2},
    ]
    assert MODULE._match_row({"Same title", "2025"}, rows)["id"] == 2


def test_job_store_is_resumable_and_redacts_urls(tmp_path):
    store = MODULE.JobStore.create(
        tmp_path,
        "Example.enl",
        [{"rec_number": 7, "title": "A paper", "doi": "10.1/example"}],
        "batch-1",
    )
    store.begin_attempt(7)
    store.transition(
        7,
        "retryable_failed",
        stage="download",
        error="failed at https://publisher.test/pdf?token=secret",
    )

    loaded = MODULE.JobStore.load(tmp_path, "batch-1")
    assert loaded.resumable_jobs()[0]["rec_number"] == 7
    assert loaded.job(7)["error"] == "failed at <url>"
    assert loaded.report_path.is_file()
    events = [
        json.loads(line) for line in loaded.events_path.read_text(encoding="utf-8").splitlines()
    ]
    assert events[-1]["to"] == "retryable_failed"


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://127.0.0.1/paper.pdf",
        "http://169.254.169.254/latest/meta-data",
        "https://example.org:8443/paper.pdf",
    ],
)
def test_oa_url_guard_rejects_unsafe_targets(url):
    with pytest.raises(ValueError):
        retrieval.validate_public_url(url, resolve_dns=False)


def test_openalex_resolver_extracts_and_deduplicates_pdf_urls(monkeypatch):
    monkeypatch.setattr(
        retrieval,
        "_json_get",
        lambda _url, _timeout: {
            "best_oa_location": {"pdf_url": "https://example.org/paper.pdf"},
            "locations": [
                {"pdf_url": "https://example.org/paper.pdf"},
                {"pdf_url": "https://archive.example/paper.pdf"},
            ],
        },
    )
    candidates = retrieval.resolve_openalex("10.1/example")
    assert [(item.provider, item.url) for item in candidates] == [
        ("openalex", "https://example.org/paper.pdf"),
        ("openalex", "https://archive.example/paper.pdf"),
    ]
