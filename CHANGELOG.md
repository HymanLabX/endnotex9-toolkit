# Changelog

## 0.3.0 — 2026-11-02

### Upgrade notes

- Library selection is now pinned by setup. Use `setup --library <path>` or
  `setup --use-open-library`, then restart the MCP server when switching libraries.
- New configurations use per-library indexes. An existing index is never silently
  reused for another source. `index --adopt-source` verifies all source hashes before
  binding an unidentified legacy index, preserving reviewed classification state.
- Cleaner reports and caches live in the printed per-library `report_dir`, outside
  the installed plugin. `--commit` applies a saved preview and rejects changed sources.
  Close EndNote before metadata, duplicate and file-cleanup commits.

### Fixes and improvements

- Serialize toolkit writers across threads and processes. Journal before-images,
  roll back ordinary publication failures, and support explicit `recover-writeback`
  after process interruption. Recovery refuses edits outside the transaction.
- Limit backup pruning to identifiable, completed backups belonging to the selected
  library, using creation time rather than directory-name ordering.
- Preserve Unicode in duplicate-title keys. Invalidate removed/missing attachment
  text and prevent publisher URL exceptions from bypassing PDF validation.
- Share title/DOI normalization and PDF validation across workflows. Reuse existing
  extracted text correctly during paper reading.
- Index exact normalized-title and DOI selectors. Lock plugin dependencies in
  `uv.lock` and test the same dependency snapshot in CI.
- Exclude machine settings, runtime reports, bytecode, build output and cleanup
  backups from source distributions. Preserve format investigations in dedicated docs.

The plugin retains its 30 MCP tools and five skills. Custom 1 classification remains
preview-only; indexed year changes and record deletion remain unsupported.
