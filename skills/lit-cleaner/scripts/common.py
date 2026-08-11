"""Shared helpers for the lit-cleaner scripts.

Every script here is read-only until the caller explicitly asks for a
commit; nothing imports EndNote or writes without going through
`endnotex9_mcp.commit`, which itself refuses to write while EndNote holds
the library open (`writeback._ensure_unlocked`) and always backs up first.
"""

from __future__ import annotations

import re
import sys
import unicodedata
from importlib.util import find_spec
from pathlib import Path


def package_root() -> Path | None:
    """The endnotex9-mcp tree these scripts ship inside, or None if detached.

    Do not count parent directories here. The same scripts run from a source
    checkout and from an installed plugin, where Claude Code has copied the
    plugin somewhere else entirely, so walk up looking for the package instead
    of assuming a depth.
    """
    for parent in Path(__file__).resolve().parents:
        if (parent / "src" / "endnotex9_mcp" / "__init__.py").is_file():
            return parent
    return None


PACKAGE_ROOT = package_root()
if PACKAGE_ROOT is not None:
    sys.path.insert(0, str(PACKAGE_ROOT / "src"))
elif find_spec("endnotex9_mcp") is None:
    raise SystemExit(
        "Cannot find endnotex9_mcp. Run this script from an endnotex9-mcp checkout "
        "or plugin directory, or install the package into the active environment."
    )

from endnotex9_mcp import config as configmod  # noqa: E402
from endnotex9_mcp import groups, parser, refstable  # noqa: E402
from endnotex9_mcp.refstable import VALUE_SEPARATOR  # noqa: E402


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


def is_mostly_latin(s):
    """Whether `s` is written in the Latin alphabet.

    Guards the heuristics below, all of which reason about word counts and
    word lengths and mean nothing outside a space-separated alphabetic
    script.
    """
    letters = [c for c in s if c.isalpha()]
    if not letters:
        return False
    return sum(1 for c in letters if c.isascii()) / len(letters) >= 0.8


def looks_abbrev_journal(journal):
    """Whether `journal` looks like an abbreviated title ("Acta Mater").

    Latin-script only, deliberately. A Chinese journal name is a handful of
    characters with no spaces, which every clause below reads as "few, short
    words" — `材料科学` scored as an abbreviation, and the caller would then
    happily overwrite it with CrossRef's longer English title. The name is
    not abbreviated, it is just not English.
    """
    if not journal or not is_mostly_latin(journal):
        return False
    tokens = journal.replace(".", "").split()
    if not tokens:
        return False
    short_ratio = sum(1 for t in tokens if len(t) <= 5) / len(tokens)
    return (
        not re.search(r"\b(of|the|and)\b", journal, re.I)
        and len(tokens) <= 5
        and short_ratio >= 0.6
    )


def norm_name(s):
    """Fold a personal name to a comparable key.

    Latin names fold to ASCII letters, so accents and punctuation stop
    mattering. A name with no Latin letters at all — a CJK name, which this
    project's libraries are full of — would fold to the empty string, and an
    empty key is a substring of everything, so `same_surname` would call it
    a match against any name on earth. Those keep their own characters
    instead, which compare equal to each other and unequal to Latin ones.
    """
    folded = re.sub(
        r"[^a-z]",
        "",
        unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii").lower(),
    )
    if folded:
        return folded
    return re.sub(r"[\s\W_]", "", unicodedata.normalize("NFKC", s).lower())


def surname_of(abbrev_author):
    return norm_name(abbrev_author.split(",", 1)[0])


def same_surname(local_author, crossref_family):
    """Whether a stored author and a CrossRef `family` name are the same person.

    Substring either way, because the two sources disagree about compound
    and hyphenated surnames often enough to matter ("Dall'Olio" vs
    "DallOlio", "Zhang" vs "Zhang-Li"). An empty key on either side is never
    a match — see `norm_name`; treating it as one made every CJK-named
    author match every CrossRef author, which is exactly the case where
    guessing is least defensible.
    """
    local, remote = surname_of(local_author), norm_name(crossref_family)
    if not local or not remote:
        return False
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
    return all(same_surname(author, crossref_names[i][0]) for i, author in enumerate(authors))


def norm_title(t):
    """Fold a title to a comparison key, keeping non-Latin scripts intact.

    Same trap as `norm_name`: folding to ASCII turns "退火对形状记忆合金 A"
    and "时效对形状记忆合金 A" into the same key, so two unrelated Chinese
    papers by one author in one year would look like duplicates.
    """
    folded = re.sub(
        r"[^a-z0-9]",
        "",
        unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode("ascii").lower(),
    )
    stripped = re.sub(r"[\s\W_]", "", unicodedata.normalize("NFKC", t).lower())
    # Prefer the ASCII fold only when it kept essentially the whole title;
    # otherwise most of the text was non-Latin and the fold threw it away.
    return folded if len(folded) >= 0.8 * len(stripped) else stripped


NOTES_CITATION_TAIL_RE = re.compile(r"doi:\s*\S+\.?\s*$", re.I)


def notes_looks_like_pubmed_dump(note):
    lines = [ln.strip() for ln in note.split(VALUE_SEPARATOR)]
    return bool(note) and "eng" in lines and bool(NOTES_CITATION_TAIL_RE.search(note))
