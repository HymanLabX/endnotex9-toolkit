"""Write a field back into an EndNote X9 library.

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

Treat a first real run of any of these seven (bibliographic, author/
journal/keyword/notes, or title/abstract) against a *different* library as
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

Every write verifies itself: the resulting file is walked block by block,
every record is decoded again, the targets must hold exactly the intended
value, every other record must be byte-identical to before, and the
free-space ledger must describe the blocks actually present — checked
against the original file too, so a library that arrives inconsistent is
reported rather than written over. A failure raises before anything is
copied over the library.
"""

from __future__ import annotations

import os
import shutil
import struct
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from . import myisam
from .refstable import (  # the row codec is shared with the read path
    FIXED_WIDTHS,
    PACK_BITS,
    REFS_COLUMNS,
    decode_row,
    encode_row,
)

__all__ = [
    "FIXED_WIDTHS",
    "PACK_BITS",
    "REFS_COLUMNS",
    "WRITABLE_COLUMNS",
    "WritebackError",
    "backup_library",
    "decode_row",
    "encode_row",
    "repair_free_space",
    "set_column",
    "set_fields",
]

# Columns safe to write: not indexed, not part of any term list, and not a
# sort key. Anything outside this set needs its own analysis first.
#
# `custom_1` through `custom_7`, `research_notes` and `notes` are free-form
# user fields with no structural role at all. `volume`/`number`/`pages`/
# `section`/`number_of_volumes` are ordinary bibliographic fields.
# `author`/`secondary_title`/`keywords` back three of EndNote's term lists —
# see the module docstring for the byte-level `terms.MYD` read that shows
# those lists are a denormalized UI cache with no link back to `refs`, not a
# structure these columns need to stay in sync with. `title`/`abstract` were
# verified last, `title` via a byte-level read of the real `refs.MYI` that
# accounts for both of the library's two keys (`id`, `yearIndex`) and finds
# `title` in neither — see the module docstring.
WRITABLE_COLUMNS = frozenset(
    {
        "custom_1",
        "custom_2",
        "custom_3",
        "custom_4",
        "custom_5",
        "custom_6",
        "custom_7",
        "research_notes",
        "notes",
        "volume",
        "number",
        "pages",
        "section",
        "number_of_volumes",
        "author",
        "secondary_title",
        "keywords",
        "title",
        "abstract",
    }
)

# Offsets into the .MYI state header, located by matching known values and
# cross-checked against MyISAM's documented `MI_STATE_INFO` layout — the
# same layout that puts `key_root[0]`/`key_root[1]` at 124/132, which a
# byte-level read of a real `refs.MYI` confirmed independently.
_MYI_DEL = 36  # number of deleted blocks in the data file
_MYI_SPLIT = 44  # number of blocks, deleted ones included
_MYI_DELLINK = 52  # offset of the first block in the free chain
_MYI_DATA_FILE_LENGTH = 68
_MYI_EMPTY = 76  # total bytes held by deleted blocks


class WritebackError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# block chains
# --------------------------------------------------------------------------


def _scan(data: bytes) -> list:
    blocks, pos = [], 0
    while pos < len(data):
        block = myisam._read_block(data, pos)
        blocks.append(block)
        pos += max(block.span, 1)
    if pos != len(data):
        raise WritebackError("block walk overran the data file")
    return blocks


def _chain(blocks_by_pos: dict, head) -> list:
    """The head block plus every continuation it links to."""
    chain, nxt, hops = [head], head.next_pos, 0
    while nxt and not chain[-1].last and hops < 4096:
        cont = blocks_by_pos.get(nxt)
        if cont is None:
            raise WritebackError(f"dangling continuation at {nxt}")
        chain.append(cont)
        nxt, hops = cont.next_pos, hops + 1
    return chain


