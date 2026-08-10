"""ref-clean metadata: fill in / correct text fields, author list, keyword list.

Two-source waterfall per field, same as the proven session run:
  author / journal  -> same-record source first (Notes / alternate_title),
                        CrossRef by DOI second, each gated by its own check.
  volume/issue/pages/article_number/number_of_volumes/abstract/title
                     -> local blank -> CrossRef fill only (no local source).
  keywords           -> strip the "X-MOL" import-noise token.
  notes               -> cleared only once author or journal was actually
                        recovered from it.

Always dry-run first (writes ref_clean_metadata_preview.json, nothing to
EndNote). Only with --commit does it call commit.update_reference_fields
with dry_run=False, which backs up the library and refuses to run while
EndNote has it open.

Usage:
    python metadata.py                 # census + build + dry-run, report only
    python metadata.py --commit        # do the above, then actually write
    python metadata.py --limit 50      # cap how many records are examined (debugging)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "src"))

import common
import crossref

from endnotex9_mcp import commit

OUT_DIR = Path(__file__).resolve().parent / "out"


def build_updates(rows, crossref_data):
    updates = {}
    stats = {
        "keywords_xmol": 0,
        "author_from_notes": 0,
        "author_from_crossref": 0,
        "author_skipped_count_mismatch": 0,
        "author_skipped_surname_mismatch": 0,
        "journal_from_alt_title": 0,
        "journal_from_crossref": 0,
        "notes_cleared": 0,
        "volume_filled": 0,
        "issue_filled": 0,
        "pages_filled": 0,
        "article_number_filled": 0,
        "abstract_filled": 0,
        "title_filled": 0,
    }
    review = {"author_count_mismatch": [], "author_surname_mismatch": []}

    for rec, row in rows.items():
        fields = {}
        authors = common.values(row, "author")
        keywords = common.values(row, "keywords")
        journal = common.text(row, "secondary_title")
        alt_title = common.text(row, "alternate_title")
        note = common.text(row, "notes")
        doi = common.doi_of(row)
        message = crossref_data.get(doi) if doi else None
        message = message if isinstance(message, dict) else None

        if any(k.lower() == "x-mol" for k in keywords):
            fields["keywords"] = [k for k in keywords if k.lower() != "x-mol"]
            stats["keywords_xmol"] += 1

        note_fixed_something = False
        has_abbrev = any(common.is_abbrev_author(a) for a in authors)
        if has_abbrev and common.notes_looks_like_pubmed_dump(note):
            lines = [ln.strip() for ln in note.split(common.VALUE_SEPARATOR)]
            eng_idx = lines.index("eng")
            candidates = [ln for ln in lines[:eng_idx] if ln.strip()]
            if len(candidates) == len(authors):
                fields["author"] = candidates
                stats["author_from_notes"] += 1
                note_fixed_something = True
                has_abbrev = False

        if has_abbrev and message:
            cr_names = crossref.authors_of(message)
            if len(cr_names) == len(authors):
                mismatches = [
                    i
                    for i, (fam, _giv) in enumerate(cr_names)
                    if common.norm_name(fam) != common.surname_of(authors[i])
                    and common.surname_of(authors[i]) not in common.norm_name(fam)
                    and common.norm_name(fam) not in common.surname_of(authors[i])
                ]
                if not mismatches:
                    fields["author"] = [f"{fam}, {giv}" if giv else fam for fam, giv in cr_names]
                    stats["author_from_crossref"] += 1
                else:
                    stats["author_skipped_surname_mismatch"] += 1
                    review["author_surname_mismatch"].append(
                        {"rec": rec, "doi": doi, "current": authors, "crossref": cr_names}
                    )
            else:
                stats["author_skipped_count_mismatch"] += 1
                review["author_count_mismatch"].append(
                    {"rec": rec, "doi": doi, "current_count": len(authors), "crossref_count": len(cr_names)}
                )

        journal_abbrev_or_blank = not journal or common.looks_abbrev_journal(journal)
        if journal_abbrev_or_blank and alt_title and alt_title != journal and len(alt_title) > len(journal):
            fields["journal"] = alt_title
            stats["journal_from_alt_title"] += 1
            # journal came from alternate_title here, not notes -- do not
            # set note_fixed_something, or an unrelated, legitimate notes
            # value would get wiped by the `if note_fixed_something` block
            # below.
        elif journal_abbrev_or_blank and message:
            cr_journal = crossref.container_title_of(message)
            if cr_journal and cr_journal != journal and len(cr_journal) >= len(journal):
                fields["journal"] = cr_journal
                stats["journal_from_crossref"] += 1

        if note_fixed_something:
            fields["notes"] = ""
            stats["notes_cleared"] += 1

        if message:
            if not common.text(row, "volume"):
                v = (message.get("volume") or "").strip()
                if v:
                    fields["volume"] = v
                    stats["volume_filled"] += 1
            if not common.text(row, "number"):
                v = (message.get("issue") or "").strip()
                if v:
                    fields["issue"] = v
                    stats["issue_filled"] += 1
            if not common.text(row, "pages"):
                v = (message.get("page") or "").strip()
                if v:
                    fields["pages"] = v
                    stats["pages_filled"] += 1
            if not common.text(row, "section"):
                v = (message.get("article-number") or "").strip()
                if v:
                    fields["article_number"] = v
                    stats["article_number_filled"] += 1
            if not common.text(row, "abstract"):
                v = crossref.abstract_of(message)
                if v:
                    fields["abstract"] = v
                    stats["abstract_filled"] += 1
            if not common.text(row, "title"):
                v = crossref.title_of(message)
                if v:
                    fields["title"] = v
                    stats["title_filled"] += 1

        if fields:
            updates[rec] = fields

    return updates, stats, review


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    rdb, library_path, _pdf_dir = common.load_library()
    rows = common.read_all_rows(rdb)
    if args.limit:
        rows = dict(list(rows.items())[: args.limit])

    needs_crossref = set()
    for row in rows.values():
        doi = common.doi_of(row)
        if not doi:
            continue
        authors = common.values(row, "author")
        journal = common.text(row, "secondary_title")
        note = common.text(row, "notes")
        note_covers_author = any(common.is_abbrev_author(a) for a in authors) and common.notes_looks_like_pubmed_dump(note)
        needs_author = any(common.is_abbrev_author(a) for a in authors) and not note_covers_author
        needs_journal = not journal or common.looks_abbrev_journal(journal)
        needs_fill = any(
            not common.text(row, c) for c in ("volume", "number", "pages", "section", "abstract", "title")
        )
        if needs_author or needs_journal or needs_fill:
            needs_crossref.add(doi)

    print(f"records examined: {len(rows)}")
    print(f"unique DOIs to query CrossRef for: {len(needs_crossref)}")

    def progress(done, total):
        if done % 50 == 0 or done == total:
            print(f"  crossref: {done}/{total}")

    crossref_data = crossref.fetch_many(needs_crossref, on_progress=progress) if needs_crossref else {}

    updates, stats, review = build_updates(rows, crossref_data)

    print("\n--- fix counts (records) ---")
    for k, v in stats.items():
        print(f"  {k}: {v}")
    print(f"\ntotal records with at least one fix: {len(updates)}")

    OUT_DIR.mkdir(exist_ok=True)
    (OUT_DIR / "metadata_updates.json").write_text(
        json.dumps({str(k): v for k, v in updates.items()}, ensure_ascii=False), encoding="utf-8"
    )
    (OUT_DIR / "metadata_review.json").write_text(json.dumps(review, ensure_ascii=False, indent=2), encoding="utf-8")

    if not updates:
        print("\nnothing to do.")
        return

    preview = commit.update_reference_fields(rdb, library_path, updates, dry_run=True)
    print(f"\ndry_run: would_write={preview['would_write']} unchanged={preview['unchanged']}")
    (OUT_DIR / "metadata_dry_run.json").write_text(json.dumps(preview, ensure_ascii=False, indent=2), encoding="utf-8")

    review_count = len(review["author_count_mismatch"]) + len(review["author_surname_mismatch"])
    if review_count:
        print(f"\n{review_count} record(s) need manual review -- see out/metadata_review.json")

    if not args.commit:
        print("\n(dry run only -- rerun with --commit to actually write)")
        return

    result = commit.update_reference_fields(rdb, library_path, updates, dry_run=False)
    print(f"\ncommitted: records_written={result['records_written']} backup_path={result['backup_path']}")
    (OUT_DIR / "metadata_commit_result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
