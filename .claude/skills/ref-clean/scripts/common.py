"""Shared helpers for the ref-clean scripts.

Every script here is read-only until the caller explicitly asks for a
commit; nothing imports EndNote or writes without going through
`endnotex9_mcp.commit`, which itself refuses to write while EndNote holds
the library open (`writeback._ensure_unlocked`) and always backs up first.
"""

from __future__ import annotations

import re
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "src"))

from endnotex9_mcp import config as configmod
from endnotex9_mcp import groups, parser, refstable
from endnotex9_mcp.refstable import VALUE_SEPARATOR


def load_library():
    """(rdb_dir, library_path, pdf_dir) from the same config the MCP server uses."""
    cfg = configmod.load_config()
    if not cfg.library_path:
        raise SystemExit("No library_path configured; run `endnotex9-mcp setup` first.")
    rdb = groups.rdb_dir(cfg.library_path)
    if not (rdb / "refs.MYD").exists():
        raise SystemExit(f"No refs.MYD under {rdb} -- is library_path correct?")
    return rdb, cfg.library_path, cfg.pdf_dir


def read_all_rows(rdb):
    return {int(r["id"]): r for r in refstable.read_rows(rdb)}


def text(row, col):
    v = row.get(col)
    if isinstance(v, (bytes, bytearray)):
        return v.decode("utf-8", "replace")
    return "" if v is None else str(v)


def values(row, col):
    return [p.strip() for p in text(row, col).split(VALUE_SEPARATOR) if p.strip()]


def doi_of(row):
    return parser.normalize_doi(text(row, "electronic_resource_number"))


def has_pdf(row):
    """Whether `row` has a usable PDF attachment.

    Goes through `refstable._pdf_files` (the same parser the read path and
    `index_pdfs` use) rather than a raw truthy check on `link_to_pdf`: that
    column can hold junk left over from a removed attachment (a stray
    separator) or a URL-form entry, either of which is non-empty but not a
    real attachment reference.
    """
    return bool(refstable._pdf_files(row))


def is_abbrev_author(name):
    if "," not in name:
        return False
    given = name.split(",", 1)[1].strip()
    tokens = given.replace(".", " ").split()
    return bool(tokens) and all(len(t) == 1 for t in tokens)


def looks_abbrev_journal(journal):
    if not journal:
        return False
    tokens = journal.replace(".", "").split()
    if not tokens:
        return False
    short_ratio = sum(1 for t in tokens if len(t) <= 5) / len(tokens)
    return not re.search(r"\b(of|the|and)\b", journal, re.I) and len(tokens) <= 5 and short_ratio >= 0.6


def norm_name(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z]", "", s.lower())


def surname_of(abbrev_author):
    return norm_name(abbrev_author.split(",", 1)[0])


def same_surname(local_author, crossref_family):
    """Whether a stored author and a CrossRef `family` name are the same person.

    Substring either way, because the two sources disagree about compound
    and hyphenated surnames often enough to matter ("Dall'Olio" vs
    "DallOlio", "Zhang" vs "Zhang-Li").
    """
    local, remote = surname_of(local_author), norm_name(crossref_family)
    return local == remote or local in remote or remote in local


def author_list_is_truncated(authors, crossref_names):
    """Whether `authors` looks like the head of `crossref_names`, cut short.

    The `et al.` import problem: a record keeps the first author or three
    and drops the rest. Completing that from CrossRef is only safe when the
    names actually line up, so this demands a strict positional surname
    prefix -- same people, same order, just fewer of them. Anything else
    (a different order, an extra author CrossRef does not know about, a
    genuine disagreement) fails here and is left for a human.

    A local list *longer* than CrossRef's is never treated as truncated:
    CrossRef itself is sometimes incomplete, and dropping authors a record
    already has would be the worse error.
    """
    if len(authors) >= len(crossref_names):
        return False
    return all(
        same_surname(author, crossref_names[i][0]) for i, author in enumerate(authors)
    )


NOTES_CITATION_TAIL_RE = re.compile(r"doi:\s*\S+\.?\s*$", re.I)


def notes_looks_like_pubmed_dump(note):
    lines = [ln.strip() for ln in note.split(VALUE_SEPARATOR)]
    return bool(note) and "eng" in lines and bool(NOTES_CITATION_TAIL_RE.search(note))
