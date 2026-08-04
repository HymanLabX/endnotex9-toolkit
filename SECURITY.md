# Security Policy

## Supported versions

Security and data-integrity fixes currently target the latest release on the default branch.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting or a private security advisory when available.
Do not post a real EndNote library, attachment, XML export, configuration file, or filesystem
path in a public issue.

For ordinary bugs, open a public issue with a synthetic reproducer and redacted logs.

## Data-integrity boundary

Almost the entire public MCP surface reads EndNote source files and prepares writeback previews;
it does not commit. Two tools are the narrow exception: `update_reference_fields` (volume, issue,
pages, article number, number of volumes, journal, notes, the author list, the keyword list) and
`mark_references` (a marker string into Custom 2–7, for building an EndNote Smart Group — never a
delete). Neither can touch any other column, and neither can delete a record; `custom_1` stays
reserved for the classification writeback preview above, which still has no commit path of its
own.

`author`/`secondary_title` (journal)/`keywords` back three of EndNote's own term lists
(`terms.MYD`/`jterms.MYD`), which made them the one part of this allowlist that could not be
signed off by inference alone. `terms.MYD` was read byte for byte against a real 1386-record
library before these columns were added: every row decodes as a fixed `(row id, list_id, string)`
shape with no field tying a term back to the `refs` records that use it, meaning `terms`/`jterms`
are a denormalized cache for EndNote's term-list-manager UI, not a structure `refs` is validated
against. Writing straight to `refs` leaves that cache stale (old values still listed, new ones
missing) until EndNote's own Tools/References → Define Term Lists → Update List is run — a UI gap,
not a corruption path, and this package never opens `terms.MYD`/`jterms.MYD` at all, only
`refs.MYD`/`refs.MYI`/`.enl`. The matching sort-cache question (`misc.DbSortedRecList`/`csort`)
was also checked empirically rather than assumed: the real library's `DbSortedRecList` was found
already stale by roughly 160 records from an earlier round of manual deletions in EndNote, with no
ill effect on later normal use. See `writeback.py`'s module docstring for the full record of this.

Both write tools default to `dry_run=True` (diff only, nothing written) and, on a real commit,
always back up the whole library to a fresh timestamped folder before touching anything — see
`writeback.backup_library` and the "Safety model" section of the README.

**EndNote holding the library open during a commit is a real, unmitigated risk beyond what the
above covers.** These tools write `refs.MYD`/`refs.MYI`/`.enl` directly, outside EndNote's own
process. If EndNote still has the library open: the write may simply fail on a sharing conflict
(the safe outcome), or it may succeed while EndNote's in-memory copy stays stale — and if EndNote
itself later writes anything back to disk (autosave, closing, any edit), it can silently overwrite
this package's change, or worse, write using offsets that no longer match what is now on disk.
`writeback._ensure_unlocked` makes a best-effort check before every backup and every write
(opening each library file for read+write and refusing on `OSError`), but this is not a guarantee:
a process that opened the files with share-read/share-write access — plausible for a memory-mapped
ISAM engine — would not be caught. Treat "close EndNote first" as a hard requirement enforced by
the operator, not something the lock check can be relied on alone to catch.

The low-level engine
(`writeback.set_column`/`set_fields`, arbitrary columns in `WRITABLE_COLUMNS`) is still
experimental and is deliberately not exposed to MCP clients directly; only the two field-scoped
wrappers in `commit.py` are. Extending `WRITABLE_COLUMNS`, `TEXT_FIELDS`/`LIST_FIELDS`, or
`MARKER_COLUMNS` further needs the same kind of verification already documented in `writeback.py`'s
module docstring (not indexed, not tied back to a term list without checking, not a sort key)
before it can be trusted. Always test writeback work against disposable copies and keep
independently verified backups — for a change that will run against a real library, that means
confirming the library still opens in EndNote with no "needs repair" prompt and still sorts and
searches normally afterwards, not just that the package's own tests pass.

## Untrusted imports and attachments

Treat EndNote XML exports and attachment metadata obtained from other people as untrusted input.
The XML reader rejects DTD/entity declarations and disables network/entity resolution. Attachment
paths must remain under the configured PDF root after URL decoding and symlink resolution.

Please report any input that bypasses these containment checks using the private process above.
