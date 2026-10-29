"""Download verified PDFs for selected EndNote records and attach them natively.

The durable job ledger and OA retrieval are separate modules.  This entry point owns
the fragile live boundaries: EndNote selection, the visible institutional browser,
response-stage CDP capture, and EndNote's Attach File dialog.
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import unquote, urlparse

SCRIPT_DIR = Path(__file__).resolve().parent


def _package_root() -> Path:
    """The endnotex9-toolkit tree this skill ships inside.

    Do not count parent directories here. The same script runs from a source
    checkout and from an installed plugin, where Claude Code has copied the
    plugin somewhere else entirely, so walk up looking for the package instead
    of assuming a depth.
    """
    for parent in Path(__file__).resolve().parents:
        if (parent / "src" / "endnotex9_toolkit" / "__init__.py").is_file():
            return parent
    raise SystemExit(
        "Cannot find endnotex9_toolkit. Run this script from an endnotex9-toolkit checkout "
        "or plugin directory."
    )


REPO_ROOT = _package_root()
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(REPO_ROOT / "src"))

from job_store import JobStore  # noqa: E402
from retrieval import download_pdf, resolve_oa_candidates  # noqa: E402

from endnotex9_toolkit import config as configmod  # noqa: E402
from endnotex9_toolkit import groups, parser, refstable  # noqa: E402

MAX_CAPTURE_BYTES = 200 * 1024 * 1024

PUBLISHER_SELECTORS = (
    (
        ("sciencedirect.com", "elsevier.com"),
        ('a[href*="/pdfft" i]', 'a[href*="/pdf" i]', 'a[data-aa-name*="pdf" i]'),
    ),
    (
        ("onlinelibrary.wiley.com",),
        ('a[href*="pdfdirect" i]', 'a[href*="/epdf/" i]', 'a[href*="/pdf/" i]'),
    ),
    (
        ("link.springer.com", "nature.com"),
        ('a[data-track-action*="download pdf" i]', 'a[href*="/content/pdf/" i]'),
    ),
    (
        ("tandfonline.com",),
        ('a[href*="/doi/pdf/" i]', 'a[href*="/doi/epdf/" i]'),
    ),
    (
        ("ieeexplore.ieee.org",),
        ('a[href*="stampPDF" i]', 'a[href*="document/" i][href*="pdf" i]'),
    ),
    (
        ("journals.aps.org",),
        ('a[href*="/pdf/" i]',),
    ),
    (
        ("pubs.aip.org",),
        ("a.article-pdfLink", 'a[href*="/article-pdf/" i]'),
    ),
    (
        ("pubs.rsc.org",),
        ('a[href*="/articlepdf/" i]',),
    ),
)

GENERIC_PDF_SELECTORS = (
    'a[href*=".pdf" i]',
    'a[href*="/pdf" i]',
    'a[aria-label*="pdf" i]',
    'button[aria-label*="pdf" i]',
)


def text(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value or "")


def normalize_doi(value: object) -> str:
    value = parser.normalize_doi(text(value)).casefold()
    value = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi\s*:\s*)", "", value)
    return value.rstrip(". ")


def normalize_title(value: object) -> str:
    return " ".join(re.findall(r"[\w]+", text(value).casefold(), flags=re.UNICODE))


def title_similarity(left: object, right: object) -> float:
    return SequenceMatcher(None, normalize_title(left), normalize_title(right)).ratio()


def title_similarity_in_text(title: object, body: object) -> float:
    """Compare against similarly sized windows, not an entire first page."""
    needle = normalize_title(title)
    haystack = normalize_title(body)
    if not needle or not haystack:
        return 0.0
    if needle in haystack:
        return 1.0
    title_words = needle.split()
    body_words = haystack.split()
    width = len(title_words)
    if not width:
        return 0.0
    candidates = []
    for window_width in {max(1, width - 2), width, width + 2}:
        for start in range(0, max(1, min(len(body_words) - window_width + 1, 300))):
            candidates.append(" ".join(body_words[start : start + window_width]))
    return max(
        (SequenceMatcher(None, needle, candidate).ratio() for candidate in candidates), default=0
    )


def record_doi(row: dict) -> str:
    return normalize_doi(row.get("electronic_resource_number", ""))


def load_rows() -> tuple[object, Path, list[dict]]:
    cfg = configmod.load_config()
    if not cfg.library_path:
        raise RuntimeError("No EndNote library is configured or currently open.")
    rdb_dir = groups.rdb_dir(cfg.library_path)
    if not (rdb_dir / "refs.MYD").is_file():
        raise RuntimeError(f"EndNote library is not readable: {cfg.library_path}")
    return cfg, rdb_dir, list(refstable.read_rows(rdb_dir))


def _endnote_window():
    if os.name != "nt":
        raise RuntimeError("Live EndNote attachment is supported on Windows only.")
    try:
        from pywinauto import Application, Desktop
    except ImportError as exc:
        raise RuntimeError("Install the lit-downloader extra (pywinauto is missing).") from exc

    windows = [
        window
        for window in Desktop(backend="win32").windows()
        if window.is_visible()
        and window.window_text().startswith("EndNote X9 - [")
        and window.process_id()
    ]
    if len(windows) != 1:
        raise RuntimeError(f"Expected one visible EndNote X9 library window; found {len(windows)}.")
    return (
        Application(backend="win32")
        .connect(process=windows[0].process_id())
        .window(handle=windows[0].handle)
    )


def visible_texts(window) -> set[str]:
    labels: set[str] = set()
    for control in window.descendants():
        try:
            label = control.window_text().strip()
        except Exception:
            continue
        if label:
            labels.add(label)
    return labels


def _list_row_texts(list_view, index: int) -> set[str]:
    """Cell texts of one row, reading only the columns the list really has.

    EndNote fills its reference list on demand, so every cell read runs
    EndNote's own code. Probing a fixed 16 subitems of a 7-column list
    crashed EndNote X9 (Bld 12062) twice in a row with an access violation
    inside EndNote.exe; reading only the columns the header reports did not.
    The header answers that count without calling into EndNote at all.
    """
    values: set[str] = set()
    for subitem in range(max(1, list_view.column_count())):
        try:
            item = list_view.get_item(index, subitem)
            value = item.text().strip()
        except Exception:
            continue
        if value:
            values.add(value)
    return values


def _match_row(cells: set[str], rows: list[dict]) -> dict | None:
    normalized_cells = {normalize_title(cell) for cell in cells if cell}
    candidates = [row for row in rows if normalize_title(row.get("title")) in normalized_cells]
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        years = {text(cell).strip() for cell in cells}
        narrowed = [row for row in candidates if text(row.get("year")).strip() in years]
        if len(narrowed) == 1:
            return narrowed[0]
    return None


def selected_records(window, rows: list[dict], *, allow_many: bool) -> tuple[list[dict], object]:
    """Resolve one or more selected ListView rows without guessing from all missing PDFs."""
    list_view = window.child_window(control_id=1024, class_name="SysListView32").wrapper_object()
    indices = [i for i in range(list_view.item_count()) if list_view.get_item(i).is_selected()]
    if not indices:
        raise RuntimeError("Select at least one EndNote reference.")
    if not allow_many and len(indices) != 1:
        raise RuntimeError(
            f"Select exactly one EndNote reference (currently selected: {len(indices)})."
        )

    selected: list[dict] = []
    labels = visible_texts(window) if len(indices) == 1 else set()
    for index in indices:
        row = _match_row(_list_row_texts(list_view, index), rows)
        if row is None and labels:
            fallback = [item for item in rows if text(item.get("title")).strip() in labels]
            row = fallback[0] if len(fallback) == 1 else None
        if row is None:
            raise RuntimeError(
                "Could not uniquely match every selected EndNote row. Keep the title column "
                "visible; duplicate titles must also show the year column."
            )
        selected.append(
            {
                "rec_number": int(row["id"]),
                "title": text(row.get("title")).strip(),
                "doi": record_doi(row),
                "has_pdf": bool(refstable._pdf_files(row)),
                "ui_index": index,
                "row": row,
            }
        )
    return selected, list_view


def _click_if_visible(locator) -> bool:
    try:
        if locator.count() and locator.first.is_visible():
            locator.first.click(timeout=3000)
            return True
    except Exception:
        pass
    return False


_WOS_NO_RESULTS = re.compile(r"Your search found no results", re.I)


def title_pattern(title: object) -> re.Pattern:
    """Match a title however WoS renders its spacing, dashes and punctuation.

    Exact text never matched a title holding a narrow no-break space or an
    en dash where WoS shows a plain one, and each miss cost the full browser
    timeout. Letters and digits must still appear in the same order.
    """
    tokens = re.findall(r"[^\W_]+", text(title))
    return re.compile(r"[\W_]*".join(map(re.escape, tokens)), re.I)


def _visible(locator) -> bool:
    try:
        return bool(locator.count()) and locator.first.is_visible()
    except Exception:
        return False


def _search_wos(page, doi: str, title: str, timeout_ms: int):
    page.goto(
        "https://webofscience.clarivate.cn/wos/woscc/basic-search",
        wait_until="domcontentloaded",
        timeout=timeout_ms,
    )
    _click_if_visible(page.get_by_role("button", name=re.compile("Accept all", re.I)))

    # The form is already on screen, so a missing control is a changed page,
    # not a slow one: fail in seconds instead of the whole browser timeout.
    form_ms = min(timeout_ms, 30_000)
    # WoS remembers the last field searched, so after the first record the
    # dropdown already reads DOI and there is no "Topic" button to find.
    field_button = page.locator("button").filter(
        has_text=re.compile(r"^\s*(Topic|All Fields|DOI)\s*$")
    )
    field_button.first.wait_for(state="visible", timeout=form_ms)
    if field_button.first.inner_text().strip() != "DOI":
        field_button.first.click(timeout=form_ms)
        page.get_by_text("DOI", exact=True).last.click(timeout=form_ms)
    search_box = page.locator('input[name="search-main-box"]')
    search_box.fill(doi)
    page.locator('button[type="submit"]').click()

    result_title = page.get_by_text(title_pattern(title))
    no_results = page.get_by_text(_WOS_NO_RESULTS)
    deadline = time.monotonic() + timeout_ms / 1000
    while not _visible(result_title):
        if _visible(no_results):
            raise RuntimeError("Web of Science has no record for this DOI.")
        if time.monotonic() > deadline:
            raise RuntimeError("Web of Science did not show a result with this title.")
        page.wait_for_timeout(250)
    link = result_title.first.locator("xpath=ancestor-or-self::a[1]")
    (link if link.count() else result_title.first).click()

    # WoS labels the link-out "Full text at publisher" or, for open-access
    # records, "Free Full Text from Publisher". A record without one never
    # grows it, so this is a form-sized wait too.
    publisher = page.get_by_text(re.compile(r"full text (?:at|from) publisher", re.I))
    publisher.first.wait_for(state="visible", timeout=form_ms)
    old_pages = set(page.context.pages)
    publisher.first.click()
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        new_pages = [candidate for candidate in page.context.pages if candidate not in old_pages]
        if new_pages:
            return new_pages[-1]
        if "webofscience" not in page.url.casefold():
            return page
        page.wait_for_timeout(250)
    raise RuntimeError("Web of Science did not open the publisher full-text page.")


def _response_headers(params: dict) -> dict[str, str]:
    return {
        str(item.get("name", "")).casefold(): str(item.get("value", ""))
        for item in params.get("responseHeaders", [])
    }


def capture_pdf_response(page, click_locator, timeout_seconds: int) -> tuple[bytes, str]:
    """Capture an authorized PDF response without replaying its signed request."""
    session = page.context.new_cdp_session(page)
    done = threading.Event()
    captured: dict[str, object] = {}

    def paused(params: dict):
        request_id = params["requestId"]
        try:
            headers = _response_headers(params)
            content_type = headers.get("content-type", "").casefold()
            if "application/pdf" in content_type and "responseStatusCode" in params:
                body = session.send("Fetch.getResponseBody", {"requestId": request_id})
                payload = body["body"]
                data = base64.b64decode(payload) if body.get("base64Encoded") else payload.encode()
                if len(data) > MAX_CAPTURE_BYTES:
                    captured["error"] = "Captured PDF exceeds the 200 MB limit."
                else:
                    captured["data"] = data
                    captured["url"] = params.get("request", {}).get("url", "")
                done.set()
        finally:
            try:
                session.send("Fetch.continueResponse", {"requestId": request_id})
            except Exception:
                pass

    session.on("Fetch.requestPaused", paused)
    session.send("Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Response"}]})
    click_locator.click(timeout=30_000)
    deadline = time.monotonic() + timeout_seconds
    while not done.is_set() and time.monotonic() < deadline:
        page.wait_for_timeout(250)
    try:
        session.send("Fetch.disable")
    except Exception:
        pass
    if not done.is_set():
        raise RuntimeError("No application/pdf response was observed after opening the PDF link.")
    if captured.get("error"):
        raise RuntimeError(str(captured["error"]))
    return bytes(captured["data"]), str(captured["url"])


def publisher_selectors(url: str) -> tuple[str, ...]:
    host = (urlparse(url).hostname or "").casefold()
    specialized: list[str] = []
    for hosts, selectors in PUBLISHER_SELECTORS:
        if any(host == suffix or host.endswith(f".{suffix}") for suffix in hosts):
            specialized.extend(selectors)
    return tuple(dict.fromkeys([*specialized, *GENERIC_PDF_SELECTORS]))


_LINK_OUT_HOSTS = ("webofscience", "clarivate", "doi.org")
_CHALLENGE = re.compile(r"just a moment|verify you are human|attention required|请稍候", re.I)
_PDF_CONTROL_WAIT_SECONDS = 45


def on_publisher_page(url: str) -> bool:
    """Whether a WoS link-out tab has reached the publisher (not blank, not a redirect hop)."""
    host = (urlparse(url).hostname or "").casefold()
    return bool(host) and not any(hop in host for hop in _LINK_OUT_HOSTS)


def _wait_until(page, condition, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        page.wait_for_timeout(500)
    return condition()


def _showing_challenge(page) -> bool:
    try:
        return bool(_CHALLENGE.search(page.title())) or _visible(page.get_by_text(_CHALLENGE))
    except Exception:
        return False


def download_from_publisher(page, timeout_seconds: int) -> tuple[bytes, str]:
    # "Full text at publisher" opens a blank tab that hops through WoS's own
    # link-out pages and doi.org first. Looking for a PDF control there, once,
    # failed every record the first time this ran against a real library.
    if not _wait_until(page, lambda: on_publisher_page(page.url), timeout_seconds):
        raise RuntimeError("Web of Science's publisher link never reached the publisher.")
    page.wait_for_load_state("domcontentloaded", timeout=timeout_seconds * 1000)
    # A human-verification page is the user's to pass; give them the time.
    if _showing_challenge(page) and not _wait_until(
        page, lambda: not _showing_challenge(page), timeout_seconds
    ):
        raise RuntimeError("The publisher is showing a CAPTCHA the user has not completed.")
    _click_if_visible(page.get_by_role("button", name=re.compile("Accept|Agree", re.I)))
    candidates = [
        page.get_by_text(re.compile(r"^\s*(?:View|Download)\s+PDF\s*$", re.I)),
        page.get_by_role("link", name=re.compile("PDF", re.I)),
        *(page.locator(selector) for selector in publisher_selectors(page.url)),
    ]
    # Publisher pages draw their PDF controls with script after load.
    _wait_until(page, lambda: any(_visible(c) for c in candidates), _PDF_CONTROL_WAIT_SECONDS)
    errors = []
    for candidate in candidates:
        try:
            if candidate.count() and candidate.first.is_visible():
                return capture_pdf_response(page, candidate.first, timeout_seconds)
        except RuntimeError as exc:
            errors.append(str(exc))
    detail = errors[-1] if errors else f"no visible PDF control on {urlparse(page.url).hostname}"
    raise RuntimeError(f"The publisher page did not expose a usable PDF link: {detail}")


class BrowserRetriever:
    """Reuse one visible persistent Edge session across an entire batch."""

    def __init__(self):
        self.playwright = None
        self.context = None

    def _start(self):
        if self.context is not None:
            return
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError("Install the lit-downloader extra (playwright is missing).") from exc
        profile = configmod.config_dir() / "lit-browser-profile"
        profile.mkdir(parents=True, exist_ok=True)
        self.playwright = sync_playwright().start()
        self.context = self.playwright.chromium.launch_persistent_context(
            str(profile), channel="msedge", headless=False, accept_downloads=False
        )

    def download(self, doi: str, title: str, timeout_seconds: int) -> tuple[bytes, str]:
        self._start()
        page = self.context.pages[0] if self.context.pages else self.context.new_page()
        pages_before = set(self.context.pages)
        publisher = _search_wos(page, doi, title, timeout_seconds * 1000)
        try:
            return download_from_publisher(publisher, timeout_seconds)
        finally:
            for extra in set(self.context.pages) - pages_before:
                try:
                    extra.close()
                except Exception:
                    pass

    def close(self):
        if self.context is not None:
            self.context.close()
            self.context = None
        if self.playwright is not None:
            self.playwright.stop()
            self.playwright = None


def validate_pdf(data: bytes, doi: str, title: str) -> dict:
    if not data.startswith(b"%PDF-"):
        raise RuntimeError("Downloaded response is not a PDF (missing %PDF- signature).")
    if len(data) > MAX_CAPTURE_BYTES:
        raise RuntimeError("Downloaded PDF exceeds the 200 MB validation limit.")
    try:
        import fitz
    except ImportError as exc:
        raise RuntimeError("Install the lit-downloader extra (PyMuPDF is missing).") from exc

    document = None
    try:
        document = fitz.open(stream=data, filetype="pdf")
        if document.page_count < 1:
            raise RuntimeError("Downloaded PDF contains no pages.")
        extracted = "\n".join(page.get_text() for page in document)
        metadata_title = document.metadata.get("title", "")
        pages = document.page_count
    except Exception as exc:
        raise RuntimeError(f"Downloaded PDF could not be parsed: {exc}") from exc
    finally:
        if document is not None:
            document.close()

    doi_match = normalize_doi(doi) in extracted.casefold()
    similarity = max(
        title_similarity(title, metadata_title),
        title_similarity_in_text(title, extracted[:20_000]),
    )
    if doi and not doi_match:
        raise RuntimeError("PDF identity check failed: the selected record DOI was not found.")
    if similarity < 0.70:
        raise RuntimeError("PDF identity check failed: title similarity is too low.")
    return {
        "pages": pages,
        "doi_match": doi_match,
        "title_similarity": round(similarity, 4),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def _filename_from_url(url: str, rec_number: int) -> str:
    name = Path(unquote(urlparse(url).path)).name
    if not name.casefold().endswith(".pdf"):
        name = f"endnote-{rec_number}.pdf"
    return re.sub(r"[^\w.() -]", "_", name)


def save_staging(data: bytes, url: str, rec_number: int) -> Path:
    folder = configmod.config_dir() / "downloads" / f"rec-{rec_number}"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / _filename_from_url(url, rec_number)
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_bytes(data)
    os.replace(temporary, path)
    return path


_LVM_SETITEMSTATE = 0x1000 + 43
_LVIF_STATE = 0x0008
_LVIS_FOCUSED = 0x0001
_LVIS_SELECTED = 0x0002


class _LVItemState(ctypes.Structure):
    """The leading LVITEM fields LVM_SETITEMSTATE reads.

    They sit at the same offsets in the 32- and 64-bit layouts, so this is
    safe to hand 32-bit EndNote from 64-bit Python.
    """

    _fields_ = [
        (name, ctypes.c_uint) for name in ("mask", "iItem", "iSubItem", "state", "stateMask")
    ]


def set_row_selected(list_view, index: int, selected: bool) -> None:
    """Select or clear one list row (index -1: every row), as a click would.

    Only LVM_SETITEMSTATE is sent; the list control then notifies EndNote
    itself. pywinauto's select()/deselect() additionally forge an
    LVN_ITEMCHANGED for the parent in 64-bit NMLISTVIEW layout, which 32-bit
    EndNote reads misaligned, and clearing a selection that way meant one
    call per row: 1,236 of them before every attachment.
    """
    from pywinauto.remote_memory_block import RemoteMemoryBlock

    flags = _LVIS_FOCUSED | _LVIS_SELECTED
    item = _LVItemState(mask=_LVIF_STATE, state=flags if selected else 0, stateMask=flags)
    remote = RemoteMemoryBlock(list_view)
    try:
        remote.Write(item)
        if not list_view.send_message(_LVM_SETITEMSTATE, index, remote):
            raise RuntimeError(f"EndNote's reference list refused to change row {index}.")
    finally:
        del remote


_LVM_GETNEXTITEM = 0x1000 + 12
_LVNI_SELECTED = 0x0002


def selected_rows(list_view) -> list[int]:
    """Indices the list control reports selected (pointer-free messages only)."""
    count, found, index = list_view.item_count(), [], -1
    while len(found) <= count:
        index = list_view.send_message(_LVM_GETNEXTITEM, index, _LVNI_SELECTED)
        if not 0 <= index < count:
            break
        found.append(index)
    return found


def _row_shows(list_view, index: int, target: dict, rows: list[dict]) -> bool:
    """Whether list row `index` shows `target` and could not be another record."""
    if not 0 <= index < list_view.item_count():
        return False
    match = _match_row(_list_row_texts(list_view, index), rows)
    return match is not None and int(match["id"]) == int(target["id"])


def locate_row(list_view, hint: int, target: dict, rows: list[dict]) -> int:
    """The list row showing `target`, starting from where it was when selected.

    Attaching a PDF can move a record when the list is sorted by the
    attachment column, so a row index taken at selection time can point at a
    different record by the time it is used. Clicking it would attach a PDF
    to the wrong reference. Search outward from the old position instead.
    """
    for index in sorted(range(list_view.item_count()), key=lambda i: abs(i - hint)):
        if _row_shows(list_view, index, target, rows):
            return index
    raise RuntimeError("The record is no longer shown in EndNote's reference list.")


def _title_column(list_view, index: int, title: object) -> int:
    wanted = normalize_title(title)
    for column in range(max(1, list_view.column_count())):
        try:
            if normalize_title(list_view.get_item(index, column).text()) == wanted:
                return column
        except Exception:
            continue
    raise RuntimeError("Keep the Title column visible in EndNote's reference list.")


def click_row(window, list_view, index: int, column: int) -> None:
    """Pick one reference the way a user does: a real click on its title cell.

    EndNote follows only selection that comes from input. A row selected with
    LVM_SETITEMSTATE is highlighted by the list control, yet EndNote kept its
    own current reference: the Attach File button stayed disabled with a
    batch still selected, and a new attachment was not saved until a real
    click moved to another record. The title cell rather than the row's
    middle, because some columns (Rating) change the record when clicked.
    """
    window.set_focus()
    list_view.get_item(index, column).click_input(where="text")
    time.sleep(0.5)


def attach_with_endnote(window, list_view, index: int, title: object, pdf_path: Path):
    column = _title_column(list_view, index, title)
    click_row(window, list_view, index, column)
    if selected_rows(list_view) != [index]:
        raise RuntimeError("EndNote did not select just the target record; nothing attached.")
    attach_button = window.child_window(control_id=32803)
    attach_button.wait("enabled", timeout=10)
    attach_button.wrapper_object().click()

    dialog = window.app.window(title_re="Select a file to link to the reference.*")
    dialog.wait("visible", timeout=15)
    dialog.child_window(control_id=1148, class_name="Edit").set_edit_text(str(pdf_path))
    copy_box = dialog.child_window(control_id=3376, class_name="Button").wrapper_object()
    if copy_box.get_check_state() != 1:
        copy_box.check()
    dialog.child_window(control_id=1, class_name="Button").click()
    dialog.wait_not("visible", timeout=30)

    # EndNote copies the file at once but keeps the new link in the open
    # reference until the user moves to another one: refs.MYD stayed
    # untouched for over a minute, and was written the moment a real click
    # picked another record. Not clicked back afterwards, since saving can
    # re-sort the list and the old index may then be someone else's row.
    if list_view.item_count() > 1:
        neighbour = index + 1 if index + 1 < list_view.item_count() else index - 1
        click_row(window, list_view, neighbour, column)


def restore_selection(list_view, indices: list[int]) -> None:
    set_row_selected(list_view, -1, False)
    for index in indices:
        set_row_selected(list_view, index, True)


def wait_for_attachment(rdb_dir: Path, rec_number: int, before: set[str]) -> list[str]:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        rows = list(refstable.read_rows(rdb_dir))
        row = next((item for item in rows if int(item["id"]) == rec_number), None)
        if row:
            added = sorted(set(refstable._pdf_files(row)) - before)
            if added:
                return added
        time.sleep(1)
    raise RuntimeError("EndNote displayed the attachment but did not persist it within 30 seconds.")


def refresh_indexes():
    for command in ("index", "index-pdfs"):
        subprocess.run(
            [sys.executable, "-m", "endnotex9_toolkit.cli", command],
            cwd=REPO_ROOT,
            check=True,
            stdout=subprocess.DEVNULL,
        )


def classify_failure(error: object) -> str:
    message = str(error).casefold()
    if (
        "identity check" in message
        or "could not be parsed" in message
        or "contains no pages" in message
    ):
        return "identity_mismatch"
    if "429" in message or "rate limit" in message or "too many requests" in message:
        return "rate_limited"
    if any(token in message for token in ("captcha", "sign in", "login", "access denied", "sso")):
        return "needs_user_auth"
    return "retryable_failed"


def browser_was_closed(error: object) -> bool:
    return "has been closed" in str(error).casefold()


def _retrieve_and_validate(
    item: dict,
    store: JobStore,
    browser: BrowserRetriever,
    args,
) -> tuple[bytes, str, str, dict]:
    rec_number = item["rec_number"]
    errors: list[str] = []
    if not args.no_oa:
        candidates, resolver_errors = resolve_oa_candidates(item["doi"], min(args.timeout, 30))
        errors.extend(resolver_errors)
        for candidate in candidates:
            store.transition(
                rec_number,
                "downloading",
                stage="oa_download",
                source=candidate.provider,
            )
            try:
                data, source_url = download_pdf(candidate, min(args.timeout, 120))
                validation = validate_pdf(data, item["doi"], item["title"])
                return data, source_url, candidate.provider, validation
            except Exception as exc:
                errors.append(f"{candidate.provider}: {exc}")

    store.transition(rec_number, "downloading", stage="institutional_browser", source="wos")
    try:
        data, source_url = browser.download(item["doi"], item["title"], args.timeout)
        validation = validate_pdf(data, item["doi"], item["title"])
        return data, source_url, "wos-publisher", validation
    except Exception as exc:
        errors.append(f"wos-publisher: {exc}")
        raise RuntimeError("; ".join(errors[-5:])) from exc


def _current_row(rdb_dir: Path, rec_number: int) -> dict | None:
    return next(
        (row for row in refstable.read_rows(rdb_dir) if int(row["id"]) == int(rec_number)),
        None,
    )


def _selection_result(selected: list[dict], cfg, *, status: str) -> dict:
    records = [
        {
            "rec_number": item["rec_number"],
            "title": item["title"],
            "doi": item["doi"],
            "has_pdf": item["has_pdf"],
        }
        for item in selected
    ]
    result = {"status": status, "library": cfg.library_path, "records": records}
    if len(records) == 1:
        result.update(records[0])
    return result


def run(args) -> dict:
    cfg, rdb_dir, rows = load_rows()
    window = _endnote_window()
    batch_mode = bool(args.batch or args.resume)
    selected, list_view = selected_records(
        window,
        rows,
        allow_many=batch_mode or args.status or args.dry_run,
    )
    selected_by_id = {item["rec_number"]: item for item in selected}
    original_indices = [item["ui_index"] for item in selected]

    if args.status or args.dry_run:
        return _selection_result(selected, cfg, status="ready" if args.status else "planned")

    if not batch_mode:
        item = selected[0]
        if item["has_pdf"]:
            raise RuntimeError("The selected EndNote reference already has a PDF attachment.")
        if not item["doi"]:
            raise RuntimeError("The selected reference has no DOI; this version requires a DOI.")

    jobs_root = configmod.config_dir() / "lit-downloader" / "jobs"
    if args.resume:
        store = JobStore.load(jobs_root, args.resume)
        if os.path.normcase(store.manifest["library"]) != os.path.normcase(cfg.library_path):
            raise RuntimeError("The resume batch belongs to a different EndNote library.")
    else:
        store = JobStore.create(jobs_root, cfg.library_path, selected, args.batch_id)

    resumable = store.resumable_jobs()
    missing_selection = [
        int(job["rec_number"])
        for job in resumable
        if job["status"] != "attached" and int(job["rec_number"]) not in selected_by_id
    ]
    if missing_selection:
        raise RuntimeError(
            "Resuming requires all pending EndNote records to be selected and visible: "
            + ", ".join(map(str, missing_selection))
        )

    browser = BrowserRetriever()
    attached_jobs: list[int] = [
        int(job["rec_number"]) for job in resumable if job["status"] == "attached"
    ]
    access_failures = 0
    processed = 0
    pause_reason = ""
    try:
        actionable = [job for job in resumable if job["status"] != "attached"][: args.limit]
        for position, job in enumerate(actionable):
            rec_number = int(job["rec_number"])
            item = selected_by_id[rec_number]
            processed += 1
            current = _current_row(rdb_dir, rec_number)
            if current is None:
                store.transition(
                    rec_number,
                    "permanent_failed",
                    stage="preflight",
                    error="EndNote record no longer exists.",
                )
                continue
            if refstable._pdf_files(current):
                store.transition(
                    rec_number,
                    "skipped_existing",
                    stage="preflight",
                    pdf_files=refstable._pdf_files(current),
                )
                continue
            if not item["doi"]:
                store.transition(
                    rec_number,
                    "permanent_failed",
                    stage="preflight",
                    error="Record has no DOI; automatic identity matching is unsafe.",
                )
                continue

            store.begin_attempt(rec_number)
            try:
                data, source_url, source, validation = _retrieve_and_validate(
                    item, store, browser, args
                )
                store.transition(
                    rec_number,
                    "validated",
                    stage="pdf_validation",
                    source=source,
                    pages=validation["pages"],
                    size_bytes=len(data),
                    sha256=validation["sha256"],
                    error="",
                )
                staging = save_staging(data, source_url, rec_number)
                before = set(refstable._pdf_files(current))
                store.transition(rec_number, "attaching", stage="endnote_attach")
                row_index = locate_row(list_view, item["ui_index"], item["row"], rows)
                attach_with_endnote(window, list_view, row_index, item["title"], staging)
                added = wait_for_attachment(rdb_dir, rec_number, before)
                store.transition(
                    rec_number,
                    "attached",
                    stage="endnote_persisted",
                    pdf_files=added,
                )
                attached_jobs.append(rec_number)
                access_failures = 0
            except Exception as exc:
                status = classify_failure(exc)
                store.transition(
                    rec_number,
                    status,
                    stage=store.job(rec_number).get("stage", "download"),
                    # pywinauto's timeouts and visibility errors carry no
                    # message; without the type the failure is unreadable.
                    error=str(exc) or repr(exc),
                )
                if status in {"rate_limited", "needs_user_auth"}:
                    access_failures += 1
                else:
                    access_failures = 0
                if status == "needs_user_auth":
                    pause_reason = "institutional login or user action required"
                    break
                if browser_was_closed(exc):
                    # Every later record would fail the same way within a second.
                    pause_reason = "the download browser window was closed"
                    break
                if access_failures >= args.max_access_failures:
                    pause_reason = "publisher access circuit breaker opened"
                    break
            if position + 1 < len(actionable) and args.delay:
                time.sleep(args.delay)
    finally:
        browser.close()
        restore_selection(list_view, original_indices)

    if attached_jobs:
        try:
            refresh_indexes()
        except Exception as exc:
            for rec_number in attached_jobs:
                store.transition(
                    rec_number,
                    "attached",
                    stage="index_refresh",
                    error=f"Attachment persisted but index refresh failed: {exc}",
                )
        else:
            for rec_number in attached_jobs:
                store.transition(rec_number, "indexed", stage="complete", error="")

    summary = store.summary()
    pending = len(store.resumable_jobs())
    failures = sum(
        summary.get(status, 0)
        for status in ("permanent_failed", "identity_mismatch", "retryable_failed", "rate_limited")
    )
    if pause_reason or pending:
        status = "batch_paused" if pause_reason else "batch_partial"
    elif failures:
        status = "batch_finished_with_errors"
    else:
        status = "batch_complete"

    result = {
        "status": status,
        "batch_id": store.batch_id,
        "processed": processed,
        "summary": summary,
        "report": str(store.report_path),
    }
    if pause_reason:
        result["pause_reason"] = pause_reason
    if not batch_mode and len(store.jobs) == 1 and store.jobs[0]["status"] == "indexed":
        job = store.jobs[0]
        result.update(
            {
                "status": "attached",
                "rec_number": job["rec_number"],
                "title": job["title"],
                "doi": job["doi"],
                "validation": {
                    "pages": job["pages"],
                    "sha256": job["sha256"],
                },
                "pdf_files": job["pdf_files"],
                "size_bytes": job["size_bytes"],
            }
        )
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--status", action="store_true", help="resolve and validate selection only")
    parser.add_argument("--batch", action="store_true", help="process all selected references")
    parser.add_argument("--resume", metavar="BATCH_ID", help="resume a durable batch")
    parser.add_argument("--batch-id", help="optional id for a new batch")
    parser.add_argument("--dry-run", action="store_true", help="show selected work without writing")
    parser.add_argument("--no-oa", action="store_true", help="skip OA resolvers and start at WoS")
    parser.add_argument("--limit", type=int, default=20, help="maximum records per invocation")
    parser.add_argument("--delay", type=float, default=10, help="seconds between batch records")
    parser.add_argument(
        "--max-access-failures",
        type=int,
        default=3,
        help="pause after consecutive access/rate-limit failures",
    )
    parser.add_argument("--timeout", type=int, default=300, help="browser step timeout in seconds")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.timeout <= 0 or args.limit <= 0 or args.delay < 0 or args.max_access_failures <= 0:
        raise SystemExit(
            "timeout, limit, and max-access-failures must be positive; delay cannot be negative"
        )
    if args.resume and args.batch_id:
        raise SystemExit("--batch-id cannot be used with --resume")
    try:
        result, code = run(args), 0
    except Exception as exc:
        result, code = {"status": "error", "error": str(exc) or repr(exc)}, 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
