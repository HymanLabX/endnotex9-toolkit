"""Export references as BibTeX.

Citation keys prefer EndNote's own Label field when it is set — in a library
maintained alongside a LaTeX project that field usually already holds the
citekey the .tex files use, and silently inventing a different one would
break every `\\cite` in the document. Only when Label is empty is a key
derived from author/year/title.
"""

from __future__ import annotations

import re
import unicodedata

# EndNote reference type -> BibTeX entry type. Anything unmapped becomes
# @misc, which every .bst can render.
_ENTRY_TYPES = {
    "journal article": "article",
    "electronic article": "article",
    "book": "book",
    "edited book": "book",
    "book section": "incollection",
    "conference paper": "inproceedings",
    "conference proceedings": "inproceedings",
    "thesis": "phdthesis",
    "report": "techreport",
    "manuscript": "unpublished",
    "patent": "misc",
    "computer program": "misc",
    "web page": "misc",
}

_ESCAPES = {
    "\\": r"\textbackslash{}",
    "{": r"\{",
    "}": r"\}",
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}

_KEY_STOPWORDS = {"a", "an", "the", "on", "of", "in", "for", "and", "with", "to"}


def _escape(value: str) -> str:
    return "".join(_ESCAPES.get(ch, ch) for ch in value)


def _entry_type(ref: dict) -> str:
    return _ENTRY_TYPES.get((ref.get("ref_type") or "").strip().lower(), "misc")


def _ascii_slug(text: str) -> str:
    """Fold to ASCII letters/digits — BibTeX keys travel through too many
    tools to risk non-ASCII in them."""
    folded = unicodedata.normalize("NFKD", text)
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    return re.sub(r"[^A-Za-z0-9]", "", folded).lower()


def _derive_key(ref: dict) -> str:
    authors = ref.get("authors") or []
    surname = _ascii_slug(authors[0].split(",")[0]) if authors else ""
    year = re.sub(r"\D", "", ref.get("year") or "")
    word = ""
    for candidate in re.split(r"\s+", ref.get("title") or ""):
        slug = _ascii_slug(candidate)
        if len(slug) > 2 and slug not in _KEY_STOPWORDS:
            word = slug
            break
    key = f"{surname}{year}{word}"
    return key or f"ref{ref.get('rec_number', '')}"


def citation_key(ref: dict) -> str:
    label = (ref.get("label") or "").strip()
    if label:
        # Labels are free text; keep only what BibTeX accepts in a key.
        cleaned = re.sub(r"[^A-Za-z0-9:_\-.+/]", "", _ascii_slug(label) or label)
        if cleaned:
            return cleaned
    return _derive_key(ref)


def _format_pages(pages: str) -> str:
    """BibTeX renders a page range with an en dash: 1505--1518."""
    return re.sub(r"\s*[-\u2013\u2014]+\s*", "--", pages.strip())


def _fields(ref: dict, include_abstract: bool) -> list[tuple[str, str]]:
    entry_type = _entry_type(ref)
    container = ref.get("container_title") or ""

    fields: list[tuple[str, str | None]] = [
        ("author", " and ".join(ref.get("authors") or [])),
        ("editor", " and ".join(ref.get("editors") or [])),
        ("title", ref.get("title")),
        ("journal", container if entry_type == "article" else ""),
        ("booktitle", container if entry_type in {"incollection", "inproceedings"} else ""),
        ("series", ref.get("collection_title")),
        ("year", ref.get("year")),
        ("volume", ref.get("volume")),
        ("number", ref.get("issue")),
        ("pages", _format_pages(ref.get("pages") or "")),
        ("eid", ref.get("article_number")),
        ("publisher", ref.get("publisher")),
        ("address", ref.get("publisher_place")),
        ("edition", ref.get("edition")),
        ("school", ref.get("publisher") if entry_type == "phdthesis" else ""),
        ("doi", ref.get("doi")),
        ("url", ref.get("url")),
        # EndNote's ISBN field carries the ISSN for serials; emitting it
        # under the wrong name makes styles print a nonsense identifier.
        ("issn", ref.get("isbn") if entry_type == "article" else ""),
        ("isbn", ref.get("isbn") if entry_type != "article" else ""),
        ("language", ref.get("language")),
        ("keywords", ", ".join(ref.get("keywords") or [])),
        ("note", ref.get("research_notes")),
    ]
    if include_abstract:
        fields.append(("abstract", ref.get("abstract")))

    return [(name, value) for name, value in fields if value]


def format_entry(ref: dict, key: str | None = None, include_abstract: bool = False) -> str:
    entry_type = _entry_type(ref)
    key = key or citation_key(ref)

    lines = [f"@{entry_type}{{{key},"]
    for name, value in _fields(ref, include_abstract):
        escaped = _escape(str(value))
        # Braces around the title preserve its capitalisation against styles
        # that would otherwise lowercase it.
        rendered = f"{{{escaped}}}" if name == "title" else escaped
        lines.append(f"  {name:<10} = {{{rendered}}},")
    lines[-1] = lines[-1].rstrip(",")
    lines.append("}")
    return "\n".join(lines)


def format_bibtex(refs: list[dict], include_abstract: bool = False) -> str:
    """Render a .bib file, giving colliding citation keys an a/b/c suffix."""
    used: dict[str, int] = {}
    entries = []
    for ref in refs:
        key = citation_key(ref)
        if key in used:
            used[key] += 1
            key = f"{key}{chr(ord('a') + used[key] - 1)}"
        else:
            used[key] = 0
        entries.append(format_entry(ref, key=key, include_abstract=include_abstract))
    return "\n\n".join(entries)
