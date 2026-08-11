"""lit-cleaner dedup: detect duplicates, merge unique fields into the survivor,
tag the loser for the user to delete in EndNote.

This never deletes a record. It:
  1. Groups by DOI (confirmed) and, separately, by normalized title + year
     + first-author surname for records with no DOI (likely -- lower
     confidence, always left for manual review, never auto-merged).
  2. Within a confirmed 2-member DOI group, if has_pdf differs, the no-PDF
     side is the delete candidate and the PDF side is kept. Any other shape
     (3+ members, both/neither has a PDF) is left for manual review instead
     of guessed at.
  3. For confirmed pairs, merges: any TEXT_FIELDS value the delete side has
     and the keep side lacks is copied onto the keep side (real field-level
     merge, via commit.update_reference_fields), and any LIST_FIELDS values
     unique to the delete side are appended onto the keep side's list.
  4. Tags the delete side via commit.mark_records (default column custom_7,
     default marker LIT_CLEANER_DUPLICATE) so the user can build an EndNote
     Smart Group ("Custom 7 contains LIT_CLEANER_DUPLICATE") and delete the
     matches themselves.

Usage:
    python dedup.py                # detect + build merge plan, dry-run, report
    python dedup.py --commit       # do the above, then merge + tag for real
    python dedup.py --marker "MY_MARKER" --column custom_6
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

import common  # side effect: puts the endnotex9_mcp package on sys.path

from endnotex9_mcp import commit

OUT_DIR = Path(__file__).resolve().parent / "out"


norm_title = common.norm_title


def detect_groups(rows):
    by_doi = defaultdict(list)
    no_doi = []
    for rec, row in rows.items():
        doi = common.doi_of(row)
        if doi:
            by_doi[doi].append(rec)
        else:
            no_doi.append(rec)

    confirmed = [recs for recs in by_doi.values() if len(recs) > 1]

    by_fuzzy = defaultdict(list)
    for rec in no_doi:
        row = rows[rec]
        title = norm_title(common.text(row, "title"))
        year = common.text(row, "year")
        authors = common.values(row, "author")
        first_author = common.surname_of(authors[0]) if authors else ""
        if title and year:
            by_fuzzy[(title, year, first_author)].append(rec)
    likely = [recs for recs in by_fuzzy.values() if len(recs) > 1]

    return confirmed, likely


def classify(rows, group):
    has_pdf = {rec: common.has_pdf(rows[rec]) for rec in group}
    pdf_recs = [r for r, v in has_pdf.items() if v]
    no_pdf_recs = [r for r, v in has_pdf.items() if not v]
    if len(group) == 2 and len(pdf_recs) == 1 and len(no_pdf_recs) == 1:
        return {"keep": pdf_recs[0], "delete": no_pdf_recs[0]}
    return None


# "notes" is deliberately excluded: it is where the PubMed/WoS import scratch
# junk this whole tool exists to clean up lives (citation-tracking IDs,
# "Times Cited" counters), not a field where "the other copy has something
# and we don't" means the other copy's value is worth keeping.
MERGE_TEXT_FIELDS = {k: v for k, v in commit.TEXT_FIELDS.items() if k != "notes"}


def build_merge(rows, keep_rec, delete_rec):
    keep_row, delete_row = rows[keep_rec], rows[delete_rec]
    fields = {}
    for field, column in MERGE_TEXT_FIELDS.items():
        keep_val = common.text(keep_row, column)
        delete_val = common.text(delete_row, column)
        if not keep_val and delete_val:
            fields[field] = delete_val
    for field, column in commit.LIST_FIELDS.items():
        keep_vals = common.values(keep_row, column)
        delete_vals = common.values(delete_row, column)
        extra = [v for v in delete_vals if v not in keep_vals and v.lower() != "x-mol"]
        if extra:
            fields[field] = keep_vals + extra
    return fields


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    # Records tagged before the lit-cleaner rename carry LIT_CLEAN_DUPLICATE,
    # which this string does not contain as a substring -- a Smart Group built
    # on the old marker matches nothing tagged from here on, and the user is
    # told their duplicates are flagged either way. `LIT_CLEAN` is a prefix of
    # both and is the condition to widen such a group to; `--marker` is the
    # other way out. SKILL.md's dedup step says so where the user can see it.
    ap.add_argument("--marker", default="LIT_CLEANER_DUPLICATE")
    ap.add_argument("--column", default="custom_7")
    args = ap.parse_args()

    rdb, library_path, _pdf_dir = common.load_library()
    rows = common.read_all_rows(rdb)

    confirmed, likely = detect_groups(rows)

    resolved = []
    needs_review = []
    for group in confirmed:
        decision = classify(rows, group)
        if decision:
            resolved.append(decision)
        else:
            needs_review.append(
                {"group": group, "reason": "3+ members or has_pdf tied", "doi_confirmed": True}
            )
    for group in likely:
        needs_review.append(
            {
                "group": group,
                "reason": "no DOI, title/year/author match only",
                "doi_confirmed": False,
            }
        )

    print(f"records examined: {len(rows)}")
    print(f"confirmed DOI-duplicate groups: {len(confirmed)}")
    print(f"  auto-resolved (safe pattern): {len(resolved)}")
    print(f"likely (no-DOI) duplicate groups: {len(likely)}")
    print(f"needs manual review: {len(needs_review)}")

    merge_updates = {}
    for decision in resolved:
        fields = build_merge(rows, decision["keep"], decision["delete"])
        if fields:
            merge_updates[decision["keep"]] = fields
    print(f"\nkeep-side records that gain merged fields: {len(merge_updates)}")

    OUT_DIR.mkdir(exist_ok=True)
    (OUT_DIR / "dedup_resolved.json").write_text(
        json.dumps(resolved, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (OUT_DIR / "dedup_needs_review.json").write_text(
        json.dumps(needs_review, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (OUT_DIR / "dedup_merge_updates.json").write_text(
        json.dumps({str(k): v for k, v in merge_updates.items()}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    if not resolved:
        print("\nnothing to merge or tag.")
        return

    delete_recs = [d["delete"] for d in resolved]

    if merge_updates:
        merge_preview = commit.update_reference_fields(
            rdb, library_path, merge_updates, dry_run=True
        )
        print(
            f"\nmerge dry_run: would_write={merge_preview['would_write']} unchanged={merge_preview['unchanged']}"
        )

    mark_preview = commit.mark_records(
        rdb, library_path, delete_recs, args.marker, column=args.column, dry_run=True
    )
    print(
        f"mark dry_run: would_mark={mark_preview['would_mark']} already_marked={mark_preview['already_marked']}"
    )
    if mark_preview["conflicts"]:
        print(
            f"  conflicts (column already holds different text): {len(mark_preview['conflicts'])}"
        )

    if not args.commit:
        print("\n(dry run only -- rerun with --commit to actually write)")
        print(f"once committed, build an EndNote Smart Group: {args.column} contains {args.marker}")
        return

    if merge_updates:
        result = commit.update_reference_fields(rdb, library_path, merge_updates, dry_run=False)
        print(
            f"\nmerge committed: records_written={result['records_written']} backup_path={result['backup_path']}"
        )

    result = commit.mark_records(
        rdb, library_path, delete_recs, args.marker, column=args.column, dry_run=False
    )
    print(
        f"mark committed: records_written={result['records_written']} backup_path={result['backup_path']}"
    )
    print(
        f"\nnow in EndNote: build a Smart Group '{args.column} contains {args.marker}', "
        "review it, then delete the matches yourself."
    )


if __name__ == "__main__":
    main()