# A terminating block spends 4 bytes on its header (length plus one pad
# byte), so its slack has to fit in that single byte.
_MAX_PAD = 255
# MyISAM never emits a block shorter than this, and keeps them 4-byte aligned.
_MIN_BLOCK = 20
_ALIGN = 4
# A deleted (free) block's own header: 1 type byte + 3-byte length + 8-byte
# next-pointer into MyISAM's free-block chain.
_DELETED_BLOCK_HEADER = 12
# MyISAM's `HA_OFFSET_ERROR`: the all-ones offset that ends the free chain.
# Zero is emphatically not a stand-in — offset 0 is the first block in the
# file — see `_deleted_block` and the module docstring.
_FREE_LIST_END = 0xFFFFFFFFFFFFFFFF


def _deleted_block(size: int, next_pos: int) -> bytes:
    """A type-0 (deleted) block spanning exactly `size` bytes.

    `next_pos` is this block's link in MyISAM's free-block chain, which is
    what makes the span reusable rather than merely skippable; pass
    `_FREE_LIST_END` for the last link. Emitting the block is only part of
    the job — `del`, `empty` and `dellink` in the `.MYI` state header have
    to move with it, which `_apply_free_blocks` does and
    `_verify_free_space` refuses the write without.

    **`next_pos=0` is a bug, not a "no link" sentinel.** Offset 0 is a real
    position — the first block in the file — so a zero here points MyISAM's
    free-space reuse straight at record 1's head block. The sentinel is
    `_FREE_LIST_END`, confirmed by reading EndNote's own deleted block on a
    real library.
    """
    if size < _DELETED_BLOCK_HEADER:
        raise WritebackError(
            f"a deleted-block filler needs at least {_DELETED_BLOCK_HEADER} bytes, got {size}"
        )
    return (
        b"\x00"
        + size.to_bytes(3, "big")
        + struct.pack(">Q", next_pos)
        + b"\x00" * (size - _DELETED_BLOCK_HEADER)
    )


def _terminating(is_head: bool, payload: bytes, span: int) -> tuple[bytes, int] | None:
    """A whole-record (type 3) or last (type 9) block, and the bytes it frees.

    Returns `(block, freed)`, where `block` starts at the original block's
    offset and `freed` counts the trailing bytes of `span` that `block` does
    not cover. A record that shrinks a lot can leave more slack than the
    single pad byte can record (over `_MAX_PAD`); rather than stretch that
    field past what it holds, the excess is left for the caller to turn into
    a deleted block — MyISAM's own answer to a record shrinking in place
    without moving its head block (and therefore its index entry).
    `pad > _MAX_PAD` already guarantees more than `_DELETED_BLOCK_HEADER`
    bytes to split off, since `_MAX_PAD > _DELETED_BLOCK_HEADER`.

    The freed span is reported rather than filled in here because a deleted
    block cannot be written in isolation: its next-pointer is a link in a
    file-wide chain only `_apply_free_blocks` can see.
    """
    pad = span - 4 - len(payload)
    if pad < 0:
        return None
    kind = 3 if is_head else 9
    if pad <= _MAX_PAD:
        block = bytes([kind]) + struct.pack(">H", len(payload)) + bytes([pad]) + payload
        return block + b"\x00" * pad, 0
    target_pad = min(_MAX_PAD, pad - _DELETED_BLOCK_HEADER)
    block = bytes([kind]) + struct.pack(">H", len(payload)) + bytes([target_pad]) + payload
    return block + b"\x00" * target_pad, pad - target_pad


def _continuing(is_head: bool, chunk: bytes, row_len: int, next_pos: int) -> bytes:
    """A first (type 5) or middle (type 11) block, pointing at the next one."""
    if is_head:
        return (
            b"\x05"
            + struct.pack(">H", row_len)
            + struct.pack(">H", len(chunk))
            + struct.pack(">Q", next_pos)
            + chunk
        )
    return b"\x0b" + struct.pack(">H", len(chunk)) + struct.pack(">Q", next_pos) + chunk


