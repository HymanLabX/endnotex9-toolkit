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

import common  # side effect: puts the endnotex9_toolkit package on sys.path

from endnotex9_toolkit import commit, libraryio

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
        if field == "doi":
            # Carry over the bare DOI, never an `ARTN ...` line with it.
            keep_val, delete_val = common.doi_of(keep_row), common.doi_of(delete_row)
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
    ap.add_argument("--output-dir", help="override the per-library report directory")
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
    out_dir = common.report_dir(library_path, args.output_dir)
    if args.commit:
        path = out_dir / "dedup_plan.json"
        if not path.is_file():
            raise SystemExit("Generate and review a dedup report before --commit")
        plan = json.loads(path.read_text(encoding="utf-8"))
        if not plan["delete_recs"]:
            print("nothing to commit")
            return
        if (args.marker, args.column) != (plan["marker"], plan["column"]):
            raise SystemExit("Marker/column differ from the saved preview; generate a new report")
        merges = {int(k): v for k, v in plan["merge_updates"].items()}
        with libraryio.library_lock(library_path):
            checked = commit.mark_records(
                rdb,
                library_path,
                plan["delete_recs"],
                plan["marker"],
                column=plan["column"],
                expected_fingerprint=plan["source_fingerprint"],
            )
            if merges:
                result = commit.update_reference_fields(
                    rdb,
                    library_path,
                    merges,
                    dry_run=False,
                    expected_fingerprint=checked["source_fingerprint"],
                )
                print("merge committed: " + json.dumps(result, ensure_ascii=False))
            result = commit.mark_records(
                rdb,
                library_path,
                plan["delete_recs"],
                plan["marker"],
                column=plan["column"],
                dry_run=False,
                expected_fingerprint=libraryio.source_fingerprint(rdb, library_path),
            )
        print("mark committed: " + json.dumps(result, ensure_ascii=False))
        print(
            f"In EndNote: build a Smart Group '{args.column} contains {args.marker}', review, then delete."
        )
        return
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

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "dedup_resolved.json").write_text(
        json.dumps(resolved, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_dir / "dedup_needs_review.json").write_text(
        json.dumps(needs_review, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_dir / "dedup_merge_updates.json").write_text(
        json.dumps({str(k): v for k, v in merge_updates.items()}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    if not resolved:
        (out_dir / "dedup_plan.json").write_text(
            json.dumps(
                {
                    "delete_recs": [],
                    "merge_updates": {},
                    "marker": args.marker,
                    "column": args.column,
                }
            ),
            encoding="utf-8",
        )
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
    (out_dir / "dedup_plan.json").write_text(
        json.dumps(
            {
                "merge_updates": merge_updates,
                "delete_recs": delete_recs,
                "marker": args.marker,
                "column": args.column,
                "source_fingerprint": mark_preview["source_fingerprint"],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(
        f"mark dry_run: would_mark={mark_preview['would_mark']} already_marked={mark_preview['already_marked']}"
    )
    if mark_preview["conflicts"]:
        print(
            f"  conflicts (column already holds different text): {len(mark_preview['conflicts'])}"
        )

    print("\n(dry run only -- rerun with --commit to apply this saved preview)")
    print(f"once committed, build an EndNote Smart Group: {args.column} contains {args.marker}")


if __name__ == "__main__":
    main()
