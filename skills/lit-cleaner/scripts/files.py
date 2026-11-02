"""Preview orphan PDFs and owned metadata backups; commit the saved candidate list.

Close EndNote before applying a report. The library lock and source fingerprint
protect metadata-dependent decisions. Orphans are quarantined; only completed,
library-owned backups beyond both retention thresholds are permanently removed.
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

import common  # side effect: puts the endnotex9_toolkit package on sys.path

from endnotex9_toolkit import backups, libraryio, pdf, refstable, writeback

# Where a --commit run quarantines orphans (see main()).
QUARANTINE_DIRNAME = "_lit_cleaner_orphans"

# Every quarantine folder to skip while scanning. Without this a second run
# would find its own previously quarantined files unreferenced all over
# again, re-add them to `orphans`, and try to shutil.move() them onto
# themselves. The second entry is the name this skill used before it was
# renamed from lit-clean: a library quarantined under the old name still has
# files sitting there awaiting the user's review, and re-flagging them would
# move them a second time, into the new folder, unannounced.
#
# `_ref_clean_orphans` from the ref-clean rename before that is deliberately
# absent -- it was dropped once no library on this machine held one. This
# name is the opposite case: it was in use right up to the rename, so the
# folders it guards are the ones likeliest to actually exist.
QUARANTINE_DIRNAMES = (QUARANTINE_DIRNAME, "_lit_clean_orphans")


def find_orphaned_pdfs(rows, pdf_dir):
    pdf_dir = Path(pdf_dir)
    referenced = set()
    for row in rows.values():
        for rel in refstable._pdf_files(row):
            resolved = pdf.resolve_pdf_path(pdf_dir, rel)
            if resolved is not None:
                referenced.add(resolved)

    on_disk = []
    if pdf_dir.is_dir():
        for path in pdf_dir.rglob("*"):
            parents = path.relative_to(pdf_dir).parts[:-1]
            if (
                path.is_file()
                and path.suffix.casefold() == ".pdf"
                and path.resolve().is_relative_to(pdf_dir.resolve())
                and not any(d in parents for d in QUARANTINE_DIRNAMES)
            ):
                on_disk.append(path)

    orphans = []
    for path in on_disk:
        if path.resolve() not in referenced:
            orphans.append(path)
    return orphans, len(on_disk), len(referenced)


def find_excess_backups(library_path, keep_recent, age_days):
    return backups.excess_backups(library_path, keep_recent, age_days)


def dir_size(path):
    return sum(f.stat().st_size for f in Path(path).rglob("*") if f.is_file())


def quarantine_orphans(orphans, pdf_dir, quarantine):
    """Move each orphan under `quarantine`, keeping its path below `pdf_dir`.

    Returns `(moved_count, skipped_paths)`.

    The relative path is preserved rather than flattened to the bare
    filename, which is not cosmetic. EndNote gives every attachment its own
    numeric folder (`PDF/2923055561/tahara2009.pdf`), so distinct orphans
    routinely share a basename, and `shutil.move` onto an existing file
    falls back to `copy2` — it overwrites. Flattening therefore destroyed
    one of every same-named pair, silently, in the step this skill
    describes to the user as reversible. Duplicate records are the main
    source of orphans and the likeliest to hold identically named PDFs, so
    the collision was most likely in exactly this tool's own dedup flow.

    Anything whose destination is already occupied is left where it is and
    reported; overwriting is never the right recovery here.
    """
    pdf_dir = Path(pdf_dir)
    moved, skipped = 0, []
    for path in orphans:
        dest = Path(quarantine) / path.relative_to(pdf_dir)
        if dest.exists():
            skipped.append(path)
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(dest))
        moved += 1
    return moved, skipped


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--output-dir", help="override the per-library report directory")
    ap.add_argument("--keep-backups", type=int, default=5)
    ap.add_argument("--backup-age-days", type=int, default=14)
    args = ap.parse_args()

    rdb, library_path, pdf_dir = common.load_library()
    out_dir = common.report_dir(library_path, args.output_dir)
    report_path = out_dir / "files_report.json"
    if args.commit:
        if not report_path.is_file():
            raise SystemExit("Generate and review a files report before --commit")
        apply_report(
            json.loads(report_path.read_text(encoding="utf-8")), rdb, library_path, pdf_dir
        )
        return
    fingerprint = libraryio.source_fingerprint(rdb, library_path)
    rows = common.read_all_rows(rdb)

    report = {}

    if pdf_dir:
        orphans, total_files, total_referenced = find_orphaned_pdfs(rows, pdf_dir)
        print(f"PDF folder: {total_files} files on disk, {total_referenced} referenced by records")
        print(f"orphaned (unreferenced) PDFs: {len(orphans)}")
        report["orphans"] = [str(p) for p in orphans]
        report["orphans_total_bytes"] = sum(p.stat().st_size for p in orphans)
    else:
        print("no pdf_dir configured -- skipping orphaned-PDF check")
        orphans = []

    prunable, kept = find_excess_backups(library_path, args.keep_backups, args.backup_age_days)
    print(
        f"\nbackups: keeping {len(kept)} (most recent {args.keep_backups}, or newer than "
        f"{args.backup_age_days} days), {len(prunable)} prunable"
    )
    report["prunable_backups"] = [str(p) for p in prunable]
    report["prunable_backups_total_bytes"] = sum(dir_size(p) for p in prunable)
    if libraryio.source_fingerprint(rdb, library_path) != fingerprint:
        raise RuntimeError("The library changed while scanning; generate a fresh files report")
    report["source_fingerprint"] = fingerprint
    report["retention"] = {"keep_recent": args.keep_backups, "age_days": args.backup_age_days}

    out_dir.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    if not orphans and not prunable:
        print("\nnothing to clean up.")
        return

    print("\n(report only -- close EndNote, then --commit to apply this saved report)")


def apply_report(report, rdb, library_path, pdf_dir):
    with libraryio.library_lock(library_path):
        libraryio.require_consistent(library_path)
        writeback._ensure_unlocked(
            [Path(library_path), Path(rdb) / "refs.MYD", Path(rdb) / "refs.MYI"]
        )
        if libraryio.source_fingerprint(rdb, library_path) != report["source_fingerprint"]:
            raise RuntimeError(
                "The library changed since the files report; generate a fresh report"
            )
        rows = common.read_all_rows(rdb)
        current_orphans = set(find_orphaned_pdfs(rows, pdf_dir)[0]) if pdf_dir else set()
        orphans = [Path(p) for p in report.get("orphans", [])]
        current_backups, _ = backups.excess_backups(library_path, **report["retention"])
        prunable = [Path(p) for p in report["prunable_backups"]]
        if not set(orphans).issubset(current_orphans) or not set(prunable).issubset(
            current_backups
        ):
            raise RuntimeError("Cleanup candidates changed since review; generate a fresh report")
        if orphans:
            quarantine = Path(pdf_dir) / QUARANTINE_DIRNAME
            moved, skipped = quarantine_orphans(orphans, pdf_dir, quarantine)
            print(f"moved {moved} orphaned PDF(s) to {quarantine}; left in place: {len(skipped)}")
        for path in prunable:
            shutil.rmtree(path)
        print(f"deleted {len(prunable)} old backup folder(s)")


if __name__ == "__main__":
    main()