def _pack_chain(
    chain: list, row: bytes, eof: int
) -> tuple[list[tuple[int, bytes]], bytes, list[tuple[int, int]]]:
    """Lay `row` into the chain's existing blocks, at their existing offsets.

    Every block is refilled to exactly the span it already occupied — that is
    what keeps the following blocks, and therefore every index entry, valid.
    Returns the in-place patches, any bytes still needing a new block at end
    of file, and the `(pos, size)` spans this record gave back, for
    `_apply_free_blocks` to turn into properly chained deleted blocks.
    """
    patches: list[tuple[int, bytes]] = []
    freed: list[tuple[int, int]] = []
    remaining = row

    for i, block in enumerate(chain):
        span = block.header_len + block.data_len + (block.pad or 0)
        is_head = i == 0

        done = _terminating(is_head, remaining, span)
        if done is not None:
            terminator, over = done
            patches.append((block.pos, terminator))
            if over:
                freed.append((block.pos + len(terminator), over))
            # The row fit before the chain's original end. Any blocks that
            # used to continue it are no longer reachable from anywhere —
            # not from this now-terminating block, and not from the free
            # list — so they'd sit in the file as unlinked garbage forever.
            # Give each one back as free space instead of leaving it
            # dangling.
            for leftover in chain[i + 1 :]:
                freed.append(
                    (leftover.pos, leftover.header_len + leftover.data_len + (leftover.pad or 0))
                )
            return patches, b"", freed

        header = 13 if is_head else 11
        take = span - header
        if take <= 0:
            raise WritebackError(f"block at {block.pos} spans {span} bytes, too small to re-chain")
        chunk, remaining = remaining[:take], remaining[take:]
        next_pos = chain[i + 1].pos if i + 1 < len(chain) else eof
        patches.append((block.pos, _continuing(is_head, chunk, len(row), next_pos)))

    if not remaining:
        # Every block was refilled as a continuation but nothing is left to
        # terminate the chain — the last one would dangle.
        raise WritebackError("chain ended without a terminating block")
    return patches, remaining, freed


def _apply_free_blocks(data: bytearray, myi: bytearray, freed: list[tuple[int, int]]) -> None:
    """Turn reclaimed spans into deleted blocks, and record them in the `.MYI`.

    A deleted block is an entry in a file-wide ledger, not just a header:
    MyISAM threads the blocks together through their own next-pointers from
    `dellink`, and the state header counts them (`del`) and their bytes
    (`empty`). Writing the block without the ledger leaves a file that reads
    back record-for-record correct while EndNote treats the table as
    damaged — see the module docstring.

    New blocks are pushed onto the head of the chain, which is what MyISAM
    itself does: each points at whatever `dellink` held, then becomes the
    new `dellink`.
    """
    if not freed:
        return
    head = struct.unpack_from(">Q", myi, _MYI_DELLINK)[0]
    count = struct.unpack_from(">Q", myi, _MYI_DEL)[0]
    empty = struct.unpack_from(">Q", myi, _MYI_EMPTY)[0]
    for pos, size in freed:
        data[pos : pos + size] = _deleted_block(size, head)
        head, count, empty = pos, count + 1, empty + size
    struct.pack_into(">Q", myi, _MYI_DELLINK, head)
    struct.pack_into(">Q", myi, _MYI_DEL, count)
    struct.pack_into(">Q", myi, _MYI_EMPTY, empty)


