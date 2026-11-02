# EndNote X9 writeback format notes

Historical byte-level evidence for the allowlist and MyISAM invariants.

Write a field back into an EndNote X9 library.

This is the dangerous half of the package and it is deliberately narrow: it
sets a handful of text columns on chosen records and does nothing else. Read
myisam.py first for the storage layout.

**Why this can be done without rebuilding MyISAM's indexes.** `refs` carries
two keys — the primary key on `id` and `yearIndex` — and neither covers the
writable columns below. MyISAM's key blocks map a key to a *record offset*,
so as long as a record keeps the file offset it already has, every index
entry stays correct. That is the whole trick here: a record that grows keeps
its head block exactly where it is and chains the overflow to a block
appended at end of file. MyISAM does this natively; 107 of the reference
library's 1561 records were already stored that way before anything was
written.

**What must be kept in sync**, established by inspection of a real library:

* `rdb/refs.MYD` — the data.
* `rdb/refs.MYI` — only the state header counters (`split` at 44 and
  `data_file_length` at 68). No key-block surgery, per the above.
* `<Library>.enl` — a *stored* (uncompressed) ZIP holding a mirror of
  `refs.MYD` and `refs.frm`; verified byte-identical to the `rdb` copy on an
  untouched library, so it has to be repacked or the two diverge.

**What must not be touched**, also verified: `terms`/`jterms` cover only the
Authors, Journals and Keywords lists (per the `TermListSpec` rows in `misc`),
so none of the columns below ever appear in them; `csort` and
`misc.DbSortedRecList` are sort caches keyed on sortable columns (EndNote's
library-list sort menu only ever offers Author/Year/Title/Journal/Rating/
Recently-Added/Recently-Updated/Custom-1..3/Url — never Volume, Issue, Pages,
or Article Number); `refs_ext` and `pdf_index` are unrelated.

`volume`, `number` (issue), `pages`, `section` (article number) and
`number_of_volumes` are bibliographic blob columns with no `FIXED_WIDTHS`
entry, so the row codec already treats them exactly like `custom_1` — this
was not re-derived from a fresh byte-level dump of this library's `.MYI` key
definitions the way `id`/`yearIndex` were; it follows from the two facts
above (documented key list, documented term-list scope) plus EndNote's own
UI never exposing these as sort fields.

**`author`, `secondary_title` (journal), `keywords` and `notes`** are the
riskier-looking case, since `author`, journals and `keywords` are exactly the
three lists `terms`/`jterms` exist for. `terms.MYD` was dumped and decoded
byte for byte against a real 1386-record library to settle this rather than
infer it:

* Every one of its 4818 rows fits one fixed shape — a 1-byte packing bitmap,
  a 4-byte little-endian row id, a 2-byte little-endian `list_id` (`1` for
  Authors, `3` for Keywords — `misc`'s `TermListSpec` rows name these; `2`,
  "Journals", is `jterms` instead, a separate file), a 2-byte little-endian
  length, then the term text. Nothing else — no field ties a term back to
  the `refs` records that use it, and no field counts how many records use
  it.
* That means `terms`/`jterms` are a denormalized *cache* of the distinct
  values currently in use — built for the term-list-manager UI and
  autocomplete — not a structure `refs` rows are validated or looked up
  against. Changing `refs.author`/`secondary_title`/`keywords` text leaves
  old entries orphaned and new ones missing from that cache until EndNote
  rebuilds it (Tools/References → Define Term Lists → Update List), which
  is a stale-until-refreshed UI gap, not a corruption risk, and it is a gap
  this package's writes were already going to leave either way — nothing
  here ever touches `terms.MYD`/`jterms.MYD`.
* The sort-cache concern was checked the same way, empirically rather than
  assumed away: `misc.DbSortedRecList` on this exact library was found
  holding a stale rec-number order left over from before ~160 records were
  deleted in EndNote, and `misc.CachedSortsTime` showed EndNote had used the
  library normally since — sorting, editing, resaving — without that
  staleness causing a problem. `csort.MYD` exists but was empty (0 rows).
* `notes` is not named by any `TermListSpec` row at all — same free-text
  category as `research_notes`.

**`title` and `abstract`** were the last two added, and `title` got the most
scrutiny of anything on this list — it is the one field on the EndNote
sort menu (see above) that a corruption would be most visible and least
forgivable in. Rather than lean on the same reasoning already used for
`author`/`secondary_title`/`keywords`, the real `refs.MYI` was read at the
byte level to settle it directly:

* The file is exactly 19 x 1024 bytes — fixed-size index pages. Two
  page-aligned pointers sit next to each other in the header (offsets 124
  and 132): `1024` (page 1) and `13312` (page 13). Given the two-key fact
  above, these are `key_root[0]` and `key_root[1]` — the B-tree roots for
  `id` and `yearIndex`, and nothing else. Page 13 settles which is which:
  it contains the literal bytes for the ASCII text `"2017"`, a year value,
  confirming `yearIndex` is keyed there (and that MyISAM indexed `year` as
  packed text, not a binary integer — one more reason it needs its own
  B-tree-surgery project rather than joining this list).
