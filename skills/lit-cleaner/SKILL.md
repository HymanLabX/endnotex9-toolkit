---
name: lit-cleaner
description: Clean up an EndNote X9 library through endnotex9-toolkit -- fill in or correct metadata (volume/issue/pages/journal/author/keywords/notes/title/abstract/DOI), detect and merge duplicate references (tagging the loser for manual deletion in EndNote), and clean up orphaned PDF attachments plus this tool's own old backups. Use when the user invokes /lit-cleaner, or asks to clean, dedupe, or fix up their EndNote library's metadata.
---

# lit-cleaner

Clean the library explicitly selected by `endnotex9-toolkit setup`. Opening a different
library in EndNote does not retarget the scripts. Use `setup --library <path>` or
`setup --use-open-library` to switch, then restart the MCP server.

## Run the helpers

```powershell
uv run --locked --directory "$env:CLAUDE_PLUGIN_ROOT" python "$env:CLAUDE_PLUGIN_ROOT/skills/lit-cleaner/scripts/metadata.py"
```

Replace the script name with `dedup.py` or `files.py` below. In bash, use
`${CLAUDE_PLUGIN_ROOT}`; in PowerShell, use `$env:CLAUDE_PLUGIN_ROOT`. If the variable is unset,
substitute the checkout/plugin root. Read helper source only for debugging or modification;
use `--help` for arguments. Do not reimplement the helpers inline.

Each invocation prints `report_dir: <absolute path>`. Reports and caches live in the user's
application data under `libraries/<library-id>/lit-cleaner`, outside the installed plugin.
Use that printed directory for every report listed below. `--output-dir <path>` overrides it;
use the same override for preview and commit.

| Helper | Preview artifacts | Commit behavior |
|---|---|---|
| `metadata.py` | `metadata_dry_run.json`, `metadata_review.json`, `metadata_updates.json` | Applies the saved field diff without re-querying CrossRef |
| `dedup.py` | `dedup_resolved.json`, `dedup_needs_review.json`, `dedup_merge_updates.json`, `dedup_plan.json` | Applies saved merges, then marks the losing records |
| `files.py` | `files_report.json` | Applies only saved cleanup candidates, after revalidating them |

`--commit` requires a saved preview bound to the same library and source fingerprint. If the
library changed, generate and review a fresh report. Metadata `--limit`/`--refresh`, dedup
`--marker`/`--column`, and file retention settings belong to the preview; do not change its scope
at commit time. Metadata commits cannot be combined with `--limit` or `--refresh`.

## Choose the operation

- No arguments, `status`, `总结`, or `看看情况`: run all three helpers in report mode and summarize
  counts. Ask which operation the user wants to apply; a bare invocation does not authorize writes.
- `metadata`, `元数据`, `补全`: correct metadata.
- `dedup`, `去重`, `合并`, `重复`: merge and mark duplicate candidates.
- `files`, `文件`, `孤儿`, `备份`: review attachment and backup cleanup.
- `all`, `全部`, `都`: finish metadata first, then dedup, then files, generating each preview
  against the latest state.
- `--yes`, `-y`, `信任模式`, `trust mode`: show the summary and apply the generated preview within
  the authorized scope without waiting for another approval. Existing field allowlists still apply.

Before any commit, establish that EndNote is closed, using current session evidence; if this
has not been established, ask the user to close it. Toolkit locks coordinate toolkit processes
only. By default show a few varied diffs and obtain approval before applying a preview; reuse
approval already given for that exact scope. If an interrupted-write message appears, preserve
the recovery directory and use the documented `recover-writeback` procedure; do not bypass it.

## Metadata

1. Run `metadata.py`. A first CrossRef pass may take several minutes; progress is printed.
2. Read `metadata_dry_run.json` and `metadata_review.json`. Summarize affected records, fix types,
   and 3–5 varied diffs. Author count/order disagreements remain manual-review items.
3. After authorization and EndNote closure, run `metadata.py --commit`. Report `records_written`
   and `backup_path`. Run `rebuild_index` to refresh the MCP view.

Supported fields include volume, issue, pages, article number, number of volumes, journal,
notes, title, abstract, DOI, author list and keywords. CrossRef author completion requires
matching surnames in the same order. Never invent missing metadata or write `year`.

## Duplicates

1. Run `dedup.py`. DOI duplicates are confirmed; no-DOI title/year/author matches remain review
   candidates. Automatic decisions require exactly two copies with an unambiguous keep side.
2. Summarize resolved groups, keep/delete record numbers, merged fields, and unresolved groups
   with their reasons. Review `dedup_merge_updates.json` before approving.
3. Run `dedup.py --commit` after authorization and EndNote closure. Report merge and marker
   results and backup paths. These are two recoverable commits; report either failure explicitly.
4. Explain the final native action: create an EndNote Smart Group with
   `<column> contains <marker>` (defaults `custom_7`, `LIT_CLEANER_DUPLICATE`), review, then delete
   the losing copies in EndNote. The script never deletes records.

Older groups may use `LIT_CLEAN_DUPLICATE`; use the shared `LIT_CLEAN` prefix to include both
markers, or generate the preview with `--marker LIT_CLEAN_DUPLICATE`.

## Files and backups

1. Run `files.py`. Summarize orphan PDF count/size and removable backup count/size from
   `files_report.json`. Defaults retain the 5 newest owned backups and all backups created
   within 14 days; `--keep-backups` and `--backup-age-days` configure the preview.
2. After authorization and EndNote closure, run `files.py --commit` with the same report directory.
3. Report the quarantine path `<pdf_dir>/_lit_cleaner_orphans/`. Relative subfolders are retained,
   existing destinations are skipped, and the legacy `_lit_clean_orphans` folder is left alone.
   Backup deletion is permanent and restricted to completed backups belonging to this library;
   unrelated directories and incomplete/unidentifiable backups are retained.

Classification Custom 1 stays preview-only. Record deletion and indexed `year` writes are
outside this skill's implementation; see the repository's SECURITY.md for the format boundary.