def _verify_free_space(new_data: bytes, myi: bytes, where: str) -> None:
    """Insist the `.MYI`'s free-space ledger matches the blocks really there.

    `_verify` reads every record back and compares it field by field, and a
    file can pass that while still being wrong at this level: the records
    are all intact, but `del`/`empty`/`dellink` describe a different set of
    deleted blocks than the file holds. EndNote reacts to that by treating
    the table as damaged — observed on a real library as column sorting
    silently ceasing to work — and a free chain that reaches a live record's
    head block is worse than that, since MyISAM would hand that space out
    for reuse and overwrite a real record.

    Run against the original file as well as the rewritten one, so a library
    that arrives inconsistent is reported as such instead of being blamed on
    this write.
    """
    blocks = _scan(new_data)
    by_pos = {b.pos: b for b in blocks}
    deleted = [b for b in blocks if b.deleted]

    count = struct.unpack_from(">Q", myi, _MYI_DEL)[0]
    if count != len(deleted):
        raise WritebackError(
            f"{where}: the index counts {count} deleted blocks, the data file holds {len(deleted)}"
        )
    empty = struct.unpack_from(">Q", myi, _MYI_EMPTY)[0]
    total = sum(b.span for b in deleted)
    if empty != total:
        raise WritebackError(
            f"{where}: the index counts {empty} free bytes, the data file holds {total}"
        )

    seen: set[int] = set()
    pos = struct.unpack_from(">Q", myi, _MYI_DELLINK)[0]
    while pos != _FREE_LIST_END:
        block = by_pos.get(pos)
        if block is None:
            raise WritebackError(
                f"{where}: the free chain reaches {pos}, which is not a block boundary"
            )
        if not block.deleted:
            raise WritebackError(
                f"{where}: the free chain reaches {pos}, a live record's block — MyISAM "
                f"would hand that space out for reuse"
            )
        if pos in seen:
            raise WritebackError(f"{where}: the free chain loops back to {pos}")
        seen.add(pos)
        pos = block.next_pos
    if len(seen) != len(deleted):
        raise WritebackError(
            f"{where}: {len(deleted)} deleted blocks exist but the free chain reaches "
            f"{len(seen)} of them"
        )


def _new_tail_block(payload: bytes) -> bytes:
    """A type-9 terminating block for end of file: 4-byte header, padded to
    MyISAM's alignment and minimum block length."""
    total = max(_MIN_BLOCK, 4 + len(payload))
    total += (-total) % _ALIGN
    pad = total - 4 - len(payload)
    if pad > _MAX_PAD:
        raise WritebackError("tail block needs more padding than a byte can hold")
    return b"\x09" + struct.pack(">H", len(payload)) + bytes([pad]) + payload + b"\x00" * pad


# --------------------------------------------------------------------------
# the write
# --------------------------------------------------------------------------


def set_column(
    rdb_dir: str | Path,
    enl_path: str | Path,
    updates: dict[int, str],
    column: str = "custom_1",
) -> dict:
    """Set `column` on the given record numbers, in place.

    `updates` maps record number to the text to store; an empty string clears
    the column. Raises before touching anything if the result would not read
    back exactly as intended. A single-column convenience over `set_fields`.
    """
    if column not in WRITABLE_COLUMNS:
        raise WritebackError(
            f"{column!r} is not on the safe-to-write list "
            f"({', '.join(sorted(WRITABLE_COLUMNS))}). Other columns feed EndNote's "
            f"indexes, term lists or sort caches and need their own analysis."
        )
    result = _write_rows(rdb_dir, enl_path, {rec: {column: text} for rec, text in updates.items()})
    return {**result, "column": column}


def set_fields(
    rdb_dir: str | Path,
    enl_path: str | Path,
    updates: dict[int, dict[str, str]],
) -> dict:
    """Set several columns at once on the given record numbers, in place.

    `updates` maps record number to a `{column: text}` mapping; an empty
    string clears that column. Every record is rewritten once regardless of
    how many of its columns changed. Raises before touching anything if any
    column is outside `WRITABLE_COLUMNS` or the result would not read back
    exactly as intended.
    """
    bad_columns = sorted(
        {column for fields in updates.values() for column in fields} - WRITABLE_COLUMNS
    )
    if bad_columns:
        raise WritebackError(
            f"{bad_columns} not on the safe-to-write list "
            f"({', '.join(sorted(WRITABLE_COLUMNS))}). Other columns feed EndNote's "
            f"indexes, term lists or sort caches and need their own analysis."
        )
    result = _write_rows(rdb_dir, enl_path, updates)
    columns = sorted({column for fields in updates.values() for column in fields})
    return {**result, "columns": columns}


