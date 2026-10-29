"""After the user has imported the file: check the library against import_manifest.json, and
list the PDFs found since the import file was built, for attaching by hand.

Usage: python verify_import.py <project> --library <Library.enl> [--ris <import.ris>]

--ris reads the expected links from an import file instead of import_manifest.json (for an
import built before the manifest existed).

Read-only. Reports how many imported DOIs are in the library, how many carry their attachment
link, whether every linked file exists, and DOIs that now appear more than once (imported
twice). PDFs filed after the build go to attach_after_import.csv with the EndNote record number
to attach them to (drag the PDF onto the record, or References > File Attachments > Attach File).
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter

from collection_common import (
    console_utf8,
    get,
    list_records,
    load_project,
    ok_states,
    pdf_path,
    read_ris,
    rec_doi,
    rec_title,
)
from library import data_dir, library_references


def manifest_from_ris(path) -> dict:
    attached = {}
    for rec in read_ris(path):
        for link in get(rec, "L1"):
            if link.startswith("internal-pdf://") and rec_doi(rec):
                attached[rec_doi(rec)] = link[len("internal-pdf://") :]
    return {"attached": attached, "left_out_held": []}


def check(project, enl: str, ris: str | None = None) -> dict:
    if ris:
        manifest = manifest_from_ris(project.dir / ris)
    else:
        manifest = json.loads((project.dir / "import_manifest.json").read_text(encoding="utf-8"))
    pdf_root = data_dir(enl) / "PDF"
    refs = library_references(enl)
    by_doi: dict[str, list[dict]] = {}
    for ref in refs:
        if ref["doi"]:
            by_doi.setdefault(ref["doi"], []).append(ref)
    dois = [d for d in (rec_doi(r) for r in list_records(project)) if d]
    expected = [d for d in dois if d not in set(manifest.get("left_out_held", []))]
    present = [d for d in expected if d in by_doi]
    linked = [
        d
        for d, rel in manifest["attached"].items()
        if any(rel in r["pdf_files"] for r in by_doi.get(d, []))
    ]
    missing_files = [rel for rel in manifest["attached"].values() if not (pdf_root / rel).is_file()]
    counts = Counter(r["doi"] for r in refs if r["doi"])
    twice = sorted(d for d in expected if counts[d] > 1)
    later = []
    titles = {rec_doi(r): rec_title(r) for r in list_records(project)}
    for doi, entry in ok_states(project).items():
        if doi in titles and doi not in manifest["attached"]:
            src = pdf_path(project, doi, entry)
            recs = by_doi.get(doi, [])
            if src and not any(r["pdf_files"] for r in recs):
                later.append(
                    {
                        "doi": doi,
                        "title": titles[doi],
                        "pdf": str(src),
                        "endnote_record": ",".join(str(r["rec_number"]) for r in recs),
                    }
                )
    return {
        "library_records": len(refs),
        "expected": len(expected),
        "present": len(present),
        "attached_expected": len(manifest["attached"]),
        "attached_linked": len(linked),
        "linked_files_missing": missing_files,
        "imported_twice": twice,
        "attach_after_import": later,
    }


def main(argv=None) -> int:
    console_utf8()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("project")
    ap.add_argument("--library", required=True)
    ap.add_argument("--ris")
    args = ap.parse_args(argv)
    project = load_project(args.project)
    r = check(project, args.library, args.ris)
    print(
        f"library records {r['library_records']}; imported DOIs present "
        f"{r['present']}/{r['expected']}; PDF links {r['attached_linked']}/"
        f"{r['attached_expected']}; linked files missing {len(r['linked_files_missing'])}; "
        f"DOIs imported twice {len(r['imported_twice'])}"
    )
    for rel in r["linked_files_missing"][:10]:
        print(f"  missing file: {rel}")
    for doi in r["imported_twice"][:10]:
        print(f"  twice: {doi}")
    if r["attach_after_import"]:
        path = project.dir / "attach_after_import.csv"
        with path.open("w", encoding="utf-8-sig", newline="") as fh:
            w = csv.DictWriter(fh, ["endnote_record", "doi", "title", "pdf"])
            w.writeheader()
            w.writerows(r["attach_after_import"])
        print(
            f"PDFs found after the build, to attach by hand: {len(r['attach_after_import'])}"
            f" -> {path.name}"
        )
    ok = (
        r["present"] == r["expected"]
        and r["attached_linked"] == r["attached_expected"]
        and not r["linked_files_missing"]
        and not r["imported_twice"]
    )
    print("OK" if ok else "CHECK THE ITEMS ABOVE")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
