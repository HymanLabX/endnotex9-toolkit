"""Format a reference dict as a citation string.

Two styles are implemented:

* **apa7** — APA 7th edition.
* **gbt7714** — GB/T 7714-2015, the Chinese national standard. Required by
  essentially every mainland journal and degree thesis, and the reason it
  matters here is that it has to cite the *English* literature in this
  library correctly too: surname first, given names reduced to initials with
  no periods, at most three authors before the truncation marker, and a
  bracketed document-type code after the title.

`STYLE_FORMATTERS` is the extension point for adding more.
"""

from __future__ import annotations

import re

from .tokenizer import has_cjk

# GB/T 7714-2015 document type codes, keyed by EndNote reference type.
_GBT_TYPE_CODES = {
    "journal article": "J",
    "electronic article": "J",
    "book": "M",
    "edited book": "M",
    "book section": "M",
    "conference paper": "C",
    "conference proceedings": "C",
    "thesis": "D",
    "report": "R",
    "patent": "P",
    "standard": "S",
    "newspaper article": "N",
    "computer program": "CP",
    "online database": "DB",
    "web page": "EB",
}


def _split_name(author: str) -> tuple[str, str]:
    """Split "Last, First Middle" into (last, given). Falls back to treating
    the whole string as a surname, which is right for CJK names and for the
    occasional single-token corporate author."""
    author = author.strip()
    if "," in author:
        last, _, given = author.partition(",")
        return last.strip(), given.strip()
    return author, ""


def _initials(given: str, separator: str = " ", dotted: bool = False) -> str:
    parts = [p for p in re.split(r"[\s.\-]+", given) if p]
    suffix = "." if dotted else ""
    return separator.join(f"{p[0].upper()}{suffix}" for p in parts)


# --------------------------------------------------------------------------
# APA 7th
# --------------------------------------------------------------------------


def _author_apa(author: str) -> str:
    last, given = _split_name(author)
    if not given:
        return last
    return f"{last}, {_initials(given, separator=' ', dotted=True)}"


def _authors_apa(authors: list[str]) -> str:
    formatted = [_author_apa(a) for a in authors if a.strip()]
    if not formatted:
        return ""
    if len(formatted) == 1:
        return formatted[0]
    if len(formatted) <= 20:
        return ", ".join(formatted[:-1]) + ", & " + formatted[-1]
    return ", ".join(formatted[:19]) + ", ... " + formatted[-1]


# APA renders a chapter ("In <book> (pp. …)") differently from an article
# ("<journal>, <vol>(<issue>), <pages>"), and both differently from a
# standalone work, so the reference type has to steer the layout.
_APA_CHAPTER_TYPES = {"book section", "conference paper", "conference proceedings"}
_APA_STANDALONE_TYPES = {"book", "edited book", "thesis", "report", "computer program"}


def _apa_imprint(ref: dict) -> str:
    return ", ".join(p for p in [ref.get("publisher_place") or "", ref.get("publisher") or ""] if p)


def format_apa7(ref: dict) -> str:
    authors = _authors_apa(ref.get("authors") or [])
    year = ref.get("year") or "n.d."
    title = (ref.get("title") or "").rstrip(".")
    container = ref.get("container_title") or ""
    volume = ref.get("volume") or ""
    issue = ref.get("issue") or ""
    locator = ref.get("pages") or ref.get("article_number") or ""
    doi = ref.get("doi") or ""
    kind = (ref.get("ref_type") or "").strip().lower()

    parts = [f"{authors} ({year})." if authors else f"({year})."]
    parts.append(f"{title}.")

    if kind in _APA_CHAPTER_TYPES and container:
        parts.append(f"In {container}" + (f" (pp. {locator})." if locator else "."))
        if _apa_imprint(ref):
            parts.append(f"{_apa_imprint(ref)}.")
    elif container and kind not in _APA_STANDALONE_TYPES:
        vol_issue = volume
        if issue:
            vol_issue += f"({issue})"
        tail = ", ".join(p for p in [container, vol_issue, locator] if p)
        parts.append(f"{tail}.")
    elif _apa_imprint(ref):
        parts.append(f"{_apa_imprint(ref)}.")

    if doi:
        parts.append(doi if doi.startswith("http") else f"https://doi.org/{doi}")

    return " ".join(p for p in parts if p).strip()


# --------------------------------------------------------------------------
# GB/T 7714-2015
# --------------------------------------------------------------------------