def repair_free_space(
    rdb_dir: str | Path,
    enl_path: str | Path,
    dry_run: bool = True,
) -> dict:
    """Rebuild the free-space ledger from the blocks actually in `refs.MYD`.

    For a library an earlier version of this module left inconsistent: the
    deleted blocks are all there and every record still reads back
    correctly, but `del`/`empty`/`dellink` do not describe them and their
    next-pointers were written as zero instead of being chained. EndNote
    treats that as a damaged table; the symptom seen in the wild was column
    sorting silently ceasing to work. See the module docstring.

    Only the ledger changes. Deleted blocks keep the positions and sizes
    they already have, no record is moved or rewritten, and the repair is
    refused unless every record in the result is byte-identical to before.
    A library whose ledger is already correct is reported as needing
    nothing. Defaults to a dry-run report; a real run backs the whole
    library up first, exactly like a write.
    """
    rdb_dir, enl_path = Path(rdb_dir), Path(enl_path)
    myd_path, myi_path = rdb_dir / "refs.MYD", rdb_dir / "refs.MYI"
    _ensure_unlocked([enl_path, myd_path, myi_path])

    original = myd_path.read_bytes()
    myi = bytearray(myi_path.read_bytes())
    deleted = [b for b in _scan(original) if b.deleted]

    try:
        _verify_free_space(original, bytes(myi), "the library as found")
    except WritebackError as reason:
        already_consistent = False
        diagnosis = str(reason)
    else:
        already_consistent = True
        diagnosis = ""

    report = {
        "deleted_blocks": len(deleted),
        "free_bytes": sum(b.span for b in deleted),
        "was_consistent": already_consistent,
        "diagnosis": diagnosis,
        "dry_run": dry_run,
    }
    if already_consistent:
        return {**report, "repaired": False}

    too_small = [b for b in deleted if b.span < _DELETED_BLOCK_HEADER]
    if too_small:
        raise WritebackError(
            f"deleted blocks at {[b.pos for b in too_small]} are smaller than a "
            f"{_DELETED_BLOCK_HEADER}-byte free-chain header; this is not the damage "
            f"this repair understands"
        )

    # Chain them in file order — MyISAM does not care which order the free
    # list runs in, and a deterministic one keeps the repair reproducible.
    data = bytearray(original)
    ordered = sorted(deleted, key=lambda b: b.pos)
    for i, block in enumerate(ordered):
        nxt = ordered[i + 1].pos if i + 1 < len(ordered) else _FREE_LIST_END
        data[block.pos : block.pos + block.span] = _deleted_block(block.span, nxt)
    struct.pack_into(">Q", myi, _MYI_DELLINK, ordered[0].pos if ordered else _FREE_LIST_END)
    struct.pack_into(">Q", myi, _MYI_DEL, len(ordered))
    struct.pack_into(">Q", myi, _MYI_EMPTY, sum(b.span for b in ordered))

    # Nothing above should have touched a record; insist on it.
    _verify(bytes(data), original, {})
    _verify_free_space(bytes(data), bytes(myi), "after this repair")

    if dry_run:
        changed = sum(1 for a, b in zip(original, data, strict=True) if a != b)
        return {**report, "repaired": False, "would_rewrite_bytes": changed}

    backup_path = backup_library(rdb_dir, enl_path)
    staged_enl = _stage_enl(enl_path, bytes(data), rdb_dir / "refs.frm")
    try:
        myd_path.write_bytes(bytes(data))
        myi_path.write_bytes(bytes(myi))
        os.replace(staged_enl, enl_path)
    finally:
        staged_enl.unlink(missing_ok=True)

    return {**report, "repaired": True, "backup_path": str(backup_path)}


def _ensure_unlocked(paths: list[Path]) -> None:
    """Best-effort check that no other process has these files open.

    EndNote holding the library open is the case this exists for: writing to
    `refs.MYD`/`refs.MYI` out from under a running EndNote risks a sharing
    conflict, and — worse than an outright failure — EndNote's own in-memory
    copy could later overwrite this write with stale data. Opening for
    read+write is a *best effort* signal, not a guarantee: a process that
    opened with share-read/share-write (common for memory-mapped access)
    would not be caught here. Close EndNote before committing regardless of
    what this check says.
    """
    locked = []
    for path in paths:
        try:
            with open(path, "r+b"):
                pass
        except OSError:
            locked.append(str(path))
    if locked:
        raise WritebackError(
            "these library files appear to be open by another process — close EndNote (or "
            "whatever else has the library open) and try again: " + ", ".join(locked)
        )


