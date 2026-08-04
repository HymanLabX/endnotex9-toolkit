# Contributing

Thank you for helping improve `endnotex9-mcp`.

## Development setup

```bash
python -m venv .venv
python -m pip install -e ".[pdf,dev]"
ruff check src tests
pytest
python -m build
```

Tests create synthetic EndNote-like fixtures in temporary directories. Never commit a real
`.enl` library, `.Data` directory, XML export, SQLite index, or PDF attachment.

## Pull requests

- Keep changes focused and add regression tests for behavior changes.
- Preserve the read-only default for EndNote source files.
- Treat writeback changes as data-integrity work: test only on disposable copies and document
  recovery behavior.
- Run lint, tests, and package builds before opening a pull request.
- Update the English and Chinese documentation when user-facing behavior changes.

## Independent implementation requirement

This project is MIT-licensed and was written independently. Other EndNote and Zotero projects
may be consulted for ideas and interoperability expectations, but do not copy or closely
translate their source, non-obvious naming, or internal decomposition. In particular, code from
AGPL-licensed reference projects must not be incorporated here.

## Reporting bugs

Include the Python version, operating system, EndNote version, command used, and a redacted
traceback. Do not attach your real library or PDFs. A minimal synthetic fixture is strongly
preferred.
