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
> issue, pages, article number, number of volumes), journal, notes, the author list, the keyword
> list, or the unused Custom 2–7 fields. Both default to a no-op preview (`dry_run=True`) and back
> up the whole library before any real write. See [Safety model](#safety-model).

## Features

- Direct, read-only access to X9 `.enl` and `<Library>.Data/rdb` tables; no MySQL server required.
- XML export fallback and field-by-field cross-checking.
- CJK-aware SQLite FTS5 search with multi-query merging for bilingual libraries.
- EndNote group and smart-group discovery.
- Reproducible, review-gated classification with taxonomy, rules, evidence, conflict handling,
  holdout audits, and a read-only `Custom 1` writeback preview.
- Two narrow, backup-first write tools: correcting text fields (volume, issue, pages, article
  number, number of volumes, journal, notes, the author list, the keyword list), and tagging
  records in Custom 2–7 so the user can bulk-act on them with an EndNote Smart Group.
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
number, number of volumes, journal, notes, author list, keyword list) and `mark_references` (a
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
  `misc.DbSortedRecList` sort cache does not break a real library either.
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

## Install

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

## Connect an MCP client

### Codex

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
| `update_reference_fields` | Fill in or correct volume, issue, pages, article number, number of volumes, journal, notes, the author list, or the keyword list. Defaults to a `dry_run` diff preview; `dry_run=False` backs up the library and commits. |
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