def backup_library(rdb_dir: str | Path, enl_path: str | Path) -> Path:
    """Copy the live library into a fresh timestamped folder before any write.

    Copies the whole `rdb` directory (so `refs.MYD`/`refs.MYI` stay together
    with `misc`, `pdf_index`, `terms`/`jterms` and `csort`) plus the `.enl`
    mirror. Never reads back or modifies the live files. The destination is
    always new — this never overwrites a previous backup. Refuses (see
    `_ensure_unlocked`) if the library looks like it is still open elsewhere.
    """
    rdb_dir, enl_path = Path(rdb_dir), Path(enl_path)
    _ensure_unlocked([enl_path, rdb_dir / "refs.MYD", rdb_dir / "refs.MYI"])
    backups_dir = enl_path.resolve().parent / "backups"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    dest = backups_dir / f"{enl_path.stem}-{stamp}"
    for suffix in range(1000):
        try:
            dest.mkdir(parents=True, exist_ok=False)
            break
        except FileExistsError:
            dest = backups_dir / f"{enl_path.stem}-{stamp}-{suffix}"
    else:
        raise WritebackError(f"could not find a free backup directory under {backups_dir}")
    shutil.copy2(enl_path, dest / enl_path.name)
    shutil.copytree(rdb_dir, dest / "rdb")
    return dest


def _write_rows(
    rdb_dir: str | Path,
    enl_path: str | Path,
    updates: dict[int, dict[str, str]],
) -> dict:
    """The shared engine behind `set_column` and `set_fields`.

    Callers must already have checked every column against
    `WRITABLE_COLUMNS`; this only handles the block-level rewrite.
    """
    rdb_dir, enl_path = Path(rdb_dir), Path(enl_path)
    myd_path, myi_path = rdb_dir / "refs.MYD", rdb_dir / "refs.MYI"
    _ensure_unlocked([enl_path, myd_path, myi_path])

    original = myd_path.read_bytes()
    myi = bytearray(myi_path.read_bytes())
    # Before anything else: if the library's own free-space ledger is
    # already inconsistent, this write is not the thing to blame and not the
    # thing to layer on top of.
    _verify_free_space(original, bytes(myi), "the library as found")

    blocks = _scan(original)
    by_pos = {block.pos: block for block in blocks}

    heads = {}
    for block in blocks:
        if block.deleted or not block.first:
            continue
        chain = _chain(by_pos, block)
        record = b"".join(myisam._body(original, b) for b in chain)[: block.rec_len]
        heads[decode_row(record)["id"]] = (chain, record)

    missing = sorted(set(updates) - set(heads))
    if missing:
        raise WritebackError(f"no such records: {missing[:10]}")

    data = bytearray(original)
    appended = bytearray()
    freed: list[tuple[int, int]] = []
    written = 0

    for rec_number, fields in updates.items():
        chain, record = heads[rec_number]
        row = decode_row(record)
        for column, text in fields.items():
            row[column] = text.encode("utf-8")
        new_row = encode_row(row)

        eof = len(original) + len(appended)
        patches, overflow, gave_back = _pack_chain(chain, new_row, eof)
        for pos, payload in patches:
            data[pos : pos + len(payload)] = payload
        freed.extend(gave_back)
        if overflow:
            appended += _new_tail_block(overflow)
        written += 1

    data += appended

    # State counters. Everything else in the header is left alone.
    _apply_free_blocks(data, myi, freed)
    struct.pack_into(">Q", myi, _MYI_SPLIT, len(_scan(bytes(data))))
    struct.pack_into(">Q", myi, _MYI_DATA_FILE_LENGTH, len(data))

    _verify(bytes(data), original, updates)
    _verify_free_space(bytes(data), bytes(myi), "after this write")

    # Build and check the `.enl` mirror while the library is still untouched.
    # A repack that was going to fail — unreadable archive, missing
    # `refs.frm` — then fails with nothing written, instead of leaving the
    # `rdb` tables updated and the mirror stale, which is a divergence only
    # the backup can undo.
    staged_enl = _stage_enl(enl_path, bytes(data), rdb_dir / "refs.frm")
    try:
        myd_path.write_bytes(bytes(data))
        myi_path.write_bytes(bytes(myi))
        os.replace(staged_enl, enl_path)
    finally:
        staged_enl.unlink(missing_ok=True)

    return {
        "records_written": written,
        "bytes_appended": len(appended),
        "data_file_length": len(data),
    }


