"""Shared helpers for the collection pipeline (search export -> PDFs -> EndNote import file).

A *project* is one folder per search topic. It holds `project.json`, the raw exports in `raw/`,
the working list, the verified PDFs in `pdf/`, and the state files every stage shares:

- `pdf_state.json`          written by the open-access stage (`oa.py`)
- `pdf_state_manual.json`   written only by the downloads watcher (`watch.py`)
- `open_in_chrome_log.jsonl` one line per link the opener showed in the browser

Two writers never share a state file, so the OA stage and a browser round can run side by side.
Nothing here touches an EndNote library; `build_import.py` and `verify_import.py` only read it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
HELPER_DIR = SCRIPT_DIR.parent  # download_selected.py: PDF validation shared with selected mode

STATE_OA = "pdf_state.json"
STATE_BROWSER = "pdf_state_manual.json"
OPEN_LOG = "open_in_chrome_log.jsonl"
OA_URL_CACHES = ("s2_urls.json", "openalex_urls.json")

# Publishers that refuse scripted browsing in practice; their records go to the manual list.
DEFAULT_EXCLUDE_PREFIXES = ["10.1063", "10.1116", "10.1134", "10.1080"]
DEFAULT_BLOCKED_HOSTS = ["sagepub", "hal.science", "hdl.handle.net"]

USER_AGENT = "endnotex9-toolkit-lit-downloader/0.2 (collection pipeline)"


def _default_downloads() -> str:
    return str(Path.home() / "Downloads")


def _default_browser() -> str:
    for base in (
        os.environ.get("PROGRAMFILES", ""),
        os.environ.get("PROGRAMFILES(X86)", ""),
        os.environ.get("LOCALAPPDATA", ""),
    ):
        exe = Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe"
        if base and exe.is_file():
            return str(exe)
    return r"C:\Program Files\Google\Chrome\Application\chrome.exe"


@dataclass
class Project:
    """One search topic. Every field can be set in `<dir>/project.json`."""

    dir: Path
    list: str = "list.ris"
    downloads: str = field(default_factory=_default_downloads)
    browser: str = field(default_factory=_default_browser)
    crossref_cache: str = "crossref_cache"
    sciencedirect_daily: int = 100
    sciencedirect_gap: float = 60
    publisher_gap: float = 15
    exclude_prefixes: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDE_PREFIXES))
    blocked_hosts: list[str] = field(default_factory=lambda: list(DEFAULT_BLOCKED_HOSTS))
    label: str = ""

    @property
    def list_path(self) -> Path:
        return self.dir / self.list

    @property
    def pdf_dir(self) -> Path:
        return self.dir / "pdf"

    @property
    def cache_dir(self) -> Path:
        return self.dir / self.crossref_cache


def load_project(path: str | Path) -> Project:
    root = Path(path).resolve()
    if not root.is_dir():
        raise SystemExit(f"project folder not found: {root}")
    cfg_path = root / "project.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
    known = set(Project.__dataclass_fields__) - {"dir"}
    unknown = set(cfg) - known
    if unknown:
        raise SystemExit(f"unknown keys in {cfg_path}: {sorted(unknown)}")
    return Project(dir=root, **cfg)


def console_utf8() -> None:
    """A window title or paper title must never stop a run on a GBK/cp1252 console."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


# ---------------------------------------------------------------------------------------------
# RIS
# ---------------------------------------------------------------------------------------------

_TAG = re.compile(r"^([A-Z][A-Z0-9])  - ?(.*)$")


def parse_ris(text: str) -> list[list[list[str]]]:
    """Records as ordered [tag, value] pairs; continuation lines join the previous value."""
    recs, cur = [], []
    for line in text.lstrip("\ufeff").splitlines():
        m = _TAG.match(line)
        if m:
            cur.append([m.group(1), m.group(2).rstrip()])
            if m.group(1) == "ER":
                recs.append(cur)
                cur = []
        elif cur and line.strip():
            cur[-1][1] += " " + line.strip()
    return recs


def read_ris(path: str | Path) -> list[list[list[str]]]:
    return parse_ris(Path(path).read_text(encoding="utf-8-sig"))


