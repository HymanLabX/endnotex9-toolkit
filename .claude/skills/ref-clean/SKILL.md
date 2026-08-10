---
name: ref-clean
description: Clean up an EndNote X9 library through endnotex9-mcp -- fill in or correct metadata (volume/issue/pages/journal/author/keywords/notes/title/abstract), detect and merge duplicate references (tagging the loser for manual deletion in EndNote), and clean up orphaned PDF attachments plus this tool's own old backups. Use when the user invokes /ref-clean, or asks to clean, dedupe, or fix up their EndNote library's metadata.
---

# ref-clean

Three capabilities over the library configured for this project's `endnotex9-mcp` server
(`~/.config/endnotex9-mcp/config.yaml` or the platform equivalent — the scripts below load it the
same way the MCP server does, so there is nothing to configure separately):

1. **metadata** — fill in or correct volume/issue/pages/article number/number of volumes/journal/
   author list/keyword list/notes/title/abstract. `year` is permanently out of scope: it is a
   MyISAM index key and this project has no B-tree write capability (see `writeback.py`'s module
   docstring and the 2026-08 spike recorded in `SECURITY.md` — real, but stopped short of a
   working insert/delete/rebalance implementation).
2. **dedup** — detect duplicate references (DOI match, confirmed; title/year/author match with no
   DOI, likely and always left for manual review), merge each duplicate's unique field values into
   the copy being kept, then tag the copy being dropped with a marker. **This never deletes a
   record.** The user finishes the job in EndNote: build a Smart Group on the marker, review it,
   delete the matches themselves.
3. **files** — orphaned PDF attachments (files in the PDF folder no record references) and this
   tool's own accumulated `backups/` folders. Pure filesystem operations, no MyISAM writes at all.

## Before anything else

Read `scripts/common.py`, `scripts/metadata.py`, `scripts/dedup.py`, and `scripts/files.py` if you
have not already this session — they are short and the logic below assumes you know what each
script's flags and output files mean. Do not reimplement their logic inline; call them.

**Every write path — `commit.update_reference_fields`, `commit.mark_records`, and by extension
every `--commit` run of `metadata.py`/`dedup.py` — refuses to run while EndNote has the library
open** (`writeback._ensure_unlocked`), and always backs up the whole library first. That check is
a safety net, not a substitute for asking: before any `--commit` invocation, tell the user to
close EndNote and get their acknowledgement, the same way earlier sessions on this project did.

## Reading the arguments

Parse the skill's `args` loosely, not as a strict CLI:

- **No args, or something like "status"/"总结"/"看看情况"**: run all three scripts in
  report-only mode (no `--commit`), then summarize what each would do — counts, not the full
  per-record JSON — and ask the user which of the three (if any) they want to act on. Do not
  commit anything on a bare invocation.
- **Contains "metadata"/"元数据"/"补全"**: run the metadata flow.
- **Contains "dedup"/"去重"/"合并"/"重复"**: run the dedup flow.
- **Contains "files"/"文件"/"孤儿"/"回收站"/"备份"**: run the files flow.
- **Contains "all"/"全部"/"都"**: run all three flows in sequence (metadata, then dedup — dedup's
  merge step benefits from metadata already being filled in — then files).
- **Contains "--yes"/"-y"/"信任模式"/"trust mode"**: trust mode is on for this invocation. Skip
  the interactive preview-then-confirm round trip for every field, including `title` — there is no
  tiering (this was a deliberate choice; see `commit.py`'s module docstring for why `title` needed
  its own verification before it was even added to the allowlist, and note that trust mode does
  not change that it's already verified, only that you won't pause to ask about it specifically).
  Go straight to `--commit` after showing the summary, without waiting for a reply.
- Otherwise: ask the user which of the three they mean rather than guessing.

## Metadata flow

1. `python scripts/metadata.py` (no `--commit`). This can take a few minutes on a first run — it
   queries CrossRef once per unique DOI that needs it, at 8-way concurrency; the script prints
   progress every 50 lookups. Tell the user this before running it if the library is large.
2. Read `scripts/out/metadata_dry_run.json` and `scripts/out/metadata_review.json`. Summarize for
   the user: total records touched, a breakdown by fix type (the "fix counts" the script printed),
   and — if `metadata_review.json` has entries — call those out explicitly as needing the user's
   own judgement (CrossRef itself had the author order wrong, or count mismatches), not something
   this flow will touch.
3. Unless trust mode: show a handful of sample diffs (3-5, varied across fix types) and wait for
   explicit confirmation before proceeding.
4. `python scripts/metadata.py --commit`. Report `records_written` and `backup_path` back to the
   user. If the run raises the "open by another process" error, tell the user plainly and stop —
   do not retry silently more than once.
5. Suggest `rebuild_index` (the MCP tool) afterward so subsequent reads in the same session see
   the fresh data immediately, though the background watcher usually beats you to it.

## Dedup flow

1. `python scripts/dedup.py` (no `--commit`).
2. Read `scripts/out/dedup_resolved.json`, `dedup_needs_review.json`, and `dedup_merge_updates.json`.
   Summarize: how many duplicate groups, how many auto-resolved vs. need review, how many keep-side
   records gain merged fields (and roughly what kinds — don't dump every field), and the exact
   `needs_review` groups with a one-line reason each (3+ members, both/neither side has a PDF, or
   no-DOI fuzzy match) — these are for the user to look at themselves, this flow will not guess.
3. Unless trust mode: show the counts and 2-3 example resolved groups (rec numbers, titles if you
   want to fetch them via `get_reference_details`, keep/delete decision, what would be merged), and
   wait for explicit confirmation.
4. `python scripts/dedup.py --commit`. This merges first, then tags — report both `records_written`
   counts and the `backup_path` values.
5. Tell the user exactly what to do next in EndNote: build a Smart Group with the condition
   `<column> contains <marker>` (defaults `custom_7` / `REF_CLEAN_DUPLICATE`, both scripted flags
   if the user wants different ones), review it, and delete the matches themselves. Offer to
   re-verify afterward (e.g. `library_stats`) if they want confirmation the count dropped.

## Files flow

1. `python scripts/files.py` (no `--commit`).
2. Read `scripts/out/files_report.json`. Summarize: orphaned PDF count and total size, prunable
   backup count and total size (defaults: keep the 5 most recent backups, and anything newer than
   14 days regardless of count — mention if the user wants different thresholds via
   `--keep-backups`/`--backup-age-days`).
3. Unless trust mode: show the counts (not necessarily every path) and wait for confirmation.
4. `python scripts/files.py --commit`. Orphaned PDFs are *moved* to `<pdf_dir>/_ref_clean_orphans/`
   (reversible — tell the user where, so they can delete that folder themselves once they've
   checked it), not deleted outright. Excess backups are deleted for real, since a backup existing
   only to be deleted by the very next backup isn't protecting anything — but only the ones that
   were both beyond the recency count and past the age threshold, which the report already showed.

## What this skill will never do

Do not try to work around either of these by writing new code when the skill is invoked —
they are the documented, agreed boundary of this project, not a gap this session should close:

- **No real deletion.** Dedup tags and merges; the user deletes, in EndNote, where it's visible
  and reversible from the trash. `mark_references` and every script here are built around that.
- **No `year` writes.** It is a MyISAM index key. See `commit.py`'s and `writeback.py`'s module
  docstrings for what was actually tried and where it stopped.
