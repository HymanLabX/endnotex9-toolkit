"""Where a collection stands: PDFs found, and why every other record is still missing.

Usage: python status.py <project> [--json]

Categories for records without a PDF: no DOI; no link to open (no PDF link known); never tried
(set aside: an excluded publisher or blocked host); failed once (waits for the retry round);
failed twice or more (manual list or document delivery).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter

from collection_common import (
    console_utf8,
    crossref_message,
    doi_prefix,
    list_records,
    load_project,
    oa_urls,
    ok_states,
    read_open_log,
    rec_doi,
)
from open_links import url_for


def summarize(project) -> dict:
    recs = list_records(project)
    dois = [rec_doi(r) for r in recs]
    have = ok_states(project)
    log = read_open_log(project)
    misses = Counter(e["doi"] for e in log if not e["got"])
    repo = oa_urls(project)
    groups: dict[str, list[str]] = {
        "no DOI": [],
        "no link": [],
        "never tried": [],
        "failed once": [],
        "failed twice+": [],
    }
    for d in dois:
        if not d:
            groups["no DOI"].append("")
        elif d in have:
            continue
        elif misses[d] >= 2:
            groups["failed twice+"].append(d)
        elif misses[d] == 1:
            groups["failed once"].append(d)
        elif not url_for(d, crossref_message(project, d), repo.get(d, []), project.blocked_hosts):
            groups["no link"].append(d)
        else:
            groups["never tried"].append(d)
    sources = Counter(have[d].get("source", "?") for d in dois if d in have)
    today = time.strftime("%Y-%m-%d")
    return {
        "records": len(recs),
        "with_pdf": sum(1 for d in dois if d in have),
        "sources": dict(sources.most_common()),
        "missing": {k: len(v) for k, v in groups.items()},
        "missing_by_prefix": {
            k: Counter(doi_prefix(d) for d in v if d).most_common(8)
            for k, v in groups.items()
            if k != "no DOI"
        },
        "sciencedirect_today": sum(
            1 for e in log if e.get("date") == today and "sciencedirect.com" in e["url"]
        ),
        "sciencedirect_daily": project.sciencedirect_daily,
    }


def main(argv=None) -> int:
    console_utf8()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("project")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    s = summarize(load_project(args.project))
    if args.json:
        print(json.dumps(s, ensure_ascii=False, indent=1))
        return 0
    pct = 100 * s["with_pdf"] / s["records"] if s["records"] else 0
    print(f"records {s['records']}, with PDF {s['with_pdf']} ({pct:.0f}%)  sources {s['sources']}")
    for k, n in s["missing"].items():
        extra = s["missing_by_prefix"].get(k, [])
        print(f"  {k:14} {n:5}  {extra}")
    print(f"ScienceDirect opened today: {s['sciencedirect_today']} of {s['sciencedirect_daily']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
