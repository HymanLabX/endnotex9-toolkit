"""Fetch CrossRef metadata by DOI. Read-only network calls, no library writes.

Results are cached to disk between runs (see `fetch_many`). That is not
only for speed: without it, `metadata.py`'s dry run and its `--commit` are
two independent sweeps of the API, so what gets written is not necessarily
what was reviewed and approved.
"""

from __future__ import annotations

import html
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# CrossRef asks callers to identify themselves and puts those who do into a
# faster, more reliable pool. Set CROSSREF_MAILTO to a real address to opt
# in; a made-up one is worse than none, so the default sends no address.
_MAILTO = os.environ.get("CROSSREF_MAILTO", "").strip()
HEADERS = {"User-Agent": f"lit-clean/1.0 (mailto:{_MAILTO})" if _MAILTO else "lit-clean/1.0"}


def fetch_one(doi, timeout=20, retries=3):
    url = f"https://api.crossref.org/works/{urllib.parse.quote(doi)}"
    req = urllib.request.Request(url, headers=HEADERS)
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return doi, json.load(resp)["message"]
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return doi, None
            if attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))
        except Exception:
            if attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))
    return doi, "ERROR"


def load_cache(cache_path):
    if not cache_path or not Path(cache_path).is_file():
        return {}
    try:
        cached = json.loads(Path(cache_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}  # a damaged cache is a reason to re-fetch, not to fail
    return cached if isinstance(cached, dict) else {}


def fetch_many(dois, max_workers=8, on_progress=None, cache_path=None, refresh=False):
    """dict[doi] -> CrossRef message (dict), None (not found), or 'ERROR'.

    With `cache_path`, answers already on disk are reused and only the
    missing DOIs are fetched; the cache is rewritten afterwards. This is
    what lets a `--commit` run write exactly what its dry run displayed
    instead of asking the API again and acting on whatever comes back the
    second time. `refresh=True` ignores what is cached and asks again.

    `'ERROR'` results are never cached: a network failure is not an answer,
    and caching it would make one bad afternoon permanent.
    """
    unique = sorted(set(dois))
    cached = {} if refresh else load_cache(cache_path)
    results = {doi: cached[doi] for doi in unique if doi in cached}
    todo = [doi for doi in unique if doi not in results]

    if on_progress and results:
        on_progress(len(results), len(unique))

    if todo:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(fetch_one, doi): doi for doi in todo}
            done = len(results)
            for fut in as_completed(futures):
                doi, message = fut.result()
                results[doi] = message
                done += 1
                if on_progress:
                    on_progress(done, len(unique))

    if cache_path:
        keep = {**cached, **{d: m for d, m in results.items() if m != "ERROR"}}
        try:
            Path(cache_path).parent.mkdir(parents=True, exist_ok=True)
            Path(cache_path).write_text(json.dumps(keep, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass  # the cache is an optimisation; losing it must not fail a run
    return results


def authors_of(message):
    """[(family, given), ...], html-unescaped, family-only entries dropped."""
    out = []
    for a in message.get("author", []):
        family = html.unescape((a.get("family") or "").strip())
        given = html.unescape((a.get("given") or "").strip())
        if family:
            out.append((family, given))
    return out


def container_title_of(message):
    titles = message.get("container-title") or []
    return html.unescape(titles[0].strip()) if titles else ""


def title_of(message):
    titles = message.get("title") or []
    return html.unescape(titles[0].strip()) if titles else ""


def abstract_of(message):
    raw = message.get("abstract") or ""
    if not raw:
        return ""
    # CrossRef abstracts are JATS XML fragments; strip tags for a plain-text field.
    stripped = re.sub(r"<[^>]+>", " ", raw)
    return " ".join(html.unescape(stripped).split())
