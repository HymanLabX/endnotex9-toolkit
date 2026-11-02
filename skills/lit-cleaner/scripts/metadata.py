"""lit-cleaner metadata: fill in / correct text fields, author list, keyword list.

Two-source waterfall per field, same as the proven session run:
  author / journal  -> same-record source first (Notes / alternate_title),
                        CrossRef by DOI second, each gated by its own check.
                        Authors are reconsidered for two independent
                        reasons: initials CrossRef can spell out, and a
                        list that is merely *short* -- an `et al.` import
                        that kept the first author or three. The second is
                        only acted on when the stored names are a strict
                        positional surname prefix of CrossRef's, and never
                        shortens a list CrossRef knows less about.
  volume/issue/pages/article_number/number_of_volumes/abstract/title
                     -> local blank -> CrossRef fill only (no local source).
  keywords           -> strip the "X-MOL" import-noise token.
  doi                -> rewrite to the bare DOI when the field carries more
                        (a Web of Science `ARTN <n>` line, a doi.org URL);
                        the ARTN value moves to a blank article number.
  notes               -> cleared only once author or journal was actually
                        recovered from it.

Always dry-run first: the plan lands in out/metadata_updates.json and the
diff in the printed report_dir/metadata_dry_run.json, with nothing written to EndNote. Only
with --commit does it call commit.update_reference_fields with
dry_run=False, which backs up the library and refuses to run while EndNote
has it open.

CrossRef answers are cached in out/crossref_cache.json, so `--commit`
writes exactly what the preceding dry run displayed rather than sweeping
the API a second time and acting on whatever comes back. Use --refresh to
discard the cache and re-query.

Usage:
    python metadata.py                 # census + build + dry-run, report only
    python metadata.py --commit        # do the above, then actually write
    python metadata.py --refresh       # ignore the CrossRef cache
    python metadata.py --limit 50      # cap how many records are examined (debugging)
"""

from __future__ import annotations

import argparse
import json

import common  # side effect: puts the endnotex9_toolkit package on sys.path
import crossref

from endnotex9_toolkit import commit, libraryio


