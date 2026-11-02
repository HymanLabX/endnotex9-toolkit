"""Validate PDF bytes and bibliographic identity, independently of browser/UI code."""

from __future__ import annotations

import hashlib
from difflib import SequenceMatcher

from .normalize import doi_key, title_words
from .pdf import _load_fitz

MAX_CAPTURE_BYTES = 200 * 1024 * 1024


class MissingDoiError(RuntimeError):
    """The document parsed, but the requested DOI is absent."""


def title_similarity(left: object, right: object) -> float:
    return SequenceMatcher(None, title_words(left), title_words(right)).ratio()


def title_similarity_in_text(title: object, body: object) -> float:
    needle, haystack = title_words(title), title_words(body)
    if not needle or not haystack:
        return 0.0
    if needle in haystack:
        return 1.0
    title_tokens, body_tokens = needle.split(), haystack.split()
    width = len(title_tokens)
    candidates = (
        " ".join(body_tokens[start : start + window])
        for window in {max(1, width - 2), width, width + 2}
        for start in range(max(1, min(len(body_tokens) - window + 1, 300)))
    )
    return max((SequenceMatcher(None, needle, part).ratio() for part in candidates), default=0)


def validate_pdf(data: bytes, doi: str, title: str) -> dict:
    if not data.startswith(b"%PDF-"):
        raise RuntimeError("Downloaded response is not a PDF (missing %PDF- signature).")
    if len(data) > MAX_CAPTURE_BYTES:
        raise RuntimeError("Downloaded PDF exceeds the 200 MB validation limit.")
    fitz = _load_fitz()
    try:
        with fitz.open(stream=data, filetype="pdf") as document:
            if document.page_count < 1:
                raise RuntimeError("Downloaded PDF contains no pages.")
            extracted = "\n".join(page.get_text() for page in document)
            metadata_title = document.metadata.get("title", "")
            pages = document.page_count
    except Exception as exc:
        raise RuntimeError(f"Downloaded PDF could not be parsed: {exc}") from exc
    doi_match = bool(doi_key(doi)) and doi_key(doi) in extracted.casefold()
    similarity = max(
        title_similarity(title, metadata_title), title_similarity_in_text(title, extracted[:20_000])
    )
    # Check the title even when a caller might accept an author version without DOI.
    if similarity < 0.70:
        raise RuntimeError("PDF identity check failed: title similarity is too low.")
    if doi and not doi_match:
        raise MissingDoiError("PDF identity check failed: the selected record DOI was not found.")
    return {
        "pages": pages,
        "doi_match": doi_match,
        "title_similarity": round(similarity, 4),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
