"""Merge the raw search exports, drop in-batch duplicates and records the library already has, and
sort the rest into tiers for the user to review.

Usage: python merge.py <project> [--library <Library.enl>] [--existing <a.ris> ...]
                                 [--rules <rules.py>]

Reads every `raw/*.ris`. A record counts as already held when its DOI or normalized title matches
the library (read-only, through the refs table) or an `--existing` RIS file.

`--rules` names a topic-specific module defining `classify(rec) -> (tier, why)`, where `rec` maps
each RIS tag to its list of values. Tiers are free-form letters; `X` means excluded. Without
rules every new record is tier `A`. Writes `tiers.csv` (one row per record, with the reason) and
`tier_<T>.ris` for every tier except `X` and `DUP`.
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import sys
from collections import Counter

from collection_common import (
    console_utf8,
    first,
    load_project,
    norm_title,
    read_ris,
    rec_doi,
    rec_journal,
    rec_title,
    write_ris,
)


def held_keys(library: str | None, existing: list[str]) -> tuple[set[str], set[str]]:
    dois, titles = set(), set()
    for path in existing:
        for rec in read_ris(path):
            if rec_doi(rec):
                dois.add(rec_doi(rec))
            titles.add(norm_title(rec_title(rec)))
    if library:
        from library import library_references

        for ref in library_references(library):
            if ref["doi"]:
                dois.add(ref["doi"].lower())
            titles.add(norm_title(ref["title"]))
    titles.discard("")
    return dois, titles


def load_rules(path: str | None):
    if not path:
        return lambda rec: ("A", "no topic rules")
    spec = importlib.util.spec_from_file_location("topic_rules", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.classify


def dedupe(recs: list) -> list:
    out, seen = [], set()
    for rec in recs:
        key = rec_doi(rec) or first(rec, "AN").lower() or norm_title(rec_title(rec))
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        out.append(rec)
    return out


def merge(raw: list, classify, held_dois: set[str], held_titles: set[str]) -> list[dict]:
    rows = []
    for rec in dedupe(raw):
        doi = rec_doi(rec)
        if (doi and doi in held_dois) or norm_title(rec_title(rec)) in held_titles:
            tier, why = "DUP", "already held"
        else:
            tags: dict[str, list[str]] = {}
            for tag, value in rec:
                tags.setdefault(tag, []).append(value)
            tier, why = classify(tags)
        rows.append(
            {
                "tier": tier,
                "why": why,
                "year": first(rec, "PY"),
                "doi": doi,
                "title": rec_title(rec),
                "journal": rec_journal(rec),
                "type": first(rec, "TY"),
                "_rec": rec,
            }
        )
    return rows


def main(argv=None) -> int:
    console_utf8()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("project")
    ap.add_argument("--library", help="EndNote .enl whose records count as already held")
    ap.add_argument("--existing", nargs="*", default=[], help="RIS files of already-held records")
    ap.add_argument("--rules", help="topic rules module with classify(rec) -> (tier, why)")
    args = ap.parse_args(argv)
    project = load_project(args.project)
    files = sorted((project.dir / "raw").glob("*.ris"))
    if not files:
        raise SystemExit(f"no exports in {project.dir / 'raw'}")
    raw = [rec for f in files for rec in read_ris(f)]
    held_dois, held_titles = held_keys(args.library, args.existing)
    rows = merge(raw, load_rules(args.rules), held_dois, held_titles)
    counts = Counter(r["tier"] for r in rows)
    print(f"raw {len(raw)} from {len(files)} files, unique {len(rows)}, tiers {dict(counts)}")
    with (project.dir / "tiers.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(
            fh, ["tier", "why", "year", "type", "doi", "title", "journal"], extrasaction="ignore"
        )
        w.writeheader()
        w.writerows(sorted(rows, key=lambda r: (r["tier"], r["year"])))
    for tier in sorted(counts):
        if tier in ("X", "DUP"):
            continue
        write_ris(project.dir / f"tier_{tier}.ris", [r["_rec"] for r in rows if r["tier"] == tier])
        print(f"  tier_{tier}.ris  {counts[tier]}")
    no_doi = sum(1 for r in rows if r["tier"] not in ("X", "DUP") and not r["doi"])
    print(f"kept records without DOI: {no_doi}; reasons per record in tiers.csv")
    return 0


if __name__ == "__main__":
    sys.exit(main())