def build_updates(rows, crossref_data):
    updates = {}
    stats = {
        "keywords_xmol": 0,
        "doi_cleaned": 0,
        "article_number_from_artn": 0,
        "author_from_notes": 0,
        "author_from_crossref": 0,
        "author_completed_from_crossref": 0,
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

        raw_doi = common.text(row, "electronic_resource_number")
        artn = common.article_number_of(raw_doi)
        # Only when what is left is a DOI, or an ARTN line is what was
        # stripped: normalize_doi keeps just the first line of anything else,
        # and dropping the rest of an unrecognised field is not a cleanup.
        if raw_doi != doi and (doi.startswith("10.") or artn):
            fields["doi"] = doi
            stats["doi_cleaned"] += 1
        if artn and not common.text(row, "section"):
            fields["article_number"] = artn
            stats["article_number_from_artn"] += 1

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

        # Two separate things can be wrong with a stored author list, and
        # gating on only the first (as this used to) misses the other
        # entirely: the names can be initials that CrossRef can spell out,
        # and the list can be *short* -- the `et al.` import problem, where
        # a record keeps the first author or three and drops the rest. A
        # record whose two authors are already spelled out in full is
        # invisible to an abbreviation check, however many are missing.
        cr_names = crossref.authors_of(message) if message else []
        truncated = cr_names and common.author_list_is_truncated(authors, cr_names)
        if cr_names and "author" not in fields and (has_abbrev or truncated):
            full = [f"{fam}, {giv}" if giv else fam for fam, giv in cr_names]
            if truncated:
                # Same people in the same order, just fewer of them.
                fields["author"] = full
                stats["author_completed_from_crossref"] += 1
            elif len(cr_names) == len(authors):
                mismatches = [
                    i
                    for i, (fam, _giv) in enumerate(cr_names)
                    if not common.same_surname(authors[i], fam)
                ]
                if not mismatches:
                    fields["author"] = full
                    stats["author_from_crossref"] += 1
                else:
                    stats["author_skipped_surname_mismatch"] += 1
                    review["author_surname_mismatch"].append(
                        {"rec": rec, "doi": doi, "current": authors, "crossref": cr_names}
                    )
            else:
                # Counts differ and the overlap does not line up — either
                # the record has authors CrossRef lacks, or they disagree
                # about who wrote this. Not something to guess at.
                stats["author_skipped_count_mismatch"] += 1
                review["author_count_mismatch"].append(
                    {
                        "rec": rec,
                        "doi": doi,
                        "current": authors,
                        "crossref": [f"{f}, {g}" for f, g in cr_names],
                    }
                )

        journal_abbrev_or_blank = not journal or common.looks_abbrev_journal(journal)
        if (
            journal_abbrev_or_blank
            and alt_title
            and alt_title != journal
            and len(alt_title) > len(journal)
        ):
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
            if not common.text(row, "section") and "article_number" not in fields:
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
    ap.add_argument("--output-dir", help="override the per-library report directory")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument(
        "--refresh", action="store_true", help="ignore the per-library CrossRef cache and re-query"
    )
    args = ap.parse_args()

    rdb, library_path, _pdf_dir = common.load_library()
    out_dir = common.report_dir(library_path, args.output_dir)
    if args.commit:
        if args.refresh or args.limit:
            raise SystemExit(
                "--commit applies the saved preview; generate a new report to change its scope"
            )
        plan = out_dir / "metadata_dry_run.json"
        if not plan.is_file():
            raise SystemExit("Generate and review a metadata report before --commit")
        preview = json.loads(plan.read_text(encoding="utf-8"))
        if not preview["diffs"]:
            print("nothing to commit")
            return
        updates = {}
        for diff in preview["diffs"]:
            updates.setdefault(int(diff["rec_number"]), {})[diff["field"]] = diff["new_value"]
        result = commit.update_reference_fields(
            rdb,
            library_path,
            updates,
            dry_run=False,
            expected_fingerprint=preview["source_fingerprint"],
        )
        (out_dir / "metadata_commit_result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(result, ensure_ascii=False))
        return
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
        note_covers_author = any(
            common.is_abbrev_author(a) for a in authors
        ) and common.notes_looks_like_pubmed_dump(note)
        # Whether the list is truncated cannot be known without asking
        # CrossRef how many authors there are, so any record with a DOI is
        # worth a lookup on author grounds alone. Gating this on
        # `is_abbrev_author` (as it used to) hid every record whose too-few
        # authors happened to be spelled out in full.
        needs_author = bool(authors) and not note_covers_author
        needs_journal = not journal or common.looks_abbrev_journal(journal)
        needs_fill = any(
            not common.text(row, c)
            for c in ("volume", "number", "pages", "section", "abstract", "title")
        )
        if needs_author or needs_journal or needs_fill:
            needs_crossref.add(doi)

    print(f"records examined: {len(rows)}")
    print(f"unique DOIs to query CrossRef for: {len(needs_crossref)}")

    def progress(done, total):
        if done % 50 == 0 or done == total:
            print(f"  crossref: {done}/{total}")

    out_dir.mkdir(parents=True, exist_ok=True)
    crossref_data = (
        crossref.fetch_many(
            needs_crossref,
            on_progress=progress,
            cache_path=out_dir / "crossref_cache.json",
            refresh=args.refresh,
        )
        if needs_crossref
        else {}
    )
    errors = sum(1 for v in crossref_data.values() if v == "ERROR")
    if errors:
        print(f"  {errors} DOI lookup(s) failed and were skipped (not cached; rerun to retry)")

    updates, stats, review = build_updates(rows, crossref_data)

    print("\n--- fix counts (records) ---")
    for k, v in stats.items():
        print(f"  {k}: {v}")
    print(f"\ntotal records with at least one fix: {len(updates)}")

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "metadata_updates.json").write_text(
        json.dumps({str(k): v for k, v in updates.items()}, ensure_ascii=False), encoding="utf-8"
    )
    (out_dir / "metadata_review.json").write_text(
        json.dumps(review, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    if not updates:
        (out_dir / "metadata_dry_run.json").write_text(
            json.dumps(
                {"diffs": [], "source_fingerprint": libraryio.source_fingerprint(rdb, library_path)}
            ),
            encoding="utf-8",
        )
        print("\nnothing to do.")
        return

    preview = commit.update_reference_fields(rdb, library_path, updates, dry_run=True)
    print(f"\ndry_run: would_write={preview['would_write']} unchanged={preview['unchanged']}")
    (out_dir / "metadata_dry_run.json").write_text(
        json.dumps(preview, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    review_count = len(review["author_count_mismatch"]) + len(review["author_surname_mismatch"])
    if review_count:
        print(
            f"\n{review_count} record(s) need manual review -- see metadata_review.json in report_dir"
        )

    print("\n(dry run only -- rerun with --commit to apply this saved preview)")


if __name__ == "__main__":
    main()
