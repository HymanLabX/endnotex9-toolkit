"""Trim the reviewed tiers into the working list, recording every removal with its reason.

Usage: python trim.py <project> --input tier_A.ris tier_B.ris
                      [--journal-only] [--languages English,Chinese]
                      [--old-lowcite 2015:5] [--drop-errata] [--dry-run]

--journal-only     keep only TY JOUR (drops proceedings papers and book chapters; journal
                   articles from conference special issues stay, since WoS types them JOUR)
--languages        keep only these LA values; records without LA are kept
--old-lowcite Y:N  drop records published in or before year Y with fewer than N citations
                   (read from the WoS note "Times Cited in Web of Science Core Collection: n")
--drop-errata      drop corrections ("... (vol 853, 157090, 2021)", "Erratum", "Corrigendum")

Writes the project's working list (backing up an existing one as <name>.before_trim.ris) and
removed_trim.csv. Trim before the downloads start; PDFs fetched for removed records are wasted.
"""

from __future__ import annotations

import argparse
import csv
import re
import shutil
import sys

from collection_common import (
    console_utf8,
    first,
    get,
    load_project,
    read_ris,
    rec_doi,
    rec_journal,
    rec_title,
    write_ris,
)
from merge import dedupe

CITED = re.compile(r"Times Cited in Web of Science Core Collection:\s*(\d+)", re.I)
ERRATUM = re.compile(
    r"\(vol\.?\s*\d+[^)]*\b(19|20)\d\d\)\s*$|^(erratum|corrigendum|correction)\b|"
    r"^(erratum|corrigendum|correction) (to|for|on)\b",
    re.I,
)


def times_cited(rec) -> int | None:
    for note in get(rec, "N1"):
        m = CITED.search(note)
        if m:
            return int(m.group(1))
    return None


def reasons(rec, args) -> list[str]:
    why = []
    if args.journal_only and first(rec, "TY") != "JOUR":
        why.append(f"type {first(rec, 'TY') or '?'}")
    lang = first(rec, "LA")
    if args.languages and lang and lang.lower() not in args.languages:
        why.append(f"language {lang}")
    if args.old_lowcite:
        year, cites = (int(x) for x in args.old_lowcite.split(":"))
        py = first(rec, "PY")[:4]
        tc = times_cited(rec)
        if py.isdigit() and int(py) <= year and tc is not None and tc < cites:
            why.append(f"{py}, cited {tc}")
    if args.drop_errata and ERRATUM.search(rec_title(rec)):
        why.append("erratum")
    return why


def main(argv=None) -> int:
    console_utf8()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("project")
    ap.add_argument("--input", nargs="+", required=True, help="RIS files inside the project")
    ap.add_argument("--journal-only", action="store_true")
    ap.add_argument("--languages", default="")
    ap.add_argument("--old-lowcite", default="", metavar="YEAR:CITES")
    ap.add_argument("--drop-errata", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    args.languages = {x.strip().lower() for x in args.languages.split(",") if x.strip()}
    project = load_project(args.project)
    recs = dedupe([rec for name in args.input for rec in read_ris(project.dir / name)])
    keep, removed = [], []
    for rec in recs:
        why = reasons(rec, args)
        if why:
            removed.append(
                {
                    "reason": "; ".join(why),
                    "type": first(rec, "TY"),
                    "year": first(rec, "PY"),
                    "times_cited": times_cited(rec),
                    "language": first(rec, "LA"),
                    "title": rec_title(rec),
                    "journal": rec_journal(rec),
                    "doi": rec_doi(rec),
                }
            )
        else:
            keep.append(rec)
    tally: dict[str, int] = {}
    for row in removed:
        for part in row["reason"].split("; "):
            key = part.split(" ")[0] if not part[:4].isdigit() else "old-lowcite"
            tally[key] = tally.get(key, 0) + 1
    print(
        f"input {len(recs)}, keep {len(keep)}, remove {len(removed)}; by reason (overlapping) {tally}"
    )
    if args.dry_run:
        return 0
    if project.list_path.exists():
        shutil.copy2(project.list_path, project.list_path.with_suffix(".before_trim.ris"))
    write_ris(project.list_path, keep)
    with (project.dir / "removed_trim.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(
            fh, ["reason", "type", "year", "times_cited", "language", "title", "journal", "doi"]
        )
        w.writeheader()
        w.writerows(removed)
    print(f"wrote {project.list_path.name} ({len(keep)}) and removed_trim.csv ({len(removed)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
