# Contributing

Thank you for helping improve `endnotex9-mcp`.

## Development setup

```bash
python -m venv .venv
python -m pip install -e ".[pdf,dev]"
ruff check src tests skills
ruff format --check src tests skills
pytest
python -m build
```

Tests create synthetic EndNote-like fixtures in temporary directories. Never commit a real
`.enl` library, `.Data` directory, XML export, SQLite index, or PDF attachment.

## Working on the plugin

The root `.mcp.json` is the plugin's server manifest, not a project configuration. Its
`${CLAUDE_PLUGIN_ROOT}` is filled in only when an agent loads the directory as an installed
plugin, and `tests/test_plugin_manifests.py` requires it to stay that way. Opening a checkout as
a Claude Code project therefore starts an `endnotex9` server that fails at once, because uv is
handed the unexpanded path. Register a local-scope server under the same name instead: local
scope takes precedence over the project's `.mcp.json` and stays out of the repository.

```bash
claude mcp add --scope local endnotex9 -- uv run --directory "<checkout>" --extra pdf endnotex9-mcp serve
```

Do not reach for `${CLAUDE_PLUGIN_ROOT:-.}` to make one file serve both cases. Claude Code's
plugin loader resolves that form to `.`, the user's own working directory, and the installed
plugin stops starting.

To exercise the plugin the way a user receives it:

```bash
claude plugin validate .
claude plugin marketplace add .
claude plugin install endnotex9@endnotex9
```

A local marketplace runs the plugin straight from the checkout, so the next session picks up
your edits. The first launch builds the uv environment and can outlast the 30-second MCP
connection timeout; see the setup note in the README.

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
