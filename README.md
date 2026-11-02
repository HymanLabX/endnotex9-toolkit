# endnotex9-toolkit

A local-first [Model Context Protocol](https://modelcontextprotocol.io/) server for EndNote X9.
It lets Codex, Claude, and other MCP clients search a library, inspect existing groups, build an
auditable classification, format citations, and read attached PDFs without uploading the library
to a third-party service.

[简体中文说明](docs/README.zh-CN.md)

For the library-selection and saved-preview changes in 0.3.0, see the
[upgrade notes](CHANGELOG.md#030--2026-11-02).

> [!NOTE]
> Renamed from `endnotex9-mcp` in 0.2.0. The old command still starts the same CLI, and a
> configuration written by an earlier `setup` is still found where it was saved.

> [!IMPORTANT]
> Direct library parsing is tested against EndNote X9. The format is undocumented; run
> `endnotex9-toolkit crosscheck` before trusting a later EndNote version. Almost every MCP tool is
> read-only. Two tools — `update_reference_fields` and `mark_references` — can write to the
> library, and only to a narrow, pre-verified set of columns: bibliographic fields (volume,
> issue, pages, article number, number of volumes), journal, notes, title, abstract, DOI, the
> author list, the keyword list, or the unused Custom 2–7 fields. `year` cannot be changed at all — it
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
  number, number of volumes, journal, notes, title, abstract, DOI, the author list, the keyword
  list), and tagging records in Custom 2–7 so the user can bulk-act on them with an EndNote Smart Group.
- Incremental PDF text extraction, full-text search, and page-range reading by title or EndNote
  record number.
- Consistent record-number, title, and DOI selectors with ambiguity reporting instead of
  first-match guessing.
- APA 7th and GB/T 7714-2015 citations plus BibTeX export.
- Incremental background re-indexing when the source library changes.

## Safety model

Ordinary retrieval never modifies EndNote source files. Classification is stored separately
in the local index, and Custom 1 writeback remains a preview with no commit tool.

`update_reference_fields` and `mark_references` are the only MCP write tools. Both default to
`dry_run=True`, use independent field allowlists, and back up `.enl` plus the complete `rdb`
directory before a real change. Attachment files are not part of this metadata backup.
Pass the preview's `source_fingerprint` as `expected_fingerprint` on commit to reject changes
made since review. The cleaner scripts apply their saved, fingerprinted previews directly.

**Close EndNote before a real write or cleanup commit.** File-lock probes are best-effort.
A separate cross-process toolkit lock serializes policy checks, backups and publication, but
cannot control EndNote's own writes. Every rewritten row and the free-space ledger are checked;
the mirror is staged before publication. Ordinary publication failures roll back all three
files. A process interruption preserves before-images in `<Library>.enl.writeback-recovery`
and blocks further writes and source indexing until explicit recovery:

```bash
endnotex9-toolkit recover-writeback --library "/path/to/Library.enl"
endnotex9-toolkit index
```

Recovery refuses to overwrite files changed outside that transaction. Keep EndNote closed and
preserve the journal and backups if recovery fails. This protects against process interruption;
it is not a guarantee against storage failure or every power-loss scenario.

Backup pruning uses per-library ownership and creation time. Unrelated folders, incomplete
backups and unidentifiable legacy backups are retained. Orphan PDFs are quarantined with their
relative paths intact. See [SECURITY.md](SECURITY.md) and the
[format evidence](docs/writeback-format-notes.md) for the low-level boundary.

## Requirements

- Python 3.10 or newer
- An EndNote X9 `.enl` library, an EndNote XML export, or both
- Optional: PyMuPDF for PDF text extraction

The `.enl` library is the preferred source. An XML export is useful as a fallback, as a format
cross-check, and for learning EndNote reference-type names.

## Install as a plugin (Claude Code or Codex)

The repository is also an agent plugin: the MCP server plus five skills that drive it. This is the
shortest path for either agent, and it needs [uv](https://docs.astral.sh/uv/) on the PATH — the
plugin launches the server with `uv run --directory <plugin>`, which builds its own environment on
first use, so there is nothing to `pip install` yourself.

Claude Code:

```text
/plugin marketplace add HymanLabX/endnotex9-toolkit
/plugin install endnotex9@endnotex9
```

Codex:

```bash
codex plugin marketplace add HymanLabX/endnotex9-toolkit
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
uv run --directory <plugin-dir> --extra pdf endnotex9-toolkit setup
```

`--extra pdf` matches the environment the plugin starts the server in, so setup also finishes
building it. Building that environment is what makes a first launch slow: on a machine with an
empty uv cache it can take longer than 30 seconds, which is as long as Claude Code waits for an
MCP server to connect. If the `endnotex9` server shows as failed right after installing, run
setup, then reconnect it (`/mcp` in Claude Code) or start a new session. Before setup has run the
server still starts; every tool answers with the setup command instead of data.

`/plugin` and `codex plugin list` both report the installed directory; on a source checkout, use
the checkout root instead and point the marketplace at `.` (`/plugin marketplace add .`,
`codex plugin marketplace add .`).

The plugin ships five skills. Claude Code exposes them as slash commands; Codex lists them as
`endnotex9:<name>`:

| Skill | What it does |
|---|---|
| `/lit-cleaner` | Fill in or correct metadata, merge duplicates, tidy orphaned PDFs and old backups. |
| `/lit-downloader` | Download, verify, and natively attach full-text PDFs to selected records; or build a new collection from a Web of Science search into an EndNote import file with the PDFs already inside the library (Windows). |
| `/lit-classifier` | Profile, propose a taxonomy, write bounded rules, audit, and preview the `Custom 1` writeback. |
| `/lit-deep-reader` | Close-read one paper from its attached PDF at triage/standard/deep depth, verify its claims against the full text, and optionally save a note into its Notes field. |
| `/lit-review-writer` | Write a review over one slice of the library — a group, a research direction, a lab, or one researcher — with every claim carrying the record it came from, and a coverage ledger beside it. |

`lit-review-writer` reviews a closed corpus: it freezes the resolved records into a `corpus.json`,
reads them at an agreed depth, and its `cite_audit.py` refuses any draft citing a record outside
that file. What it can see is the collection, not the field, and it says so in the review it
writes.

`lit-deep-reader` writes nothing by itself: its `notes_block.py` merges the reading note into
whatever the Notes field already holds and prints the `update_reference_fields` payload, so the
write still goes through that tool's dry-run-then-confirm gate.

`lit-downloader` is Windows-only and runs from the `lit-downloader` extra (Playwright, PyMuPDF,
pywinauto); its own SKILL.md gives the `uv run --extra lit-downloader …` form. It drives the
Microsoft Edge already installed on the machine (`channel="msedge"`), so Playwright has no browser
to download.

Its new-collection mode (`scripts/collection/`) takes a search from export to import: merge and
tier the exports by topic rules, trim them, fetch open-access copies, open the remaining
publisher links in the user's own Chrome one at a time (ScienceDirect at most 100 a day, every
site stopped at its first block page), and write an RIS file whose `internal-pdf://` links
EndNote keeps on import. The library's database is never written, but the PDFs are copied into
its `<Library>.Data/PDF` folder ahead of the import, which the user runs.

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
endnotex9-toolkit setup
endnotex9-toolkit index-pdfs
endnotex9-toolkit status
```

For scripts or managed installations, pass paths directly; supplying path flags makes setup
non-interactive:

```bash
endnotex9-toolkit setup --library "/path/to/Library.enl"
endnotex9-toolkit setup --xml "/path/to/export.xml" --pdf-dir "/path/to/Library.Data/PDF"
endnotex9-toolkit doctor --json
```

Use `--skip-index` to write configuration only, `--db-path` for a custom index location, and
`--no-watch` to disable background refresh. Non-interactive setup rejects missing source paths
instead of saving a configuration that cannot run.

`setup` stores paths and the local SQLite index in the operating system's application-config
directory. It does not copy the EndNote library into this repository.

**Library selection is explicit and pinned.** `load_config` keeps the configured library,
XML fallback, PDF root and index together. Opening a different library in EndNote does not
retarget the toolkit. To select that library, run either:

```bash
endnotex9-toolkit setup --library "/path/to/Other.enl"
endnotex9-toolkit setup --use-open-library  # Windows: explicitly select the open X9 library
```

Restart the MCP server after changing setup. A running session rejects changed configuration
instead of mixing its cached connection with a new source. New setups use a stable per-library
index under the application-config directory; existing configured index paths are retained.
An index already bound to another source is refused, preserving reviewed classification state.
For an older nonempty index without a source identity, run `endnotex9-toolkit index --adopt-source`.
It binds the index only when every record's source hash matches; a stale or different corpus
requires a new `--db-path`, leaving the old index and reviewed state intact.
Cleaner reports and caches also live under the per-library data directory; each script prints
its absolute `report_dir`, and accepts `--output-dir` for an explicit override.

Plugin startup uses the committed `uv.lock` with `uv run --locked`. Package dependency ranges
remain available for plain pip installations; CI tests the same locked snapshot as the plugin.

## Connect an MCP client

### Codex

Installing the plugin already registers the server; `codex mcp list` will show it. To register it
by hand instead:

```bash
codex mcp add endnotex9 -- endnotex9-toolkit serve
codex mcp get endnotex9 --json
```

If `endnotex9-toolkit` is not on the PATH used by Codex, replace it with the absolute path to the
virtual environment executable. A TOML example is available at
[`examples/codex-config.toml`](examples/codex-config.toml).

### Claude Desktop or Claude Code

Add the following server entry to the relevant MCP configuration:

```json
{
  "mcpServers": {
    "endnotex9": {
      "command": "endnotex9-toolkit",
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
endnotex9-toolkit serve
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
| `update_reference_fields` | Fill in or correct volume, issue, pages, article number, number of volumes, journal, notes, title, abstract, DOI, the author list, or the keyword list. Defaults to a `dry_run` diff preview; `dry_run=False` backs up the library and commits. |
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
  pages, article number, number of volumes, journal, notes, title, abstract, DOI, author,
  keywords) and Custom 2–7 via `mark_references`; there is still no commit path for `Custom 1`,
  no field beyond those allowlists (`year` stays read-only: it is a MyISAM index key, and changing
  it needs real B-tree writes), and no record-deletion capability at all.
- Writing `author`/`journal`/`keywords` does not update EndNote's Author/Journals/Keywords term
  lists; run Tools/References → Define Term Lists → Update List in EndNote afterward if stale
  autocomplete entries matter to you.
- Citation styles are currently limited to APA 7th and GB/T 7714-2015.

## License and trademarks

Released under the [MIT License](LICENSE).

EndNote is a trademark of Clarivate. This independent project is not affiliated with or endorsed
by Clarivate. MCP client names are used only to describe interoperability.
