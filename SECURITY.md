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
pages, article number, number of volumes, journal, notes, title, abstract, DOI, the author list,
the keyword list) and `mark_references` (a marker string into Custom 2–7, for building an EndNote
Smart Group — never a delete). Neither can touch any other column, and neither can delete a
record; `custom_1` stays reserved for the classification writeback preview above, which still has
no commit path of its own.

`year` cannot be written through either tool, on purpose: it is one of only two MyISAM keys on
`refs` (`id` is the other), so changing it means removing and reinserting a B-tree key rather than
editing bytes in place — the trick every other writable column relies on does not apply. A 2026-08
read-only spike against the real `refs.MYI` confirmed the shape of the problem: the file is
exactly 19 x 1024-byte pages, both key roots were located directly (page 1 for `id`, page 13 for
`yearIndex` — confirmed by finding the literal text `"2017"` inside it), and all 19 pages are
accounted for between the two trees. What the spike did *not* reach — the page-internal entry
format and the insert/delete/rebalance algorithm — is the actual hard part, and is unsolved.
Treat `year`, and record deletion generally (which needs the same B-tree surgery, plus free-list
and term-list bookkeeping this package has never implemented), as a separate, larger project, not
something to bolt onto the current allowlist.

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
ill effect on later normal use. `title` and `abstract` were added last; `abstract` is not on
EndNote's sort menu at all, and `title` is the one entry on that menu this allowlist covers, so it
got the same key-root read described above rather than reuse of the author/journal reasoning.
`electronic_resource_number` (DOI) came after that, checked the same way plus a search of every
`rdb`/`tdb` table for DOI strings: the only other hit was PDF body text in `pdf_index`, not a copy
of the field. See `writeback.py`'s module docstring for the full record of this.

Both write tools default to `dry_run=True` (diff only, nothing written) and, on a real commit,
always back up the whole library to a fresh timestamped folder before touching anything — see
`writeback.backup_library` and the "Safety model" section of the README.

**A worked example of the above reasoning failing, and what changed because of it.** Everything in
the two paragraphs above was derived from a single library. On the second real library this package
met, part of it did not hold. That library's `csort` table was 14 KB and in active use (the
reference library's was empty, 0 rows), and its records did take the shrink path that produces
MyISAM *deleted blocks* — a path the reference library never exercised. Deleted blocks turned out
to be entries in a file-level ledger, not just spare space: `refs.MYI`'s state header counts them
(`del`, offset 36) and their bytes (`empty`, offset 76) and points at the first of them
(`dellink`, offset 52), and the blocks are chained through their own next-pointers. Writing the
blocks while leaving that ledger untouched produced a library where every record read back
correctly and the existing `_verify` pass was clean, but EndNote judged the table damaged; the only
symptom the user ever saw was that clicking a column heading stopped sorting the library, with no
error message anywhere. The same code also wrote a zero next-pointer as though it meant "no link",
when MyISAM's sentinel is `0xFFFFFFFFFFFFFFFF` and offset 0 is a real position — the first block in
the file — so the free chain was aimed at record 1's head block, which would have cost real data
the first time MyISAM reused that span.

Three things changed as a result, in decreasing order of how much they matter:

- `writeback._verify_free_space` re-derives the whole ledger from the finished bytes and fails the
  write on any mismatch, including a chain that reaches a live record. It runs against the original
  file too, so a library that arrives inconsistent is reported instead of written over. The point is
  that this class of damage is now caught before it reaches EndNote rather than by a user noticing
  a UI feature has gone quiet weeks later.
- `_verify` alone was never sufficient. "Every record reads back correctly" and "the file is a valid
  MyISAM table" are different claims, and only the first was being checked. Any future extension
  should assume the same gap exists for whatever structure it touches.
- The rule in the next paragraph — exercise a throwaway copy of *that* library first — was the
  control that would have caught this, and it was skipped. It is not optional, and "the same
  columns already worked on another library" is not a substitute for it.

**The same symptom had a second cause the ledger fix missed.** On a 1385-record library cleaned
before that fix, rebuilding the ledger left column sorting exactly as broken. Checked against
MyISAM's own rules (EndNote X9 embeds MySQL 4.1.22 as `libmysqld.dll`), the blocks themselves were
illegal: `_mi_get_block_info` rejects a deleted block shorter than 20 bytes or not a multiple of 4,
myisamchk rejects any block that does not start on a 4-byte boundary, and a deleted block's header
is 20 bytes — type, length, next-pointer *and* prev-pointer, a doubly linked chain — not the 12
this package had assumed. The old writer split a shrunk record's slack wherever the pad byte ran
out, so 32 of that library's 35 deleted blocks were off the grid (390, 343, even 12 bytes long),
and a full-table scan — which sorting needs — stopped at the first of them. Every table EndNote had
written itself passed the same checks, `pdf_index`'s 179 deleted blocks included. Now
`_terminating` frees only grid-aligned spans of at least 20 bytes, `_relink_free_chain` writes both
links, `_verify_free_space` enforces all three shape rules, and `repair_free_space` re-lays a damaged
record's blocks rather than only rewriting the ledger. The lesson above, sharpened: "every record
reads back" and "the ledger balances" are both weaker claims than "MyISAM's own checks pass".

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
