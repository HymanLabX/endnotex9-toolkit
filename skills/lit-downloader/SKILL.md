---
name: lit-downloader
description: Download, verify, and attach full-text PDFs to existing EndNote X9 references selected by the user, either one record or a controlled batch. Use when the user invokes /lit-downloader, asks to download the PDF for the selected EndNote record, asks to fill missing PDF attachments for several selected records, or asks to resume an interrupted lit-downloader batch through campus or institutional access. Prefer legal open-access copies, fall back to Web of Science and the publisher in a visible browser, and never use EndNote Find Full Text as the primary route.
---

# lit-downloader

Attach verified PDFs only to existing EndNote X9 records. Use the helper for selection, durable
state, retrieval, validation, native attachment, and index refresh. Do not reproduce those steps
with ad hoc browser or filesystem actions.

## Load only what is needed

- Read `references/browser-download.md` before changing browser or EndNote automation.
- Read `references/batch-jobs.md` before running, resuming, or diagnosing a batch.
- Read the scripts only when modifying or debugging the implementation.

## Preflight

Require Windows, exactly one open EndNote X9 library, and the intended records selected in the
visible reference list. Keep the title column visible; when titles are duplicated, also show year.

This skill ships inside the `endnotex9` plugin, whose root also holds the `endnotex9-mcp`
package and this skill's `lit-downloader` extra (Playwright, PyMuPDF, pywinauto). Run the helper
through that environment so those dependencies are present:

```powershell
uv run --directory "$env:CLAUDE_PLUGIN_ROOT" --extra lit-downloader python "$env:CLAUDE_PLUGIN_ROOT/skills/lit-downloader/scripts/download_selected.py" --status
```

Use the shell's own syntax for that variable: `$env:CLAUDE_PLUGIN_ROOT` in PowerShell, as above,
and `${CLAUDE_PLUGIN_ROOT}` in bash. PowerShell reads `${CLAUDE_PLUGIN_ROOT}` as a *shell*
variable, not an environment one, and silently substitutes an empty string. This skill is
Windows-only, so PowerShell is the shell you will usually be in.

Below, `download_selected.py <flags>` is shorthand for exactly that command shape with the flags
swapped. If `CLAUDE_PLUGIN_ROOT` is unset — you are working in an endnotex9-mcp checkout rather
than through the installed plugin — substitute the checkout root; the helper locates the package
by walking up from its own path, so both layouts work.

Inspect the selection without mutation, then stop and read the result:

```powershell
download_selected.py --status
```

Stop and ask the user to correct the selection when the returned records are not the intended
ones. Never infer a batch from all records lacking attachments.

## Execute

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

## Interpret the result

- Single-record success is only `status: attached`.
- Batch success is `batch_complete`; inspect `summary` and `batch-report.csv` for every item.
- `batch_partial` means queued work remains because of `--limit`.
- `batch_paused` means user action or an access circuit breaker stopped the run. Resolve the
  reported cause, restore the pending selections, and use `--resume`.
- `batch_finished_with_errors` requires reporting failed record numbers and their terminal stages.

Report the batch ID, counts by status, report path, and any records needing user action. For a
single record, report record number, title, page count, size, and relative attachment name. Never
expose signed URLs, cookies, or resolver contact emails.

## Non-negotiable boundaries

- Require `%PDF-`, a parseable document, DOI presence, and title similarity before attachment.
- Skip existing PDFs; never replace or duplicate them automatically.
- Attach only through EndNote's native Attach File dialog with relative-copy enabled.
- Never write `refs.MYD`, `refs.MYI`, `.enl`, or `link_to_pdf` directly.
- Never use Sci-Hub, stealth browsers, CAPTCHA solvers, fixed-coordinate clicks, or signed-URL
  replay outside the authorized browser session.
- Keep publisher downloads sequential and respect institutional and publisher access policies.
- Treat Web of Science as discovery and linking infrastructure, not as the PDF host.
