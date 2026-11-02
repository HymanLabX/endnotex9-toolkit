"""Locate, extract and index the text of PDFs attached to references.

EndNote stores an attachment as `internal-pdf://<folder-id>/<filename>.pdf`,
relative to `<Library>.Data/PDF`. Resolving that back to a real file is
fiddlier than it looks: the folder id is stable but the filename in the XML
does not always survive round-tripping through EndNote's exporter byte for
byte (accented characters in author names are the usual culprit). So
resolution falls back to percent-decoding, and finally to "whatever PDF is
in that folder" — the folder holds exactly one attachment.

Text extraction needs PyMuPDF, which is an optional extra (`pip install
endnotex9-toolkit[pdf]`) rather than a hard dependency: PyMuPDF is AGPL-3.0 and
this package is MIT, so it is imported lazily and only when the user has
chosen to install it. Everything else in the server works without it.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from urllib.parse import unquote

from . import db as dbmod

logger = logging.getLogger(__name__)

# PDF extractors emit a word broken across lines as "marten-\nsitic";
# rejoining it is what makes such a word findable at all.
_HYPHEN_LINEBREAK_RE = re.compile(r"(?<=\w)-\s*\n\s*(?=[a-z])")
_SPACE_RUN_RE = re.compile(r"[ \t\u00a0\u3000]+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")

PDF_EXTRA_HINT = (
    "PDF text extraction needs PyMuPDF, which ships as an optional extra: "
    'run `pip install "endnotex9-toolkit[pdf]"` and try again.'
)


class PdfBackendMissing(RuntimeError):
    """Raised when a PDF operation is attempted without PyMuPDF installed."""


def _load_fitz():
    try:
        import fitz
    except ImportError as exc:  # pragma: no cover - depends on install extras
        raise PdfBackendMissing(PDF_EXTRA_HINT) from exc
    return fitz


def backend_available() -> bool:
    try:
        _load_fitz()
    except PdfBackendMissing:
        return False
    return True


def resolve_pdf_path(pdf_dir: str | Path, rel_path: str) -> Path | None:
    """Map an `internal-pdf://` relative path onto a file on disk."""
    if not pdf_dir or not rel_path:
        return None
    root = Path(pdf_dir).expanduser().resolve()
    if not root.is_dir():
        return None

    safe_candidates: list[Path] = []
    for candidate in dict.fromkeys((rel_path, unquote(rel_path))):
        if not candidate or "\x00" in candidate:
            continue
        windows = PureWindowsPath(candidate)
        portable = PurePosixPath(candidate.replace("\\", "/"))
        if windows.drive or windows.root or portable.is_absolute() or ".." in portable.parts:
            continue

        path = (root.joinpath(*portable.parts)).resolve()
        if not path.is_relative_to(root):
            continue
        safe_candidates.append(path)
        if path.is_file() and path.suffix.casefold() == ".pdf":
            return path

    # EndNote occasionally renames an attachment while keeping the folder id.
    # Retain the historical single-PDF fallback, but only inside a folder that
    # already passed the same containment checks above.
    for path in safe_candidates:
        folder_path = path.parent
        if folder_path == root or not folder_path.is_dir():
            continue
        pdfs = sorted(
            candidate.resolve()
            for candidate in folder_path.glob("*.pdf")
            if candidate.resolve().is_relative_to(root)
        )
        if len(pdfs) == 1:
            return pdfs[0]
    return None


def file_fingerprint(path: Path) -> str:
    """Size plus mtime — enough to notice a replaced attachment, and far
    cheaper than hashing a 20 MB PDF on every index run."""
    stat = path.stat()
    return f"{stat.st_size}:{stat.st_mtime_ns}"


def _clean(text: str) -> str:
    text = _HYPHEN_LINEBREAK_RE.sub("", text)
    text = _SPACE_RUN_RE.sub(" ", text)
    return _BLANK_LINES_RE.sub("\n\n", text).strip()


def extract_pages(path: str | Path) -> list[str]:
    """Return the text of each page, in order."""
    fitz = _load_fitz()
    with fitz.open(str(path)) as document:
        return [_clean(page.get_text()) for page in document]


def _attachments(conn: sqlite3.Connection, rec_numbers: list[int] | None) -> list[tuple[int, str]]:
    sql = "SELECT rec_number, pdf_files FROM refs WHERE COALESCE(pdf_files, '[]') != '[]'"
    params: list = []
    if rec_numbers is not None:
        if not rec_numbers:
            return []
        placeholders = ", ".join("?" for _ in rec_numbers)
        sql += f" AND rec_number IN ({placeholders})"
        params = list(rec_numbers)

    pairs = []
    for row in conn.execute(sql, params):
        for rel_path in json.loads(row["pdf_files"] or "[]"):
            pairs.append((row["rec_number"], rel_path))
    return pairs


def count_attachments(conn: sqlite3.Connection) -> int:
    return len(_attachments(conn, None))


def index_pdfs(
    conn: sqlite3.Connection,
    pdf_dir: str | Path,
    rec_numbers: list[int] | None = None,
    force: bool = False,
    max_files: int | None = None,
    on_progress=None,
) -> dict:
    """Extract and index attachment text, skipping files already up to date.

    Returns counts rather than raising on individual failures — one corrupt
    or password-protected PDF in a 600-file library should not abort the run.
    `on_progress(done, total)` is called after each file, for callers that
    want to show something during what can be a multi-minute first run.
    """
    _load_fitz()  # fail fast and clearly if the extra is missing

    stats = {"extracted": 0, "skipped_unchanged": 0, "missing_file": 0, "failed": 0, "pages": 0}
    pairs = _attachments(conn, rec_numbers)

    for done, (rec_number, rel_path) in enumerate(pairs, start=1):
        if max_files is not None and stats["extracted"] >= max_files:
            break
        if on_progress is not None:
            on_progress(done, len(pairs))

        path = resolve_pdf_path(pdf_dir, rel_path)
        if path is None:
            dbmod.delete_pdf_text(conn, rec_number, rel_path)
            stats["missing_file"] += 1
            continue

        fingerprint = file_fingerprint(path)
        if not force:
            existing = dbmod.get_pdf_doc(conn, rec_number, rel_path)
            if (
                existing
                and existing["file_hash"] == fingerprint
                and existing["status"] == "ok"
                and existing["abs_path"] == str(path)
            ):
                stats["skipped_unchanged"] += 1
                continue

        try:
            pages = extract_pages(path)
        except Exception:
            logger.exception("PDF text extraction failed for %s", path)
            dbmod.store_pdf_text(conn, rec_number, rel_path, str(path), [], fingerprint, "failed")
            stats["failed"] += 1
            conn.commit()
            continue

        dbmod.store_pdf_text(conn, rec_number, rel_path, str(path), pages, fingerprint, "ok")
        stats["extracted"] += 1
        stats["pages"] += len(pages)
        conn.commit()

    dbmod.set_meta(
        conn, "last_pdf_indexed_at", datetime.now(timezone.utc).isoformat(timespec="seconds")
    )
    conn.commit()
    stats["attachments_seen"] = len(pairs)
    return stats


def read_pages(
    conn: sqlite3.Connection,
    rec_number: int,
    first_page: int = 1,
    last_page: int | None = None,
    max_chars: int = 20000,
) -> dict:
    """Return stored page text for one reference, truncated to `max_chars`."""
    rows = dbmod.get_pdf_pages(conn, rec_number, first_page, last_page)
    if not rows:
        return {
            "rec_number": rec_number,
            "pages": [],
            "note": "No extracted text for this reference. "
            "Run index_pdfs first, or it may have no PDF attachment.",
        }

    pages = []
    used = 0
    truncated = False
    for row in rows:
        body = row["body"] or ""
        remaining = max_chars - used
        if remaining <= 0:
            truncated = True
            break
        if len(body) > remaining:
            body = body[:remaining]
            truncated = True
        pages.append({"page": row["page"], "file": row["rel_path"], "text": body})
        used += len(body)

    result = {"rec_number": rec_number, "pages": pages, "chars_returned": used}
    if truncated:
        result["truncated"] = True
        result["note"] = (
            f"Output capped at {max_chars} characters. Request a narrower page "
            f"range to read further."
        )
    return result
