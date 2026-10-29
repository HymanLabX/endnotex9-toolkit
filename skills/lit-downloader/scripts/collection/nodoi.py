"""Find DOIs for working-list records that have none, by CrossRef title search.

Usage: python nodoi.py <project> [--apply]

Writes nodoi_crossref.csv. A match needs a near-identical title (similarity >= 0.93) and the same
publication year (+-1), and must not duplicate a DOI already in the list. With --apply, accepted
DOIs are written into the list as "DO  - " lines (backup: <list>.before_nodoi.ris) and the
CrossRef records are cached for the later stages. No key or email is sent.
"""

from __future__ import annotations

import argparse
import csv
import difflib
import json
import re
import shutil
import sys
import time
import unicodedata
import urllib.parse
import urllib.request

from collection_common import USER_AGENT, console_utf8, load_project, safe_name

GREEK = {
    "α": "alpha",  # noqa: RUF001
    "β": "beta",
    "γ": "gamma",  # noqa: RUF001
    "δ": "delta",
    "ω": "omega",
    "ε": "epsilon",
    "μ": "mu",
}


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "")
    for g, name in GREEK.items():
        s = s.replace(g, f" {name} ")
    s = re.sub(r"<[^>]+>", " ", s)  # CrossRef titles may carry <i>, <sub> markup
    return " ".join(re.sub(r"[^a-z0-9]+", " ", s.lower()).split())


def best_match(title: str, year: int | None, items: list[dict], known: set[str]):
    best = None
    for it in items:
        sim = difflib.SequenceMatcher(None, norm(title), norm((it.get("title") or [""])[0])).ratio()
        parts = (it.get("issued") or {}).get("date-parts") or [[None]]
        y = parts[0][0] if parts and parts[0] else None
        if best is None or sim > best[0]:
            best = (sim, it, y)
    ok = bool(
        best
        and best[0] >= 0.93
        and year
        and best[2]
        and abs(best[2] - year) <= 1
        and best[1]["DOI"].lower() not in known
    )
    return best, ok


def main(argv=None) -> int:
    console_utf8()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("project")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)
    project = load_project(args.project)
    ris = project.list_path
    lines = ris.read_text(encoding="utf-8-sig").splitlines()
    recs, cur = [], {}
    for i, line in enumerate(lines):
        if line.startswith("TY  - "):
            cur = {}
        elif line.startswith("ER  -"):
            if "DO" not in cur:
                recs.append({**cur, "er_line": i})
        elif line[2:6] == "  - ":
            cur.setdefault(line[:2], line[6:].strip())
    known = {line[6:].strip().lower() for line in lines if line.startswith("DO  - ")}
    print(f"{len(recs)} records without DOI", flush=True)
    rows, accepted = [], {}
    for n, r in enumerate(recs, 1):
        title = r.get("TI") or r.get("T1") or ""
        journal = r.get("T2") or r.get("JO") or r.get("JF") or ""
        year = int(r["PY"][:4]) if r.get("PY", "")[:4].isdigit() else None
        q = urllib.parse.urlencode({"query.bibliographic": f"{title} {journal}", "rows": 5})
        try:
            req = urllib.request.Request(
                f"https://api.crossref.org/works?{q}", headers={"User-Agent": USER_AGENT}
            )
            items = json.load(urllib.request.urlopen(req, timeout=60))["message"]["items"]
        except (OSError, ValueError) as exc:
            print(f"{n:3} lookup failed: {exc}", flush=True)
            items = []
        best, ok = best_match(title, year, items, known)
        it = best[1] if best else {}
        rows.append(
            {
                "no": n,
                "year": year,
                "journal": journal,
                "title": title,
                "doi": it.get("DOI", "").lower(),
                "title_similarity": round(best[0], 3) if best else "",
                "crossref_year": best[2] if best else "",
                "crossref_journal": (it.get("container-title") or [""])[0],
                "crossref_title": (it.get("title") or [""])[0],
                "accepted": "yes" if ok else "no",
            }
        )
        if ok:
            accepted[r["er_line"]] = it
        print(
            f"{n:3} {'OK ' if ok else 'no '} {rows[-1]['title_similarity']!s:5} {title[:70]}",
            flush=True,
        )
        time.sleep(2)
    if rows:
        with (project.dir / "nodoi_crossref.csv").open("w", encoding="utf-8-sig", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
    print(f"accepted {len(accepted)} of {len(recs)}; details in nodoi_crossref.csv", flush=True)
    if args.apply and accepted:
        shutil.copy2(ris, ris.with_suffix(".before_nodoi.ris"))
        out = []
        for i, line in enumerate(lines):
            if i in accepted:
                out.append(f"DO  - {accepted[i]['DOI'].lower()}")
            out.append(line)
        ris.write_text("\r\n".join(out) + "\r\n", encoding="utf-8", newline="")
        project.cache_dir.mkdir(parents=True, exist_ok=True)
        for it in accepted.values():
            (project.cache_dir / (safe_name(it["DOI"].lower()) + ".json")).write_text(
                json.dumps(it, ensure_ascii=False), encoding="utf-8"
            )
        print(f"added {len(accepted)} DOIs to {ris.name}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
