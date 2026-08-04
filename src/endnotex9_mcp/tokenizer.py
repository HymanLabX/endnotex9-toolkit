"""Tokenize mixed Chinese/English text for SQLite FTS5.

FTS5's built-in tokenizers (unicode61/porter) treat a run of CJK characters
as a single opaque token, so a query for "记忆合金" never matches text
containing "形状记忆合金材料" — the whole run is one blob.

The standard pure-Python workaround: segment CJK ourselves (via jieba) at
index time *and* at query time, then hand FTS5 text in which every CJK run
has been replaced by its space-separated words. unicode61 splits on
whitespace, so each jieba word becomes its own indexable term. No custom C
tokenizer extension needed.

Latin text is deliberately left untouched — case, punctuation and word
boundaries are unicode61's job, and preserving the original characters is
what lets `snippet()` return readable excerpts instead of a lowercased,
de-punctuated reconstruction.
"""

from __future__ import annotations

import re

import jieba

# Unified CJK ideographs, extension A, and compatibility ideographs.
_CJK_CLASS = r"㐀-䶿一-鿿豈-﫿"
_CJK_RUN_RE = re.compile(f"[{_CJK_CLASS}]+")
_TERM_RE = re.compile(f"[{_CJK_CLASS}]+|[A-Za-z0-9][A-Za-z0-9_\\-.]*")


def _cut(run: str) -> list[str]:
    return [w for w in jieba.cut_for_search(run) if w.strip()]


def to_fts_text(text: str) -> str:
    """Return `text` with every CJK run replaced by its segmented words.

    Latin runs pass through unchanged so the stored copy stays human-readable
    for snippet extraction.
    """
    if not text:
        return ""
    return _CJK_RUN_RE.sub(lambda m: " " + " ".join(_cut(m.group(0))) + " ", text)


def tokenize(text: str) -> list[str]:
    """Split mixed CJK/Latin text into a flat list of query terms."""
    if not text:
        return []
    tokens: list[str] = []
    for match in _TERM_RE.finditer(text):
        run = match.group(0)
        if _CJK_RUN_RE.fullmatch(run):
            tokens.extend(_cut(run))
        else:
            tokens.append(run.lower())
    # Preserve order, drop duplicates.
    return list(dict.fromkeys(tokens))


def has_cjk(text: str) -> bool:
    return bool(_CJK_RUN_RE.search(text or ""))
