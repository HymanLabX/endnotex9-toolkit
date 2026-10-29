"""Open-access stage: look up every working-list DOI that still lacks a PDF on OpenAlex and
Semantic Scholar (batch APIs, no key, no email) and fetch the legal copies they list.

Usage: python oa.py <project> [--workers 4] [--host-gap 3] [--dry-run]

Safe to rerun at any stage: records that have a PDF from either state file are never touched,
and a URL already tried for a DOI is not fetched again. Publisher hosts that refuse scripts
(ScienceDirect, IOP, T&F, AIP, APS, SAGE, ...) are left to the browser rounds. A repository copy
without the DOI printed in it (an accepted manuscript) needs title similarity >= 0.92 and is
recorded as an author version. Writes pdf_state.json and the URL caches s2_urls.json /
openalex_urls.json, which the opener and watcher use to recognise repository copies.
"""

from __future__ import annotations

import argparse
import collections
import datetime
import json
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

from collection_common import (
    HELPER_DIR,
    STATE_OA,
    console_utf8,
    list_records,
    load_project,
    load_validator,
    ok_states,
    read_json,
    rec_doi,
    rec_title,
    safe_name,
    write_json_atomic,
)

sys.path.insert(0, str(HELPER_DIR))
from retrieval import fetch_public

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126 Safari/537.36"
)
SKIP = re.compile(
    r"linkinghub\.elsevier|sciencedirect|scientific\.net|iopscience|perfdrive|"
    r"tandfonline|pubs\.aip\.org|journals\.aps|sagepub",
    re.I,
)


def get_json(req, what: str):
    for attempt in range(5):
        try:
            return json.load(urllib.request.urlopen(req, timeout=60))
        except (OSError, ValueError) as exc:
            print(f"{what}: {exc}; retrying", flush=True)
            time.sleep(20 * (attempt + 1))
    raise SystemExit(f"{what} failed")


def lookup(todo: list[str]) -> dict[str, list[tuple[str, str]]]:
    found = collections.defaultdict(list)
    for i in range(0, len(todo), 50):  # OpenAlex: 50 DOIs per filter request
        q = urllib.parse.quote("doi:" + "|".join(todo[i : i + 50]), safe=":|/")
        api = (
            "https://api.openalex.org/works?per-page=50"
            f"&select=doi,best_oa_location,primary_location,locations&filter={q}"
        )
        res = get_json(
            urllib.request.Request(api, headers={"User-Agent": BROWSER_UA}), f"OpenAlex batch {i}"
        )
        for w in res.get("results", []):
            d = (w.get("doi") or "").lower().removeprefix("https://doi.org/")
            locs = [
                w.get("best_oa_location") or {},
                w.get("primary_location") or {},
                *(w.get("locations") or []),
            ]
            found[d].extend(("openalex", loc["pdf_url"]) for loc in locs if loc.get("pdf_url"))
        time.sleep(1.5)
    for i in range(0, len(todo), 400):  # Semantic Scholar: 400 DOIs per batch request
        chunk = todo[i : i + 400]
        req = urllib.request.Request(
            "https://api.semanticscholar.org/graph/v1/paper/batch?fields=openAccessPdf",
            data=json.dumps({"ids": ["DOI:" + d for d in chunk]}).encode(),
            headers={"Content-Type": "application/json", "User-Agent": BROWSER_UA},
        )
        for d, item in zip(chunk, get_json(req, f"Semantic Scholar batch {i}"), strict=False):
            u = ((item or {}).get("openAccessPdf") or {}).get("url") or ""
            if u:
                found[d].append(("semanticscholar", u))
        time.sleep(3)
    return found


def as_list(v) -> list[str]:
    return [] if not v else [v] if isinstance(v, str) else list(v)


def norm_url(u: str) -> str:
    return re.sub(r"^https?://", "", u.strip()).rstrip("/").lower()


def new_candidates(found, state, caches) -> dict[str, list[tuple[str, str]]]:
    """URLs not tried before. Older runs recorded no `tried` list; their cached URLs count as tried."""
    out = {}
    for d, pairs in found.items():
        tried = set(state.get(d, {}).get("tried", []))
        if d in state and "tried" not in state[d]:
            tried |= {norm_url(u) for c in caches.values() for u in as_list(c.get(d))}
        new = {norm_url(u): (src, u) for src, u in pairs if not SKIP.search(u)}
        new = [v for k, v in new.items() if k not in tried]
        if new:
            out[d] = new
    return out


class Fetcher:
    def __init__(self, host_gap: float):
        self.gap, self.lock, self.last = host_gap, threading.Lock(), {}

    def __call__(self, url: str) -> tuple[str, bytes]:
        host = urlparse(url).netloc
        with self.lock:
            wait = self.last.get(host, 0) + self.gap - time.time()
            self.last[host] = max(time.time(), self.last.get(host, 0) + self.gap)
        if wait > 0:
            time.sleep(wait)  # polite pacing per host
        # The selected-records mode's guarded transfer: public hosts only (a landing page's
        # <meta citation_pdf_url> may name any host), redirects re-checked, a 100 MB cap, and the
        # OS certificate store, which repositories serving an incomplete chain need.
        data, final = fetch_public(
            url, {"User-Agent": BROWSER_UA, "Accept": "application/pdf,*/*"}, timeout=60
        )
        return final, data


