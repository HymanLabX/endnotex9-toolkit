"""Build the EndNote import file: the working list with an `internal-pdf://` link for every
verified PDF, the PDFs copied into the library's own attachment folder.

Usage: python build_import.py <project> --library <Library.enl> [--out import.ris]
                              [--label TEXT] [--journal-case] [--keep-held]
                              [--move-orphans] [--dry-run]

The library's database is only read; its attachment folder is written to. Each PDF goes to
<Library>.Data/PDF/<n>/<file>, where <n> is a stable 10-digit number derived from the DOI; a
number the library already uses, or a folder holding someone else's file, is skipped for the next
candidate. EndNote keeps the L1 text verbatim when the user imports the file (File > Import >
File, Reference Manager (RIS), Unicode (UTF-8)), so the attachments arrive inside the library
without any write to its database.

--label         write LB (EndNote Label) on every record, to find the batch later
                (default: `label` from project.json)
--journal-case  replace an all-capitals journal name (as WoS exports it) with CrossRef's
                container title
--keep-held     also include records the library already has, by DOI or (20+ characters of)
                normalized title (default: leave them out, since importing them would create
                duplicates)
--move-orphans  move attachment folders this project copied earlier for records no longer in the
                list, and that the library does not reference, to <project>/orphan_pdf_backup/

Writes import_manifest.json beside the import file; verify_import.py checks the library
against it after the user has imported.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

from collection_common import (
    console_utf8,
    crossref_message,
    folder_number,
    get,
    list_records,
    load_project,
    norm_title,
    ok_states,
    pdf_path,
    rec_doi,
    rec_title,
    write_ris,
)
from library import data_dir, library_references, referenced_folders


def choose_folder(doi: str, name: str, pdf_root: Path, taken: set[str]) -> str:
    for attempt in range(50):
        num = folder_number(doi, attempt)
        folder = pdf_root / num
        if num in taken:
            continue
        if folder.is_dir() and any(p.name != name for p in folder.iterdir()):
            continue  # someone else's attachment lives here
        return num
    raise RuntimeError(f"no free attachment folder for {doi}")


def fix_journal(rec, message: dict) -> bool:
    title = (message.get("container-title") or [""])[0].strip()
    for pair in rec:
        if pair[0] == "T2" and pair[1].isupper() and title and not title.isupper():
            pair[1] = title
            return True
    return False


def orphans(project, pdf_root: Path, referenced: set[str], used: set[str]) -> list[dict]:
    """Folders holding exactly one of this project's PDFs, under its DOI's own number, that
    nothing references and this import does not use."""
    found = []
    for doi, entry in ok_states(project).items():
        for attempt in range(3):
            num = folder_number(doi, attempt)
            folder = pdf_root / num
            if num in referenced or num in used or not folder.is_dir():
                continue
            if [p.name for p in folder.iterdir()] == [entry.get("file")]:
                found.append({"folder": num, "doi": doi, "file": entry["file"]})
    return found


def main(argv=None) -> int:
    console_utf8()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("project")
    ap.add_argument("--library", required=True)
    ap.add_argument("--out", default="import.ris")
    ap.add_argument("--label")
    ap.add_argument("--journal-case", action="store_true")
    ap.add_argument("--keep-held", action="store_true")
    ap.add_argument("--move-orphans", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    project = load_project(args.project)
    label = project.label if args.label is None else args.label
    pdf_root = data_dir(args.library) / "PDF"
    refs = library_references(args.library)
    referenced = referenced_folders(refs)
    held = {r["doi"] for r in refs if r["doi"]}
    # Records without a DOI can only be recognised by title; very short titles are not evidence.
    held_titles = {t for t in (norm_title(r["title"]) for r in refs) if len(t) >= 20}
    states = ok_states(project)

    out, manifest, skipped = [], {}, []
    copied = present = journals = 0
    taken = set(referenced)
    for rec in list_records(project):
        rec = [list(pair) for pair in rec]
        doi = rec_doi(rec)
        if not args.keep_held and (
            (doi and doi in held) or norm_title(rec_title(rec)) in held_titles
        ):
            skipped.append(doi or rec_title(rec))
            continue
        extra = []
        src = pdf_path(project, doi, states[doi]) if doi in states else None
        if src:
            num = choose_folder(doi, src.name, pdf_root, taken)
            taken.add(num)
            dest = pdf_root / num / src.name
            if dest.is_file() and dest.stat().st_size == src.stat().st_size:
                present += 1
            else:
                copied += 1
                if not args.dry_run:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dest)
            extra.append(["L1", f"internal-pdf://{num}/{src.name}"])
            manifest[doi] = f"{num}/{src.name}"
        if label and not get(rec, "LB"):
            extra.append(["LB", label])
        if args.journal_case and doi:
            journals += fix_journal(rec, crossref_message(project, doi))
        er = next(i for i, pair in enumerate(rec) if pair[0] == "ER")
        out.append(rec[:er] + extra + rec[er:])

    stale = orphans(project, pdf_root, referenced, {v.split("/")[0] for v in manifest.values()})
    print(
        f"records {len(out)}, with PDF {len(manifest)} (copy {copied}, already there {present})"
        f", left out as already in the library {len(skipped)}"
        + (f", journal names fixed {journals}" if args.journal_case else "")
    )
    print(f"orphan folders from earlier copies: {len(stale)}")
    if args.dry_run:
        print("dry run: nothing written")
        return 0
    out_path = project.dir / args.out
    write_ris(out_path, out)
    (project.dir / "import_manifest.json").write_text(
        json.dumps(
            {
                "built": time.strftime("%Y-%m-%d %H:%M:%S"),
                "library": str(Path(args.library).resolve()),
                "ris": out_path.name,
                "records": len(out),
                "label": label,
                "left_out_held": skipped,
                "attached": manifest,
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    if stale and args.move_orphans:
        backup = project.dir / "orphan_pdf_backup"
        backup.mkdir(exist_ok=True)
        for o in stale:
            shutil.move(str(pdf_root / o["folder"]), str(backup / o["folder"]))
        (backup / "moved.json").write_text(
            json.dumps(stale, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        print(f"moved {len(stale)} orphan folders to {backup}")
    print(f"wrote {out_path} and import_manifest.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