def _verify(new_data: bytes, original: bytes, updates: dict[int, dict[str, str]]) -> None:
    """Re-read the rewritten file and insist it says exactly what it should."""

    def rows(data: bytes) -> dict:
        out = {}
        blocks = _scan(data)
        by_pos = {b.pos: b for b in blocks}
        for block in blocks:
            if block.deleted or not block.first:
                continue
            chain = _chain(by_pos, block)
            record = b"".join(myisam._body(data, b) for b in chain)[: block.rec_len]
            out[decode_row(record)["id"]] = decode_row(record)
        return out

    before, after = rows(original), rows(new_data)
    if set(before) != set(after):
        raise WritebackError("record set changed during write")

    for rec_number, row in after.items():
        expected = before[rec_number]
        if rec_number in updates:
            row, expected = dict(row), dict(expected)
            for column, text in updates[rec_number].items():
                want = text.encode("utf-8")
                got = row.get(column, b"")
                if got != want:
                    raise WritebackError(
                        f"record {rec_number}: {column} reads back {got!r}, expected {want!r}"
                    )
                if want:
                    expected[column] = want
                else:
                    row.pop(column, None)
                    expected.pop(column, None)
        if row != expected:
            changed = [k for k in set(row) | set(expected) if row.get(k) != expected.get(k)]
            raise WritebackError(f"record {rec_number} changed unexpectedly in {changed}")


def _stage_enl(enl_path: Path, refs_myd: bytes, frm_path: Path) -> Path:
    """Build the replacement `.enl` mirror beside the original, and verify it.

    Every member of the archive is carried across and only `refs.MYD` is
    substituted. This library's `.enl` was observed to hold just `refs.MYD`
    and `refs.frm` — but that is one library, inspected once, and dropping a
    member we failed to anticipate would be both silent and unrecoverable.
    Copying what we do not understand costs nothing.

    MyISAM's files are re-stored uncompressed, which is how EndNote writes
    them and what the read path expects.
    """
    with zipfile.ZipFile(enl_path) as source:
        members = [(info, source.read(info.filename)) for info in source.infolist()]
    if not any(info.filename == "refs.frm" for info, _ in members):
        members.append((zipfile.ZipInfo("refs.frm"), frm_path.read_bytes()))
    if not any(info.filename == "refs.MYD" for info, _ in members):
        members.append((zipfile.ZipInfo("refs.MYD"), b""))

    staged = enl_path.with_suffix(".enl.writeback-tmp")
    try:
        with zipfile.ZipFile(staged, "w", zipfile.ZIP_STORED) as out:
            for info, payload in members:
                # Reuse the member's own header so timestamps and permissions
                # survive a round trip.
                target = zipfile.ZipInfo(info.filename, date_time=info.date_time)
                target.compress_type = zipfile.ZIP_STORED
                target.external_attr = info.external_attr
                target.internal_attr = info.internal_attr
                target.create_system = info.create_system
                out.writestr(target, refs_myd if info.filename == "refs.MYD" else payload)

        expected = sorted(info.filename for info, _ in members)
        with zipfile.ZipFile(staged) as check:
            if sorted(check.namelist()) != expected:
                raise WritebackError(
                    f"repacked {enl_path.name} holds {sorted(check.namelist())}, "
                    f"expected {expected}"
                )
            if check.read("refs.MYD") != refs_myd:
                raise WritebackError(f"repacked {enl_path.name} does not mirror the new refs.MYD")
    except Exception:
        staged.unlink(missing_ok=True)
        raise
    return staged
