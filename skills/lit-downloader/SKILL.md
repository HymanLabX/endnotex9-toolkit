---
name: lit-downloader
description: Get verified full-text PDFs into EndNote X9. Selected-records mode downloads, verifies, and natively attaches PDFs to existing records the user has selected, singly or in a controlled batch. New-collection mode turns a Web of Science search into a screened working list, fetches legal open-access copies, downloads the rest over several days in the user's own Chrome at publisher-safe pacing, builds an EndNote import file whose PDFs are already inside the library, and checks the import. Use when the user invokes /lit-downloader, asks for PDFs of selected EndNote records or to resume a batch, or asks to bring a WoS search into EndNote with full text, start or retry a daily download round, list what is still missing, or build or check the import file. Never use EndNote Find Full Text as the primary route.
---

# lit-downloader

Two modes share one validation rule: a PDF counts only after `validate_pdf` has checked its bytes,
its DOI, and its title. Pick the mode first.

| The user wants | Mode | Read first |
|---|---|---|
| PDFs for records **already in the library**, selected in EndNote | Selected records | `references/browser-download.md`, `references/batch-jobs.md` |
| A **new set of papers** from a search, with PDFs, imported into EndNote | New collection | `references/collection-pipeline.md` |

Records imported by the collection mode that are still without a PDF afterwards are ordinary
existing records; finish them in the selected-records mode or attach the user's downloads by hand.

Both modes run from the `lit-downloader` extra (Playwright, PyMuPDF, pywinauto) of the
`endnotex9` plugin. Use the shell's own syntax for the plugin root: `$env:CLAUDE_PLUGIN_ROOT` in
PowerShell and `${CLAUDE_PLUGIN_ROOT}` in bash; PowerShell reads `${CLAUDE_PLUGIN_ROOT}` as a
*shell* variable and silently substitutes an empty string. If `CLAUDE_PLUGIN_ROOT` is unset — you
are working in an endnotex9-mcp checkout rather than the installed plugin — substitute the
checkout root; the scripts find the package by walking up from their own path.

## Selected records mode

Attach verified PDFs only to existing EndNote X9 records. Use the helper for selection, durable
state, retrieval, validation, native attachment, and index refresh. Do not reproduce those steps
with ad hoc browser or filesystem actions. Read the scripts only when modifying or debugging them.

### Preflight

Require Windows, exactly one open EndNote X9 library, and the intended records selected in the
visible reference list. Keep the title column visible; when titles are duplicated, also show year.
Run the helper through the plugin environment:

```powershell
uv run --directory "$env:CLAUDE_PLUGIN_ROOT" --extra lit-downloader python "$env:CLAUDE_PLUGIN_ROOT/skills/lit-downloader/scripts/download_selected.py" --status
```

Below, `download_selected.py <flags>` is shorthand for exactly that command shape with the flags
swapped. Inspect the selection without mutation, then stop and read the result:

```powershell
download_selected.py --status
```

Stop and ask the user to correct the selection when the returned records are not the intended
ones. Never infer a batch from all records lacking attachments.

### Execute

For one selected record:

```powershell
download_selected.py --timeout 300
```

For all explicitly selected records, conservatively limited to 20 per invocation:

```powershell
download_selected.py --batch --limit 20 --delay 10
```

Resume a reported batch after restoring its pending selections:

```powershell
download_selected.py --resume <batch-id>
```

The helper tries Unpaywall when `UNPAYWALL_EMAIL` is configured, then OpenAlex, then Web of
Science and the publisher through one reusable visible Edge profile. If login, SSO, or CAPTCHA
appears, let the user complete it. Never request, type, record, or store credentials.

### Interpret the result

- Single-record success is only `status: attached`.
- Batch success is `batch_complete`; inspect `summary` and `batch-report.csv` for every item.
- `batch_partial` means queued work remains because of `--limit`.
- `batch_paused` means user action or an access circuit breaker stopped the run. Resolve the
  reported cause, restore the pending selections, and use `--resume`.
- `batch_finished_with_errors` requires reporting failed record numbers and their terminal stages.

Report the batch ID, counts by status, report path, and any records needing user action. For a
single record, report record number, title, page count, size, and relative attachment name. Never
expose signed URLs, cookies, or resolver contact emails.

## New collection mode

Read `references/collection-pipeline.md` before the first command; it has every stage's command,
checkpoint, and the lessons behind the pacing rules. The scripts live in
`scripts/collection/` and all take the project folder as their first argument:

```powershell
uv run --directory "$env:CLAUDE_PLUGIN_ROOT" --extra lit-downloader python "$env:CLAUDE_PLUGIN_ROOT/skills/lit-downloader/scripts/collection/status.py" <project>
```

Below, `collection/<script>.py <args>` is shorthand for that command shape.

The stages, with the points where you stop for the user:

1. **Query** — draft the search with the user; save it as `query.txt`. *Checkpoint: the user
   approves the query.*
2. **Export** — the full records as RIS, in batches of 1000, into `<project>/raw/`.
3. **Merge and tier** — `collection/merge.py` with topic rules written for this search (start
   from `scripts/collection/examples/rules_ti_sma.py`). *Checkpoint: the user reviews
   `tiers.csv` and chooses the tiers.*
4. **Trim** — `collection/trim.py` with the user's criteria, before any download. *Checkpoint:
   the user approves the criteria and the removed list.*
5. **Metadata** — `collection/crossref_cache.py`; `collection/nodoi.py` for records without a DOI.
6. **Open access** — `collection/oa.py`.
7. **Browser rounds** — `collection/round.py <project> daily`, once a day, each day only when the
   user says to start; then `retry` and `host <name>` rounds. Run in the background and watch
   `open.log`.
8. **Manual list** — `collection/checklist.py` for what is left (and document delivery).
9. **Import** — `collection/build_import.py`; the user imports the file in EndNote; then
   `collection/verify_import.py`. *Checkpoint: the user decides when to import; after that the
   same records must never be imported again.*

`collection/status.py` reports progress and why each missing record is missing at any point.

## Non-negotiable boundaries

- Require `%PDF-`, a parseable document, DOI presence (or, for an author version, near-exact
  title similarity), and title similarity before a PDF counts.
- Skip existing PDFs; never replace or duplicate them automatically.
- Attach only through EndNote itself: the native Attach File dialog with relative-copy enabled,
  or the user's own File > Import of a file whose `internal-pdf://` links point into the
  library's `PDF` folder. Never write `refs.MYD`, `refs.MYI`, `.enl`, `link_to_pdf`, or an X9.3
  `sdb` database directly.
- Never use Sci-Hub, stealth browsers, CAPTCHA solvers, fixed-coordinate clicks, or signed-URL
  replay outside the authorized browser session. A verification page is for the user to pass in
  their own browser; the scripts stop that site instead.
- Keep publisher access sequential and paced: ScienceDirect at least 60 s apart and at most 100
  links per calendar day unless the user explicitly raises it for that day; stop a site at its
  first block page or run of misses; stop at once if the user reports rate limiting.
- Close only browser windows the scripts opened; never the user's, never the last one.
- Send no user email or credentials to OpenAlex, Semantic Scholar, CrossRef, or Unpaywall unless
  the user explicitly asks.
- Treat Web of Science as discovery and linking infrastructure, not as the PDF host.
