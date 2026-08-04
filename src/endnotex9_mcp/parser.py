"""Parse an EndNote XML library export (X9 and later) into reference dicts.

EndNote's XML export schema has been stable across the ZIP-based (pre-X20)
and SQLite-based (X20+) .enl formats — this is the one interoperability
surface EndNote documents and keeps backward compatible, which is why
export-then-index is the robust integration path for X9 specifically.

Field names below follow CSL (Citation Style Language, the schema used by
Zotero/Mendeley/Pandoc) where a CSL equivalent exists, rather than
inventing ad hoc names — e.g. `container_title` for a journal/book title,
`publisher_place` for where it was published. The handful of fields with no
CSL counterpart (EndNote's Label, Research Notes, Accession Number) keep
EndNote's own name, spelled out.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
from collections.abc import Generator
from pathlib import Path

from lxml import etree

# Field name -> path relative to a <record> element. Paths are exact rather
# than descendant-of (`.//`) so a nested element can't be picked up by
# accident — every one of these was verified against a real X9 export.
FIELD_PATHS = {
    "title": "titles/title",
    "title_short": "titles/short-title",
    "container_title": "titles/secondary-title",
    "container_title_short": "periodical/abbr-1",
    "collection_title": "titles/tertiary-title",
    "year": "dates/year",
    "volume": "volume",
    "issue": "number",
    "pages": "pages",
    "article_number": "section",
    "number_of_volumes": "num-vols",
    "abstract": "abstract",
    "publisher": "publisher",
    "publisher_place": "pub-location",
    "edition": "edition",
    "language": "language",
    "label": "label",
    "note": "notes",
    "research_notes": "research-notes",
    "accession_number": "accession-num",
    "access_date": "access-date",
    "custom_1": "custom1",
}

_PDF_URL_PREFIX = "internal-pdf://"
_DOI_PREFIXES = (
    "https://doi.org/",
    "http://doi.org/",
    "https://dx.doi.org/",
    "http://dx.doi.org/",
    "doi:",
    "doi ",
)
_FORBIDDEN_XML_DECLARATION = re.compile(rb"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)
_XML_SCAN_TAIL = 32

# EndNote's Language field is free text, and one library happily mixes "en",
# "English" and "eng". Fold the common spellings onto an ISO 639-1 code so
# callers can filter on a single value; anything unrecognised is kept as-is.
_LANGUAGE_CODES = {
    "en": "en",
    "eng": "en",
    "english": "en",
    "zh": "zh",
    "chi": "zh",
    "zho": "zh",
    "chinese": "zh",
    "zh-cn": "zh",
    "chinese (simplified)": "zh",
    "de": "de",
    "ger": "de",
    "deu": "de",
    "german": "de",
    "fr": "fr",
    "fre": "fr",
    "fra": "fr",
    "french": "fr",
    "ja": "ja",
    "jpn": "ja",
    "japanese": "ja",
    "ru": "ru",
    "rus": "ru",
    "russian": "ru",
    "es": "es",
    "spa": "es",
    "spanish": "es",
}


def _text(el: etree._Element | None) -> str:
    if el is None:
        return ""
    return "".join(el.itertext()).strip()


def _texts(record: etree._Element, path: str) -> list[str]:
    return [t for t in (_text(el) for el in record.findall(path)) if t]


class UnsafeXmlError(ValueError):
    """Raised when an export contains a DTD or entity declaration."""


class _SafeXmlReader(io.BufferedReader):
    """Reject DTD/entity declarations while libxml streams the export.

    EndNote exports do not need either construct. Rejecting them closes the
    local-file and entity-expansion attack surface without loading a large
    library export into memory first. The retained tail catches a declaration
    split across two reads.
    """

    def __init__(self, raw: io.BufferedReader) -> None:
        super().__init__(raw)
        self._scan_tail = b""

    def read(self, size: int = -1) -> bytes:
        chunk = super().read(size)
        probe = self._scan_tail + chunk
        if _FORBIDDEN_XML_DECLARATION.search(probe):
            raise UnsafeXmlError("EndNote XML must not contain DTD or entity declarations")
        self._scan_tail = probe[-_XML_SCAN_TAIL:]
        return chunk


def _safe_iterparse(xml_path: str | Path, *, events: tuple[str, ...], tag: str):
    """Build a hardened, streaming lxml iterator for an EndNote export."""
    raw = Path(xml_path).open("rb", buffering=0)
    reader = _SafeXmlReader(raw)
    try:
        context = etree.iterparse(
            reader,
            events=events,
            tag=tag,
            load_dtd=False,
            no_network=True,
            resolve_entities=False,
            huge_tree=False,
        )
    except Exception:
        reader.close()
        raise
    return context, reader


def normalize_doi(raw: str) -> str:
    """Strip the assorted prefixes EndNote records leave on a DOI."""
    doi = raw.strip()
    lowered = doi.lower()
    for prefix in _DOI_PREFIXES:
        if lowered.startswith(prefix):
            doi = doi[len(prefix) :].strip()
            break
    return doi


def _clean_serial_number(raw: str) -> str:
    """EndNote's ISBN field doubles as ISSN and can hold several qualified
    values (`1878-0180 (Electronic)\\r1878-0180 (Linking)`). Keep the first."""
    first = re.split(r"[\r\n]", raw.strip(), maxsplit=1)[0]
    return re.sub(r"\s*\([^)]*\)\s*$", "", first).strip()


def _normalize_language(raw: str) -> str:
    return _LANGUAGE_CODES.get(raw.strip().lower(), raw.strip())


def _pdf_files(record: etree._Element) -> list[str]:
    """Relative paths of attached PDFs, as stored under `<Library>.Data/PDF`.

    EndNote writes these as `internal-pdf://<folder-id>/<filename>.pdf`; the
    folder id is part of the location, so unlike the filename alone it must
    be kept for the file to be findable on disk.
    """
    paths = []
    for raw in _texts(record, "urls/pdf-urls/url"):
        if raw.startswith(_PDF_URL_PREFIX):
            rel = raw[len(_PDF_URL_PREFIX) :].strip("/")
            if rel:
                paths.append(rel)
    return paths


def _content_hash(ref: dict) -> str:
    """Cheap fingerprint used to skip re-indexing unchanged records."""
    payload = json.dumps(ref, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def parse_record(record: etree._Element) -> dict | None:
    """Turn one `<record>` element into a reference dict, or None if it has
    no usable record number."""
    try:
        rec_number = int(_text(record.find("rec-number")))
    except (TypeError, ValueError):
        return None

    ref_type_el = record.find("ref-type")
    ref: dict = {
        "rec_number": rec_number,
        "ref_type": ref_type_el.get("name", "") if ref_type_el is not None else "",
        "authors": _texts(record, "contributors/authors/author"),
        "editors": _texts(record, "contributors/secondary-authors/author"),
        "keywords": _texts(record, "keywords/keyword"),
        "urls": _texts(record, "urls/related-urls/url"),
        "pdf_files": _pdf_files(record),
    }
    for field, path in FIELD_PATHS.items():
        ref[field] = _text(record.find(path))

    ref["doi"] = normalize_doi(_text(record.find("electronic-resource-num")))
    ref["isbn"] = _clean_serial_number(_text(record.find("isbn")))
    ref["url"] = ref["urls"][0] if ref["urls"] else ""
    ref["language"] = _normalize_language(ref["language"])

    ref["content_hash"] = _content_hash(ref)
    return ref


def library_path(xml_path: str | Path) -> str:
    """Read the source `.enl` path EndNote stamps into every exported record.

    Worth doing because the export is often saved somewhere other than next
    to the library, and this attribute is the only pointer back to where the
    attachments actually live.
    """
    context, reader = _safe_iterparse(xml_path, events=("start",), tag="database")
    try:
        for _event, element in context:
            return (element.get("path") or "").strip()
    finally:
        reader.close()
    return ""


def parse_library(xml_path: str | Path) -> Generator[dict, None, None]:
    """Yield one dict per <record>, using iterparse for constant memory use."""
    xml_path = Path(xml_path)
    context, reader = _safe_iterparse(xml_path, events=("end",), tag="record")
    try:
        for _event, record in context:
            ref = parse_record(record)
            if ref is not None:
                yield ref

            record.clear()
            while record.getprevious() is not None:
                del record.getparent()[0]
    finally:
        reader.close()
