"""Cache the CrossRef record of every DOI in the working list (publisher PDF links, Elsevier PII,
container title). Already cached DOIs are skipped, so the stage can be rerun at any time.

Usage: python crossref_cache.py <project> [--workers 4]

No key and no email are sent. A DOI CrossRef does not know is cached as {"not_found": true}.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from collection_common import USER_AGENT, console_utf8, list_dois, load_project, safe_name


def fetch(doi: str) -> dict:
    url = "https://api.crossref.org/works/" + urllib.parse.quote(doi, safe="")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)["message"]
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return {"not_found": True}
            time.sleep(5 * (attempt + 1))  # 429 / 5xx: back off
        except (OSError, ValueError):
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"CrossRef lookup failed for {doi}")


def main(argv=None) -> int:
    console_utf8()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("project")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args(argv)
    project = load_project(args.project)
    project.cache_dir.mkdir(parents=True, exist_ok=True)
    todo = [
        d
        for d in dict.fromkeys(list_dois(project))
        if not (project.cache_dir / (safe_name(d) + ".json")).exists()
    ]
    print(f"{len(todo)} DOIs to look up", flush=True)
    done = failed = 0

    def one(doi):
        msg = fetch(doi)
        (project.cache_dir / (safe_name(doi) + ".json")).write_text(
            json.dumps(msg, ensure_ascii=False), encoding="utf-8"
        )
        time.sleep(0.2)
        return msg

    with ThreadPoolExecutor(args.workers) as pool:
        for doi, fut in [(d, pool.submit(one, d)) for d in todo]:
            try:
                fut.result()
                done += 1
            except RuntimeError as exc:
                failed += 1
                print(f"FAIL {doi}: {exc}", flush=True)
            if (done + failed) % 200 == 0:
                print(f"{done + failed}/{len(todo)}", flush=True)
    print(f"cached {done}, failed {failed} (rerun to retry)")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