def write_ris(path: str | Path, recs: list[list[list[str]]]) -> None:
    """UTF-8 without BOM, CRLF, blank line between records: what EndNote's RIS filter reads."""
    with Path(path).open("w", encoding="utf-8", newline="\r\n") as fh:
        for rec in recs:
            fh.write("\n".join(f"{t}  - {v}" for t, v in rec) + "\n\n")


def get(rec: list[list[str]], tag: str) -> list[str]:
    return [v for t, v in rec if t == tag]


def first(rec: list[list[str]], *tags: str) -> str:
    for tag in tags:
        vals = get(rec, tag)
        if vals:
            return vals[0].strip()
    return ""


def rec_doi(rec: list[list[str]]) -> str:
    return normalize_doi(first(rec, "DO"))


def rec_title(rec: list[list[str]]) -> str:
    return first(rec, "TI", "T1")


def rec_journal(rec: list[list[str]]) -> str:
    return first(rec, "T2", "JO", "JF")


def normalize_doi(value: str) -> str:
    value = (value or "").strip().lower()
    value = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi\s*:\s*)", "", value)
    return value.rstrip(". ")


def norm_title(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def list_records(project: Project) -> list[list[list[str]]]:
    if not project.list_path.exists():
        raise SystemExit(f"working list not found: {project.list_path}")
    return read_ris(project.list_path)


def list_dois(project: Project) -> list[str]:
    return [d for d in (rec_doi(r) for r in list_records(project)) if d]


def safe_name(doi: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", doi)


def doi_prefix(doi: str) -> str:
    return doi.split("/", 1)[0]


# ---------------------------------------------------------------------------------------------
# State files
# ---------------------------------------------------------------------------------------------


def read_json(path: Path, default=None, attempts: int = 20):
    """Another stage may be swapping the file in this instant; a torn read is retried."""
    if not path.exists():
        return {} if default is None else default
    for _ in range(attempts - 1):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            time.sleep(0.5)
    return json.loads(path.read_text(encoding="utf-8"))


def write_json_atomic(path: Path, data) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    for _ in range(20):
        try:
            tmp.replace(path)
            return
        except PermissionError:  # a reader has the file open this instant (Windows)
            time.sleep(0.5)
    tmp.replace(path)


def ok_states(project: Project) -> dict[str, dict]:
    """DOI -> state entry for every record with a verified PDF, from either writer."""
    found = {}
    for name in (STATE_OA, STATE_BROWSER):
        for doi, entry in read_json(project.dir / name).items():
            if entry.get("status") == "ok" and doi not in found:
                found[doi] = entry
    return found


def pdf_path(project: Project, doi: str, entry: dict) -> Path | None:
    path = project.pdf_dir / entry.get("file", "")
    return path if entry.get("file") and path.is_file() else None


def read_open_log(project: Project) -> list[dict]:
    path = project.dir / OPEN_LOG
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def oa_urls(project: Project) -> dict[str, list[str]]:
    urls: dict[str, list[str]] = {}
    for name in OA_URL_CACHES:
        for doi, value in read_json(project.dir / name).items():
            urls.setdefault(doi, []).extend([value] if isinstance(value, str) else value)
    return urls


def crossref_message(project: Project, doi: str) -> dict:
    path = project.cache_dir / (safe_name(doi) + ".json")
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {}


def elsevier_pii(message: dict) -> str:
    pii = (message.get("alternative-id") or [""])[0]
    # Some titles that left Elsevier carry the DOI here instead of a PII.
    return pii if re.fullmatch(r"S?[0-9X]{15,17}", pii, re.I) else ""


def folder_number(doi: str, attempt: int = 0) -> str:
    """Stable 10-digit attachment folder per DOI, shaped like EndNote's own folder names."""
    key = doi if not attempt else f"{doi}#{attempt}"
    return str(int(hashlib.md5(key.encode()).hexdigest(), 16) % 9_000_000_000 + 1_000_000_000)


def load_validator():
    """`validate_pdf` and `title_similarity_in_text` from the selected-records helper."""
    sys.path.insert(0, str(HELPER_DIR))
    from download_selected import title_similarity_in_text, validate_pdf

    return validate_pdf, title_similarity_in_text
