"""Fetch CrossRef metadata by DOI. Read-only network calls, no library writes."""

from __future__ import annotations

import html
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

HEADERS = {"User-Agent": "ref-clean/1.0 (mailto:research-assistant@example.com)"}


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


def fetch_many(dois, max_workers=8, on_progress=None):
    """dict[doi] -> CrossRef message (dict), None (not found), or 'ERROR'."""
    results = {}
    unique = sorted(set(dois))
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(fetch_one, doi): doi for doi in unique}
        done = 0
        for fut in as_completed(futures):
            doi, message = fut.result()
            results[doi] = message
            done += 1
            if on_progress:
                on_progress(done, len(unique))
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
