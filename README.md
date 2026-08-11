# endnotex9-mcp

A local-first [Model Context Protocol](https://modelcontextprotocol.io/) server for EndNote X9.
It lets Codex, Claude, and other MCP clients search a library, inspect existing groups, build an
auditable classification, format citations, and read attached PDFs without uploading the library
to a third-party service.

[简体中文说明](docs/README.zh-CN.md)

> [!IMPORTANT]
> Direct library parsing is tested against EndNote X9. The format is undocumented; run
> `endnotex9-mcp crosscheck` before trusting a later EndNote version. Almost every MCP tool is
> read-only. Two tools — `update_reference_fields` and `mark_references` — can write to the
> library, and only to a narrow, pre-verified set of columns: bibliographic fields (volume,
> issue, pages, article number, number of volumes), journal, notes, title, abstract, the author
> list, the keyword list, or the unused Custom 2–7 fields. `year` cannot be changed at all — it
> is a MyISAM index key. Both default to a no-op preview (`dry_run=True`) and back
> up the whole library before any real write. See [Safety model](#safety-model).

## Features

- Direct, read-only access to X9 `.enl` and `<Library>.Data/rdb` tables; no MySQL server required.
- XML export fallback and field-by-field cross-checking.
- CJK-aware SQLite FTS5 search with multi-query merging for bilingual libraries.
- EndNote group and smart-group discovery.
- Reproducible, review-gated classification with taxonomy, rules, evidence, conflict handling,
  holdout audits, and a read-only `Custom 1` writeback preview.
- Two narrow, backup-first write tools: correcting text fields (volume, issue, pages, article
  number, number of volumes, journal, notes, title, abstract, the author list, the keyword list),
  and tagging records in Custom 2–7 so the user can bulk-act on them with an EndNote Smart Group.
- Incremental PDF text extraction, full-text search, and page-range reading by title or EndNote
  record number.
- Consistent record-number, title, and DOI selectors with ambiguity reporting instead of
  first-match guessing.
- APA 7th and GB/T 7714-2015 citations plus BibTeX export.
- Incremental background re-indexing when the source library changes.

## Safety model

The normal read path never writes to `.enl`, `.MYD`, `.MYI`, or attachment files. Classification
state and extracted PDF text live in a separate local SQLite index. `prepare_classification_writeback`
only produces a fingerprinted preview; there is still no commit path for the `Custom 1` topic field.

Two tools do write to the library: `update_reference_fields` (volume, issue, pages, article
number, number of volumes, journal, notes, title, abstract, author list, keyword list; `year`
cannot be changed — see below) and `mark_references` (a
marker string into Custom 2–7, meant to drive an EndNote Smart Group rather than delete anything
directly — this server never deletes a record). Both share the same guardrails:

- The column allowlist is mechanical, not just policy: `writeback.WRITABLE_COLUMNS` only accepts
  columns verified to sit outside every EndNote `.MYI` key and sort cache
  (`csort`/`misc.DbSortedRecList`). `author`, `secondary_title` (journal) and `keywords` are the
  one case that looked risky on paper — they back three of EndNote's own term lists — so
  `terms.MYD` was read byte for byte against a real library rather than assumed safe: all 4818
  rows fit one fixed shape (a row id, a `list_id` naming which term list, a length-prefixed
  string) with **no field anywhere linking a term back to the `refs` records that use it**. That
  makes `terms`/`jterms` a denormalized autocomplete cache, not a structure `refs` rows are
  validated against — writing straight to `refs` just leaves that cache stale (old values listed,
  new ones missing) until the user reopens EndNote's Tools/References → Define Term Lists →
  Update List, which is a UI-only gap, not a corruption risk. See the module docstring in
  `writeback.py` for the full byte-level evidence, including empirical confirmation that a stale
  `misc.DbSortedRecList` sort cache does not break a real library either. `title` got its own
  pass for the same reason `year` didn't: a byte-level read of the real `refs.MYI` (19 x 1024-byte
  pages) located both key roots directly — page 1 (`id`) and page 13 (`yearIndex`, confirmed by
  finding the literal text `"2017"` inside it) — and every page in the file is accounted for
  between the two trees, with none left over for a `title` index to hide in.
- `year` cannot be written at all: it is one of the library's only two MyISAM keys, and changing
  an indexed value means removing and reinserting a B-tree key, not just editing bytes in place.
  A 2026-08 spike against the real `refs.MYI` (read-only) found the key roots and page layout but
  stopped short of the page-internal format and the insert/delete/rebalance algorithm — treat
  `year`, like record deletion, as a separate future project, not part of this tool.
- Both tools default to `dry_run=True`: the first call always returns a diff and writes nothing.
  A second, explicit `dry_run=False` call is required to commit.
- A commit only ever touches fields that actually change, and always backs up the entire library
  (`.enl` plus the whole `rdb` directory) to a fresh timestamped folder first — see `backup_path`
  in the result.
- Every write re-reads the rewritten file block by block and verifies the target columns hold
  exactly the intended value and every other record is byte-identical to before, raising before
  anything is copied over the library on any mismatch.
- The `.enl` mirror is rebuilt and checked *before* `refs.MYD`/`refs.MYI` are touched, so a repack
  that was going to fail costs nothing instead of leaving the `rdb` tables updated and the mirror
  stale. Every member of the archive is carried across and only `refs.MYD` is substituted: the
  libraries inspected so far hold just `refs.MYD` and `refs.frm`, but silently dropping an
  unanticipated member would be unrecoverable.
- **Close EndNote before a real commit.** Both tools write `refs.MYD`/`refs.MYI`/`.enl` directly,
  outside EndNote's own process; writing while EndNote has the library open risks a sharing
  conflict or, worse, EndNote's stale in-memory copy silently overwriting the change later. A
  best-effort lock check (`writeback._ensure_unlocked`) refuses the write if the files look open
  elsewhere, but it is not a guarantee — a process that opened with share-read/share-write access
  would not be caught. Close EndNote regardless of what the check says.

The rest of `writeback.py`'s low-level API (arbitrary-column `set_column`/`set_fields`) remains
unexposed to MCP clients; only the two narrow, field-scoped wrappers in `commit.py` are tools. See
[SECURITY.md](SECURITY.md) before extending writeback further, and test any change against a
disposable copy of a real library before trusting it against a live one.

## Requirements

- Python 3.10 or newer
- An EndNote X9 `.enl` library, an EndNote XML export, or both
- Optional: PyMuPDF for PDF text extraction

The `.enl` library is the preferred source. An XML export is useful as a fallback, as a format
cross-check, and for learning EndNote reference-type names.

## Install as a plugin (Claude Code or Codex)

The repository is also an agent plugin: the MCP server plus four skills that drive it. This is the
shortest path for either agent, and it needs [uv](https://docs.astral.sh/uv/) on the PATH — the
plugin launches the server with `uv run --directory <plugin>`, which builds its own environment on
first use, so there is nothing to `pip install` yourself.

Claude Code:

```text
/plugin marketplace add HymanLabX/endnotex9-mcp
/plugin install endnotex9@endnotex9
```

Codex:

```bash
codex plugin marketplace add HymanLabX/endnotex9-mcp
codex plugin add endnotex9@endnotex9
```

Both read the same tree. Claude Code takes its manifest from `.claude-plugin/plugin.json`; Codex
prefers `.codex-plugin/plugin.json` and falls back to the Claude one, and both expand the same
`${CLAUDE_PLUGIN_ROOT}` in [`.mcp.json`](.mcp.json), so one `.mcp.json` serves both. The two
manifests are kept in step by `tests/test_plugin_manifests.py`.

**One Codex-specific caveat: the sandbox.** Codex's default `workspace-write` policy confines
writes to the working directory and blocks network access, and your EndNote library almost
certainly lives outside that directory. Reading works, but `lit-cleaner`'s CrossRef lookups need
the network, and any `--commit` — like every `update_reference_fields`/`mark_references` write —
needs the library's own directory to be writable. Grant those explicitly rather than reaching for
`danger-full-access`:

```toml
# ~/.codex/config.toml
[sandbox_workspace_write]
network_access = true
writable_roots = ["D:\\Research"]   # the folder holding the .enl and its .Data directory
```

This changes nothing about the server's own guarantees: writes still default to a dry run, still
back up the whole library first, and still refuse to run while EndNote holds the library open.

Then run the one-time setup so the server knows which library to read:

```bash
uv run --directory <plugin-dir> endnotex9-mcp setup
```

`/plugin` and `codex plugin list` both report the installed directory; on a source checkout, use
the checkout root instead and point the marketplace at `.` (`/plugin marketplace add .`,
`codex plugin marketplace add .`).

The plugin ships four skills. Claude Code exposes them as slash commands; Codex lists them as
`endnotex9:<name>`:

| Skill | What it does |
|---|---|
| `/lit-cleaner` | Fill in or correct metadata, merge duplicates, tidy orphaned PDFs and old backups. |
| `/lit-downloader` | Download, verify, and natively attach full-text PDFs to selected records (Windows). |
| `/lit-classifier` | Profile, propose a taxonomy, write bounded rules, audit, and preview the `Custom 1` writeback. |
| `/lit-deep-reader` | Close-read one paper from its attached PDF at triage/standard/deep depth, verify its claims against the full text, and optionally save a note into its Notes field. |

`lit-deep-reader` writes nothing by itself: its `notes_block.py` merges the reading note into
whatever the Notes field already holds and prints the `update_reference_fields` payload, so the
write still goes through that tool's dry-run-then-confirm gate.

`lit-downloader` is Windows-only and runs from the `lit-downloader` extra (Playwright, PyMuPDF,
pywinauto); its own SKILL.md gives the `uv run --extra lit-downloader …` form. It drives the
Microsoft Edge already installed on the machine (`channel="msedge"`), so Playwright has no browser
to download.

## Install as a plain package

For Claude Desktop and every other MCP client — and for Codex if you would rather register the
server by hand than install the plugin — install the package directly.

Clone the repository, create a virtual environment, and install the package:

```bash
python -m venv .venv
python -m pip install -e ".[pdf]"
```

Activate the environment before running commands, or use the executable inside `.venv` by its
absolute path. Omit `[pdf]` if PDF extraction is not needed.

Run the interactive setup once:

```bash
endnotex9-mcp setup
endnotex9-mcp index-pdfs
endnotex9-mcp status
```

For scripts or managed installations, pass paths directly; supplying path flags makes setup
non-interactive:

```bash
endnotex9-mcp setup --library "/path/to/Library.enl"
endnotex9-mcp setup --xml "/path/to/export.xml" --pdf-dir "/path/to/Library.Data/PDF"
endnotex9-mcp doctor --json
```

Use `--skip-index` to write configuration only, `--db-path` for a custom index location, and
`--no-watch` to disable background refresh. Non-interactive setup rejects missing source paths
instead of saving a configuration that cannot run.

`setup` stores paths and the local SQLite index in the operating system's application-config
directory. It does not copy the EndNote library into this repository.

**On Windows, the library `setup` writes is a fallback, not a pin.** Every `load_config` call
(every CLI command, every lit-cleaner script run, and the MCP server's own startup) checks the
registry for whichever library EndNote for Windows currently has open (`Recent Libraries\File1`,
confirmed by a file-lock check — a merely recently used but now-closed library never counts) and
prefers it over the stored path whenever they differ. Point `setup` at a library once; afterward,
whichever `.enl` you actually have open in EndNote takes over automatically, and the stored path
is only used when nothing is currently open (EndNote isn't running, or you're on a non-Windows
platform, or the library is only ever an XML export). `endnotex9-mcp status` marks the line
`(auto-detected: currently open in EndNote)` when this override is active.

## Connect an MCP client

### Codex

Installing the plugin already registers the server; `codex mcp list` will show it. To register it
by hand instead:

```bash
codex mcp add endnotex9 -- endnotex9-mcp serve
codex mcp get endnotex9 --json
```

If `endnotex9-mcp` is not on the PATH used by Codex, replace it with the absolute path to the
virtual environment executable. A TOML example is available at
[`examples/codex-config.toml`](examples/codex-config.toml).

### Claude Desktop or Claude Code

Add the following server entry to the relevant MCP configuration:

```json
{
  "mcpServers": {
    "endnotex9": {
      "command": "endnotex9-mcp",
      "args": ["serve"]
    }
  }
}
```

See [`examples/claude-mcp.json`](examples/claude-mcp.json). Use an absolute executable path when
the client does not inherit the virtual environment's PATH.

### Other clients

Use the stdio transport and launch:

```text
endnotex9-mcp serve
```

## Example requests

```text
Search my library for elastocaloric cooling in both English and Chinese.

Read pages 1-3 of "Achieving excellent superelasticity and extraordinary
elastocaloric effect in a directionally solidified Co-V-Ga alloy".

Profile the library, propose a topic taxonomy, and wait for my approval before
evaluating classification rules.

Format these references in GB/T 7714-2015 and export their BibTeX entries.
```

## MCP tools

### Search and browse

| Tool | Purpose |
|---|---|
| `search_references` | Search titles, authors, abstracts, and keywords with filters and bilingual query merging. |
| `list_references` | Browse by author, journal, keyword, year, type, language, label, group, or topic. |
| `find_related` | Find a term-overlap neighbourhood selected by record number, title, or DOI. |
| `list_groups` | List EndNote groups and smart groups. |
| `list_topics` | Count topic labels stored in the index. |
| `library_stats` | Report corpus size, coverage, years, journals, authors, keywords, groups, and PDF status. |
| `get_reference_details` | Return complete metadata selected by record number, title, or DOI. |

### PDF full text

| Tool | Purpose |
|---|---|
| `index_pdfs` | Incrementally extract text from selected or all attachments. |
| `search_fulltext` | Search extracted page text and return matching pages and excerpts. |
| `read_pdf_pages` | Read a page range by title, DOI, or `rec_number`. Ambiguous selectors return candidates. |

PyMuPDF is optional because it is AGPL-3.0/commercially licensed while this project is MIT.
Scanned PDFs without a text layer currently require external OCR.

### Citation and export

| Tool | Purpose |
|---|---|
| `get_citation` | Format one APA 7th or GB/T 7714-2015 citation selected by title, DOI, or record number. |
| `get_bibliography` | Format an ordered list of mixed title, DOI, and record-number selectors. |
| `get_bibtex` | Export mixed selectors as BibTeX while preserving EndNote labels as citation keys. |

### Classification

The classification API deliberately uses explicit review gates:

1. `profile_library` and `sample_references`
2. `create_classification_session`
3. `save_classification_taxonomy` and `approve_classification_taxonomy`
4. `save_classification_rules` and `freeze_classification_rules`
5. `evaluate_classification_rules` and `list_unresolved_classifications`
6. `create_classification_audit`, `submit_classification_audit`, and
   `get_classification_report`
7. `prepare_classification_writeback` for a read-only, fingerprinted diff

Taxonomies and rules are versioned and bound to a library fingerprint. Rule predicates are
declarative and bounded; arbitrary Python, SQL, and regular-expression execution is rejected.

### Writing back

| Tool | Purpose |
|---|---|
| `update_reference_fields` | Fill in or correct volume, issue, pages, article number, number of volumes, journal, notes, title, abstract, the author list, or the keyword list. Defaults to a `dry_run` diff preview; `dry_run=False` backs up the library and commits. |
| `mark_references` | Tag records in Custom 2–7 with a marker (e.g. for an EndNote Smart Group to bulk-delete against). Same `dry_run`/backup pattern; never deletes anything itself. |

See [Safety model](#safety-model) for exactly what these tools can and cannot touch.

### Maintenance

| Tool | Purpose |
|---|---|
| `rebuild_index` | Refresh metadata and groups, incrementally by default. |
| `index_pdfs` | Refresh extracted PDF text, incrementally by default. |

The CLI also provides `index`, `index-pdfs`, `crosscheck`, `status`, and read-only `doctor`
commands.

## How the data path works

```text
EndNote X9 .enl / <Library>.Data/rdb ── read-only parser ──┐
                                                           ├─ local SQLite + FTS5 ─ MCP stdio
Optional XML export ── fallback / cross-check ─────────────┤
<Library>.Data/PDF ── optional extraction ─────────────────┘
```

EndNote X9 stores the working tables in MyISAM-style files. The parser walks those files directly
and rejects torn or malformed reads instead of silently accepting partial data. Existing groups
are decoded from the library's `misc` table; they are not present in the XML export.

## Privacy

Do not commit a real library, XML export, `.Data` directory, extracted index, or PDF attachment.
The repository's `.gitignore` excludes common EndNote data and local MCP configuration. Tests use
synthetic fixtures created in temporary directories.

Attachment paths are resolved only inside the configured PDF directory, including after URL
decoding and symlink resolution. XML imports reject DTD/entity declarations and disable network
and entity resolution in `lxml`.

## Development

```bash
python -m pip install -e ".[pdf,dev]"
ruff check src tests
pytest
python -m build
```

See [the architecture guide](docs/architecture.md) for module boundaries and
[CONTRIBUTING.md](CONTRIBUTING.md) for contribution and independent-implementation rules.

## Limitations and roadmap

- EndNote versions newer than X9 require an explicit `crosscheck` before they are trusted.
- OCR for scanned PDFs is not included.
- Semantic/embedding search is not included.
- EndNote groups and smart groups are read-only; `mark_references` writes a Custom-field marker
  for the user to build a Smart Group from, but does not create or edit one itself.
- Writing is limited to `update_reference_fields`'s text/list-field allowlist (volume, issue,
  pages, article number, number of volumes, journal, notes, author, keywords) and Custom 2–7 via
  `mark_references`; there is still no commit path for `Custom 1`, no field beyond those
  allowlists (title and year stay read-only), and no record-deletion capability at all.
- Writing `author`/`journal`/`keywords` does not update EndNote's Author/Journals/Keywords term
  lists; run Tools/References → Define Term Lists → Update List in EndNote afterward if stale
  autocomplete entries matter to you.
- Citation styles are currently limited to APA 7th and GB/T 7714-2015.

## License and trademarks

Released under the [MIT License](LICENSE).

EndNote is a trademark of Clarivate. This independent project is not affiliated with or endorsed
by Clarivate. MCP client names are used only to describe interoperability.