META_PDF = (
    re.compile(rb"<meta[^>]+name=[\"']citation_pdf_url[\"'][^>]+content=[\"']([^\"']+)", re.I),
    re.compile(rb"<meta[^>]+content=[\"']([^\"']+)[\"'][^>]+name=[\"']citation_pdf_url", re.I),
)


def fetch_pdf(fetch, url: str) -> tuple[str, bytes]:
    final, data = fetch(url)
    if not data.startswith(b"%PDF-"):
        # Landing pages: the publisher's /pdf variant, or the <meta citation_pdf_url> it declares.
        if "mdpi.com" in final:
            final, data = fetch(final.rstrip("/") + "/pdf")
        elif "jstage.jst.go.jp" in final and "_article" in final:
            final, data = fetch(final.replace("/_article", "/_pdf"))
    if not data.startswith(b"%PDF-"):
        m = META_PDF[0].search(data) or META_PDF[1].search(data)
        if m:
            final, data = fetch(m.group(1).decode("utf-8", "replace").replace("&amp;", "&"))
    if not data.startswith(b"%PDF-"):
        raise RuntimeError(f"non-PDF from {final[:80]}")
    return final, data


def identify(validate_pdf, data: bytes, doi: str, title: str, final: str) -> tuple[dict, str]:
    try:
        return validate_pdf(data, doi, title), ""
    except RuntimeError as exc:
        # Old J-STAGE PDFs print no DOI; their URL is built from the DOI's volume/page.
        tail = re.sub(r"^[^.]+\.", "", doi.split("/", 1)[1]).replace(".", "_").lower()
        tokens = set(re.split(r"[/_.\-]", final.lower()))
        if "jstage.jst.go.jp" in final and tail and all(t in tokens for t in tail.split("_") if t):
            return {"identity": "J-STAGE URL matches DOI", "title_similarity": None}, ""
        if "DOI was not found" not in str(exc):
            raise
    info = validate_pdf(data, "", title)
    if info["title_similarity"] < 0.92:
        raise RuntimeError(f"no DOI in PDF and title similarity only {info['title_similarity']}")
    info["doi_match"] = False
    return info, "-author-version"


def main(argv=None) -> int:
    console_utf8()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("project")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--host-gap", type=float, default=3.0)
    ap.add_argument("--dry-run", action="store_true", help="look up and list new URLs, fetch none")
    args = ap.parse_args(argv)
    project = load_project(args.project)
    project.pdf_dir.mkdir(exist_ok=True)
    state_path = project.dir / STATE_OA
    state = read_json(state_path)
    titles = {rec_doi(r): rec_title(r) for r in list_records(project) if rec_doi(r)}
    have = set(ok_states(project))
    todo = [d for d in titles if d not in have and not d.startswith("arxiv-")]
    print(f"{len(todo)} DOIs without PDF", flush=True)
    found = lookup(todo)
    caches = {
        name: read_json(project.dir / name) for name in ("openalex_urls.json", "s2_urls.json")
    }
    cands = new_candidates(found, state, caches)
    hosts = collections.Counter(urlparse(c[0][1]).netloc for c in cands.values())
    print(
        f"OA URL for {len(found)} DOIs; new for {len(cands)}: {dict(hosts.most_common(12))}",
        flush=True,
    )
    if args.dry_run:
        for d, c in cands.items():
            print(f"  {d}  {c[0][1][:100]}")
        return 0
    for d, pairs in found.items():
        for src, name in (("openalex", "openalex_urls.json"), ("semanticscholar", "s2_urls.json")):
            urls = list(
                dict.fromkeys(as_list(caches[name].get(d)) + [u for s, u in pairs if s == src])
            )
            if urls:
                caches[name][d] = urls
    for name, data in caches.items():
        write_json_atomic(project.dir / name, data)

    validate_pdf, _ = load_validator()
    fetch = Fetcher(args.host_gap)
    today = datetime.date.today().isoformat()

    def work(doi):
        errors = []
        for src, url in cands[doi][:3]:
            try:
                final, data = fetch_pdf(fetch, url)
                info, suffix = identify(validate_pdf, data, doi, titles[doi], final)
                return src + suffix, data, info, errors
            except (OSError, RuntimeError, ValueError) as exc:
                errors.append(str(exc)[:120])
        raise RuntimeError("; ".join(errors))

    ok = n = 0
    with ThreadPoolExecutor(args.workers) as pool:
        futs = {pool.submit(work, d): d for d in cands}
        for fut in as_completed(futs):
            d = futs[fut]
            n += 1
            prev = state.get(d, {})
            tried = sorted(set(prev.get("tried", [])) | {norm_url(u) for _, u in cands[d][:3]})
            try:
                src, data, info, _ = fut.result()
                path = project.pdf_dir / (safe_name(d) + ".pdf")
                path.write_bytes(data)
                state[d] = {
                    "status": "ok",
                    "source": src,
                    "file": path.name,
                    "checked": today,
                    "tried": tried,
                    **info,
                }
                ok += 1
                print(f"OK   {d}  ({src})", flush=True)
            except RuntimeError as exc:
                state[d] = {
                    **prev,
                    "status": prev.get("status", "oa_failed"),
                    "checked": today,
                    "tried": tried,
                    "error": str(exc)[:200],
                }
                print(f"FAIL {d}  {str(exc)[:120]}", flush=True)
            if n % 25 == 0:
                write_json_atomic(state_path, state)
    write_json_atomic(state_path, state)
    print(f"FINISHED: {len(cands)} DOIs with new OA URLs, new PDFs {ok}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
