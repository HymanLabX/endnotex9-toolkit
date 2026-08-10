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

Every write verifies itself: the resulting file is walked block by block,
every record is decoded again, the targets must hold exactly the intended
value, and every other record must be byte-identical to before. A failure
raises before anything is copied over the library.
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

# Offsets into the .MYI state header, located by matching known values.
_MYI_SPLIT = 44
_MYI_DATA_FILE_LENGTH = 68


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


def _deleted_block(size: int) -> bytes:
    """A type-0 (deleted) block spanning exactly `size` bytes.

    MyISAM threads these into a reusable-space chain via the next-pointer;
    this package only ever rewrites existing records, never inserts new
    ones, so there is nothing to reuse the space for and the pointer is left
    as a sentinel zero rather than spliced into that chain. A decoder still
    walks straight over it — `read_records` skips anything `deleted` — so
    the only cost is that MyISAM's own inserts will not reclaim this span.
    """
    if size < _DELETED_BLOCK_HEADER:
        raise WritebackError(f"a deleted-block filler needs at least {_DELETED_BLOCK_HEADER} bytes, got {size}")
    return b"\x00" + size.to_bytes(3, "big") + b"\x00" * 8 + b"\x00" * (size - _DELETED_BLOCK_HEADER)


def _terminating(is_head: bool, payload: bytes, span: int) -> bytes | None:
    """A whole-record (type 3) or last (type 9) block filling `span` exactly.

    A record that shrinks a lot can leave more slack than the single pad
    byte can record (over `_MAX_PAD`). Rather than stretch that field past
    what it holds, the excess becomes a separate deleted-block record right
    after a normally-padded terminator — MyISAM's own answer to a record
    shrinking in place without moving its head block (and therefore its
    index entry). `pad > _MAX_PAD` already guarantees more than
    `_DELETED_BLOCK_HEADER` bytes of slack to split off, since
    `_MAX_PAD > _DELETED_BLOCK_HEADER`.
    """
    pad = span - 4 - len(payload)
    if pad < 0:
        return None
    kind = 3 if is_head else 9
    if pad <= _MAX_PAD:
        return bytes([kind]) + struct.pack(">H", len(payload)) + bytes([pad]) + payload + b"\x00" * pad
    target_pad = min(_MAX_PAD, pad - _DELETED_BLOCK_HEADER)
    leftover = pad - target_pad
    head = bytes([kind]) + struct.pack(">H", len(payload)) + bytes([target_pad]) + payload + b"\x00" * target_pad
    return head + _deleted_block(leftover)


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


def _pack_chain(chain: list, row: bytes, eof: int) -> tuple[list[tuple[int, bytes]], bytes]:
    """Lay `row` into the chain's existing blocks, at their existing offsets.

    Every block is refilled to exactly the span it already occupied — that is
    what keeps the following blocks, and therefore every index entry, valid.
    Returns the in-place patches plus any bytes still needing a new block at
    end of file.
    """
    patches: list[tuple[int, bytes]] = []
    remaining = row

    for i, block in enumerate(chain):
        span = block.header_len + block.data_len + (block.pad or 0)
        is_head = i == 0

        done = _terminating(is_head, remaining, span)
        if done is not None:
            patches.append((block.pos, done))
            # The row fit before the chain's original end. Any blocks that
            # used to continue it are no longer reachable from anywhere —
            # not from this now-terminating block, and not from the free
            # list — so they'd sit in the file as unlinked garbage forever.
            # Reclaim each one as its own deleted block instead of leaving
            # it dangling.
            for leftover in chain[i + 1 :]:
                leftover_span = leftover.header_len + leftover.data_len + (leftover.pad or 0)
                patches.append((leftover.pos, _deleted_block(leftover_span)))
            return patches, b""

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
    return patches, remaining


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
    bad_columns = sorted({column for fields in updates.values() for column in fields} - WRITABLE_COLUMNS)
    if bad_columns:
        raise WritebackError(
            f"{bad_columns} not on the safe-to-write list "
            f"({', '.join(sorted(WRITABLE_COLUMNS))}). Other columns feed EndNote's "
            f"indexes, term lists or sort caches and need their own analysis."
        )
    result = _write_rows(rdb_dir, enl_path, updates)
    columns = sorted({column for fields in updates.values() for column in fields})
    return {**result, "columns": columns}


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
    written = 0

    for rec_number, fields in updates.items():
        chain, record = heads[rec_number]
        row = decode_row(record)
        for column, text in fields.items():
            row[column] = text.encode("utf-8")
        new_row = encode_row(row)

        eof = len(original) + len(appended)
        patches, overflow = _pack_chain(chain, new_row, eof)
        for pos, payload in patches:
            data[pos : pos + len(payload)] = payload
        if overflow:
            appended += _new_tail_block(overflow)
        written += 1

    data += appended
    _verify(bytes(data), original, updates)

    # State counters. Everything else in the header is left alone.
    myi = bytearray(myi_path.read_bytes())
    struct.pack_into(">Q", myi, _MYI_SPLIT, len(_scan(bytes(data))))
    struct.pack_into(">Q", myi, _MYI_DATA_FILE_LENGTH, len(data))

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
