# Architecture

`endnotex9-toolkit` separates immutable EndNote source data from rebuildable local indexes and
reviewed classification state.

```text
EndNote library / XML / PDFs
             │
             ▼
      source adapters
             │
             ▼
  SQLite metadata + FTS5 + PDF pages
             │
       ┌─────┴─────────┐
       ▼               ▼
 search/citation   classification state
       │               │
       └──────┬────────┘
              ▼
         MCP stdio tools
```

## Module map

| Area | Modules | Responsibility |
|---|---|---|
| EndNote input | `myisam`, `refstable`, `groups`, `parser`, `sources` | Read X9 tables, decode records/groups, parse XML, choose a source, and cross-check readers. |
| Local index | `db`, `indexer`, `tokenizer`, `watcher` | Own the SQLite schema, incremental indexing, CJK tokenization, and source-change monitoring. |
| Retrieval | `search`, `browse`, `resolver`, `pdf` | Metadata search, filtered browsing, safe record-number/title/DOI resolution, PDF extraction, and page retrieval. |
| Classification | `profile`, `sampling`, `classification`, `writeplan` | Profile the corpus, create reproducible samples, version taxonomy/rules, audit results, and prepare read-only writeback plans (still no commit path for `custom_1`). |
| Writeback engine | `writeback` | Low-level X9 row rewriting: a column allowlist verified safe against MyISAM keys/term lists/sort caches, multi-column writes, self-verification, and library backups. Not exposed to MCP directly. |
| Commit surface | `commit` | The only MCP-writable layer: two field-scoped, backup-first, dry-run-by-default wrappers over `writeback` — text/list fields (bibliographic fields, journal, notes, author, keywords) and Custom 2–7 markers. Never deletes a record. |
| Output | `citation`, `bibtex` | Format citations, bibliographies, and BibTeX. |
| Interfaces | `config`, `cli`, `server` | Load local configuration, expose commands, and register MCP tools. |

## Data ownership

- `.enl`, `.Data/rdb`, XML exports, and PDFs belong to EndNote and are treated as source data.
- The SQLite database belongs to this package. Metadata and PDF text can be rebuilt; reviewed
  classification sessions must be migrated rather than silently discarded.
- MCP clients receive structured results only. Source files are never returned or uploaded by
  the server.

## Safety invariants

1. Ordinary indexing and retrieval never write to EndNote source files.
2. Direct-reader failures fall back to XML only when a configured XML export is available, and
   the response reports which source was used.
3. Torn MyISAM walks fail rather than yielding partial records.
4. Attachment paths cannot escape the configured PDF root after decoding or symlink resolution.
5. XML parsing rejects DTD/entity declarations and disables entity and network resolution.
6. Ambiguous titles and duplicate DOIs return candidates rather than choosing the first record.
7. Classification evaluation is bound to immutable taxonomy, ruleset, and library fingerprints.
8. Classification writeback preparation verifies source hashes and produces a preview; it does
   not commit, and there is no commit path for `custom_1`.
9. The only two commit-capable tools (`update_reference_fields`, `mark_references`) write through
   a mechanically-verified column allowlist, default to a no-op dry run, back up the whole library
   before any real write, and self-verify the result. Neither can delete a record or touch a
   column outside its own narrow allowlist.
10. The `.enl` mirror is staged and verified before the `rdb` tables are written, and preserves
    every archive member it did not expect. A failed repack leaves the library untouched rather
    than diverged.
11. Retrieval limits are clamped (`db.MAX_LIMIT`): `limit` arrives from the calling model, and an
    unbounded or negative value would otherwise return the whole library in one reply.

## Extending the server

Keep storage logic outside `server.py`; MCP handlers should validate user-facing inputs and call a
domain module. Add domain tests first, then an MCP round-trip test in `tests/test_server.py` to
verify tool registration and SDK serialization.

Any schema change must be explicit and additive where possible. Never delete or recreate a local
index merely because its version is unknown: it may contain reviewed classification state.
