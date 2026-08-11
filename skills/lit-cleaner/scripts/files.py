"""lit-cleaner files: orphaned PDF attachments + this tool's own old backups.

Pure filesystem operations -- never touches refs.MYD/MYI/.enl, so none of
writeback.py's guardrails apply here and none are needed: nothing here is a
MyISAM write. Still list-then-confirm, same as everything else in
lit-cleaner: orphaned PDFs are moved to a quarantine folder (reversible), not
deleted outright; old backups are only ever candidates beyond both a
recency count and an age threshold, and are actually deleted only once
listed and confirmed (backups are themselves insurance, so removing one is
not undone by another backup step).

Usage:
    python files.py                          # report only
    python files.py --commit                 # quarantine orphans, delete excess backups
    python files.py --keep-backups 5 --backup-age-days 14
"""

from __future__ import annotations

import argparse
import json
import shutil
import time
from pathlib import Path

import common  # side effect: puts the endnotex9_mcp package on sys.path

from endnotex9_mcp import refstable

OUT_DIR = Path(__file__).resolve().parent / "out"

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
            referenced.add(Path(rel).as_posix())

    on_disk = []
    if pdf_dir.is_dir():
        for path in pdf_dir.rglob("*"):
            parents = path.relative_to(pdf_dir).parts[:-1]
            if path.is_file() and not any(d in parents for d in QUARANTINE_DIRNAMES):
                on_disk.append(path)

    orphans = []
    for path in on_disk:
        rel = path.relative_to(pdf_dir).as_posix()
        if rel not in referenced:
            orphans.append(path)
    return orphans, len(on_disk), len(referenced)


def find_excess_backups(library_path, keep_recent, age_days):
    backups_dir = Path(library_path).resolve().parent / "backups"
    if not backups_dir.is_dir():
        return [], []
    entries = sorted(
        (p for p in backups_dir.iterdir() if p.is_dir()), key=lambda p: p.name, reverse=True
    )
    cutoff = time.time() - age_days * 86400
    recent, excess = entries[:keep_recent], entries[keep_recent:]
    prunable = [p for p in excess if p.stat().st_mtime < cutoff]
    kept = recent + [p for p in excess if p not in prunable]
    return prunable, kept


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
    ap.add_argument("--keep-backups", type=int, default=5)
    ap.add_argument("--backup-age-days", type=int, default=14)
    args = ap.parse_args()

    rdb, library_path, pdf_dir = common.load_library()
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

    OUT_DIR.mkdir(exist_ok=True)
    (OUT_DIR / "files_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    if not orphans and not prunable:
        print("\nnothing to clean up.")
        return

    if not args.commit:
        print(
            "\n(report only -- rerun with --commit to quarantine orphans and delete excess backups)"
        )
        return

    if orphans:
        quarantine = Path(pdf_dir) / QUARANTINE_DIRNAME
        moved, skipped = quarantine_orphans(orphans, pdf_dir, quarantine)
        print(f"\nmoved {moved} orphaned PDF(s) to {quarantine}")
        for path in skipped:
            print(f"  left in place (a file already sits at its quarantine path): {path}")

    for p in prunable:
        shutil.rmtree(p)
    if prunable:
        print(f"deleted {len(prunable)} old backup folder(s)")


if __name__ == "__main__":
    main()
