"""Shared Unicode-preserving keys for metadata matching and PDF identity checks."""

from __future__ import annotations

import re
import unicodedata

from .parser import normalize_doi


def doi_key(value: object) -> str:
    return normalize_doi(str(value or "")).casefold().rstrip(". ")


def title_key(value: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", value).casefold() if c.isalnum())


def title_words(value: object) -> str:
    return " ".join(re.findall(r"[\w]+", str(value or "").casefold(), flags=re.UNICODE))