def _author_gbt(author: str) -> str:
    """ "Li, Z. B." -> "Li Z B"; a CJK name is left alone."""
    last, given = _split_name(author)
    if not given or has_cjk(last):
        return last.replace(",", "").strip()
    return f"{last} {_initials(given)}".strip()


def _authors_gbt(authors: list[str], chinese: bool) -> str:
    """At most three names, then the truncation marker for the reference's
    own language ("等" for Chinese sources, "et al" for foreign ones)."""
    formatted = [_author_gbt(a) for a in authors if a.strip()]
    if not formatted:
        return ""
    if len(formatted) <= 3:
        return ", ".join(formatted)
    marker = "等" if chinese else "et al"
    return ", ".join(formatted[:3]) + ", " + marker


def _gbt_type_code(ref: dict) -> str:
    return _GBT_TYPE_CODES.get((ref.get("ref_type") or "").strip().lower(), "J")


def _is_chinese_source(ref: dict) -> bool:
    if (ref.get("language") or "").lower().startswith("zh"):
        return True
    return has_cjk(ref.get("title") or "") or has_cjk(" ".join(ref.get("authors") or []))


def format_gbt7714(ref: dict) -> str:
    chinese = _is_chinese_source(ref)
    authors = _authors_gbt(ref.get("authors") or [], chinese)
    title = (ref.get("title") or "").rstrip(". ")
    code = _gbt_type_code(ref)
    year = ref.get("year") or ""
    volume = ref.get("volume") or ""
    issue = ref.get("issue") or ""
    # GB/T 7714 allows an article number where a journal prints no page range.
    locator = ref.get("pages") or ref.get("article_number") or ""
    doi = ref.get("doi") or ""

    parts: list[str] = []
    if authors:
        parts.append(f"{authors}. ")
    parts.append(f"{title}[{code}]")

    if code in {"J", "N"}:
        source = ref.get("container_title") or ""
        head = f". {source}" if source else "."
        detail = ", ".join(p for p in [year, f"{volume}({issue})" if issue else volume] if p)
        parts.append(head + (f", {detail}" if detail else ""))
        if locator:
            parts.append(f": {locator}")
        parts.append(".")
    else:
        if code == "M" and (ref.get("collection_title") or ref.get("container_title")):
            # Analytic (a chapter in a book): 析出文献[M]//专著. …
            parts.append(f"//{ref.get('container_title') or ref.get('collection_title')}")
        imprint = ": ".join(
            p for p in [ref.get("publisher_place") or "", ref.get("publisher") or ""] if p
        )
        tail = ", ".join(p for p in [imprint, year] if p)
        parts.append(f". {tail}" if tail else ".")
        if locator:
            parts.append(f": {locator}")
        parts.append(".")

    text = "".join(parts)
    text = re.sub(r"\.\s*\.", ".", text)
    if doi:
        text += f" DOI: {doi}."
    return text.strip()


STYLE_FORMATTERS = {
    "apa7": format_apa7,
    "gbt7714": format_gbt7714,
}

STYLE_ALIASES = {
    "apa": "apa7",
    "apa-7": "apa7",
    "gb": "gbt7714",
    "gbt": "gbt7714",
    "gb/t 7714": "gbt7714",
    "gb7714": "gbt7714",
    "国标": "gbt7714",
}


def resolve_style(style: str) -> str:
    key = (style or "").strip().lower()
    key = STYLE_ALIASES.get(key, key)
    if key not in STYLE_FORMATTERS:
        available = ", ".join(sorted(STYLE_FORMATTERS))
        raise ValueError(f"Unknown citation style '{style}'. Available: {available}")
    return key


def format_citation(ref: dict, style: str = "apa7") -> str:
    return STYLE_FORMATTERS[resolve_style(style)](ref)


def format_bibliography(refs: list[dict], style: str = "apa7", numbered: bool | None = None) -> str:
    """Format a reference list.

    GB/T 7714 is a numeric style, so entries keep the caller's order and are
    numbered `[1]`, `[2]`, … . APA is author-date, so entries are sorted
    alphabetically instead. Pass `numbered` to override.
    """
    key = resolve_style(style)
    formatter = STYLE_FORMATTERS[key]

    if numbered is None:
        numbered = key == "gbt7714"

    entries = [formatter(ref) for ref in refs]
    if key == "apa7":
        entries.sort(key=str.casefold)

    if not numbered:
        return "\n".join(entries)
    width = len(str(len(entries)))
    return "\n".join(f"[{i:>{width}}] {entry}" for i, entry in enumerate(entries, start=1))