* Walking the rest of the file by page: 1 header page, the two root pages
  above, 11 dense pages reachable under page 1 and 5 dense pages reachable
  under page 13 — 1 + 2 + 11 + 5 = 19, exactly the file's page count, with
  no leftover pages a `title` (or anything else) index could be hiding in.
  Two keys, fully accounted for, confirmed independently of the module
  docstring's original claim rather than just trusting it.
* `title` is therefore in the same position `author`/`secondary_title`/
  `keywords` were: not a key, not named by any `TermListSpec` term list,
  and covered only by the same sort cache already shown to tolerate
  staleness. `abstract` is stronger still — it is not even on EndNote's
  sort-menu list, so the sort-cache question does not apply to it at all.

**`electronic_resource_number` (DOI)** came after those, once a Web of
Science import left 151 records of a 1241-record library holding
`ARTN <article number>\\r<DOI>` in it instead of a bare DOI. It was checked
read-only against that library:

* The row codec has no `FIXED_WIDTHS` entry for it, so it is a
  variable-length text column encoded exactly like `title`.
* It is not a key (the two-key `refs.MYI` read above accounts for every
  page), not named by any `TermListSpec` row, and not on the sort menu.
* Nothing else in the library holds a copy of it. Every `rdb` and `tdb`
  file was searched for DOI strings. The only other hit was
  `pdf_index.MYD`, and that was the DOI printed on the attached PDF's first
  page (`https://doi.org/...` inside the extracted body text), not the
  field: a record without a PDF matched `refs.MYD` alone, and no
  `ARTN ...\\r10.` field value appears in `pdf_index` at all.

Treat a first real run of any of these (bibliographic, author/
journal/keyword/notes, title/abstract, or DOI) against a *different* library as
unproven until it has been exercised against a throwaway copy of that
library and that copy reopens in EndNote with no "needs repair" prompt,
sorts normally, and searches normally — see `backup_library` and
SECURITY.md. This library's own `terms.MYD` and `refs.MYI` were read, never
written, in reaching the conclusions above.

**Free space is a ledger, not a spare byte range.** A record that shrinks
can leave a span too big for the single pad byte a terminating block
carries, and the excess becomes a MyISAM *deleted block*. Emitting that
block's header is only a quarter of the job: the block is one link in a
chain threaded from `dellink` (state header offset 52) through each
deleted block's own next-pointer, and the header separately counts the
blocks (`del`, offset 36) and their bytes (`empty`, offset 76). All four
have to move together.

This was learned the hard way, on a real 845-record library. An earlier
version wrote the block alone and left the ledger untouched. Every record
read back correctly and `_verify` passed — but EndNote decided the table
was damaged, and the visible symptom was that clicking a column heading
stopped sorting the library, with no error anywhere. Two details, both
confirmed by reading EndNote's own deleted block rather than assuming:

* The chain ends with `0xFFFFFFFFFFFFFFFF` (`HA_OFFSET_ERROR`), and **zero
  is not a stand-in for "no link"** — offset 0 is a real position, the
  first block in the file, so a zero next-pointer aims MyISAM's free-space
  reuse squarely at record 1's head block. That is a data-loss bug waiting
  for MyISAM to reuse the span, quite apart from the sorting breakage.
* An untouched library's ledger is exactly self-consistent (`del=1`,
  `empty=152`, `dellink` reaching that one 152-byte block, whose
  next-pointer is the sentinel), so EndNote does maintain it and there is
  no slack to borrow against.

The broader lesson is in SECURITY.md: this module's safety argument was
derived from one library, and that library's `csort` table happened to be
empty and its records happened never to take the shrink path. Neither held
for the next library it met. `_verify_free_space` exists so that a
mismatch fails the write instead of reaching EndNote.

**The blocks have a shape too, and the ledger fix alone missed it.** A
1385-record library cleaned before that fix still would not sort once its
ledger was rebuilt. MyISAM (EndNote X9 embeds MySQL 4.1.22) requires every
block to start on a 4-byte boundary, every deleted block to be at least 20
bytes and a multiple of 4, and a deleted block's header to carry a
prev-pointer as well as a next-pointer — 20 bytes, not 12. The old split put
a shrunk record's excess slack wherever the pad byte ran out, so most of that
library's deleted blocks were off the grid, and MyISAM's scanner gives up at
the first one it meets. `_terminating` now frees only legal spans,
`_relink_free_chain` writes both links, `_verify_free_space` checks the
shape as well as the ledger, and `repair_free_space` re-lays the damaged
records. See SECURITY.md.

Every write verifies itself: the resulting file is walked block by block,
every record is decoded again, the targets must hold exactly the intended
value, every other record must be byte-identical to before, and the
free-space ledger must describe the blocks actually present — checked
against the original file too, so a library that arrives inconsistent is
reported rather than written over. A failure raises before anything is
copied over the library.
