"""Read MyISAM dynamic-format data files.

EndNote X9 stores its library as a MySQL MyISAM database: the `.enl` file is
a ZIP of `refs.MYD`/`refs.frm`, and `<Library>.Data/rdb/` holds the live,
uncompressed tables (`refs`, `refs_ext`, `misc`, `pdf_index`, `terms`,
`csort`, `jterms`). That is a documented storage engine, not a proprietary
blob — which is why the group tree, invisible in EndNote's XML export, can be
read from `misc.MYD`.

Only the reading half of the dynamic record format is implemented, and only
what is needed to walk a `.MYD` end to end and hand back whole records.

The block-type table below was reverse-engineered against a real X9 library
and is self-validating in a useful way: if any type or length were wrong, the
walk would land mid-block and hit an impossible type long before EOF. On the
reference library all four tables walk to exactly EOF and `refs`/`refs_ext`
each yield 1561 records — the same count as the XML export.

Nothing here writes. EndNote keeps derived index files (`.MYI`, plus the
`terms`/`csort` tables) that a writer would have to keep consistent, and
getting that wrong corrupts the library.
"""

from __future__ import annotations

import struct
from pathlib import Path

# MyISAM index-file magic, and the option bit that marks dynamic rows.
MYI_MAGIC = b"\xfe\xfe\x07\x01"
HA_OPTION_PACK_RECORD = 0x0001

_MAX_CONTINUATIONS = 4096


def _u2(b: bytes, o: int) -> int:
    return (b[o] << 8) | b[o + 1]


def _u3(b: bytes, o: int) -> int:
    return (b[o] << 16) | (b[o + 1] << 8) | b[o + 2]


def _u8(b: bytes, o: int) -> int:
    return struct.unpack_from(">Q", b, o)[0]


class MyISAMError(RuntimeError):
    pass


class Block:
    __slots__ = (
        "data_len",
        "deleted",
        "first",
        "header_len",
        "last",
        "next_pos",
        "pad",
        "pos",
        "rec_len",
        "type",
    )

    def __init__(self, **kw):
        for slot in self.__slots__:
            setattr(self, slot, kw.get(slot))

    @property
    def span(self) -> int:
        """Bytes this block occupies, including its header and any padding."""
        if self.deleted:
            return self.data_len
        return self.header_len + self.data_len + (self.pad or 0)


def _read_block(data: bytes, pos: int) -> Block:
    """Decode one block header.

    The 14 types form a regular family — whole record / first / last / middle,
    each in a 2-byte and a 3-byte length variant, and the "whole" and "last"
    kinds additionally in a padded variant:

        whole   1 (len2)   2 (len3)   3 (len2+pad)   4 (len3+pad)
        first   5 (len2)   6 (len3)
        last    7 (len2)   8 (len3)   9 (len2+pad)  10 (len3+pad)
        middle 11 (len2)  12 (len3)  13 (len2)
        deleted 0

    "first" and "middle" blocks carry an 8-byte file offset pointing at the
    rest of the record; "first" also carries the record's total length.
    """
    t = data[pos]

    if t == 0:
        return Block(
            type=t,
            pos=pos,
            header_len=0,
            data_len=_u3(data, pos + 1),
            rec_len=0,
            next_pos=_u8(data, pos + 4),
            deleted=True,
            first=False,
            last=False,
        )

    if t in (1, 2, 7, 8):
        wide = t in (2, 8)
        n = _u3(data, pos + 1) if wide else _u2(data, pos + 1)
        return Block(
            type=t,
            pos=pos,
            header_len=4 if wide else 3,
            data_len=n,
            rec_len=n,
            next_pos=None,
            deleted=False,
            first=t in (1, 2),
            last=True,
        )

    if t in (3, 4, 9, 10):
        wide = t in (4, 10)
        n = _u3(data, pos + 1) if wide else _u2(data, pos + 1)
        header_len = 5 if wide else 4
        return Block(
            type=t,
            pos=pos,
            header_len=header_len,
            data_len=n,
            rec_len=n,
            pad=data[pos + header_len - 1],
            next_pos=None,
            deleted=False,
            first=t in (3, 4),
            last=True,
        )

    if t in (5, 6):
        wide = t == 6
        if wide:
            return Block(
                type=t,
                pos=pos,
                header_len=15,
                rec_len=_u3(data, pos + 1),
                data_len=_u3(data, pos + 4),
                next_pos=_u8(data, pos + 7),
                deleted=False,
                first=True,
                last=False,
            )
        return Block(
            type=t,
            pos=pos,
            header_len=13,
            rec_len=_u2(data, pos + 1),
            data_len=_u2(data, pos + 3),
            next_pos=_u8(data, pos + 5),
            deleted=False,
            first=True,
            last=False,
        )

    if t in (11, 13):
        n = _u2(data, pos + 1)
        return Block(
            type=t,
            pos=pos,
            header_len=11,
            data_len=n,
            rec_len=n,
            next_pos=_u8(data, pos + 3),
            deleted=False,
            first=False,
            last=False,
        )

    if t == 12:
        n = _u3(data, pos + 1)
        return Block(
            type=t,
            pos=pos,
            header_len=12,
            data_len=n,
            rec_len=n,
            next_pos=_u8(data, pos + 4),
            deleted=False,
            first=False,
            last=False,
        )

    raise MyISAMError(f"unknown MyISAM block type {t} at offset 0x{pos:x}")


def read_records(myd_path: str | Path) -> list[bytes]:
    """Return the raw bytes of every live record in a `.MYD` file.

    Reading is done in one shot: EndNote may have the library open, and a
    single read is the closest thing to a consistent snapshot available
    without asking the user to close the application.
    """
    data = Path(myd_path).read_bytes()

    blocks: dict[int, Block] = {}
    pos = 0
    while pos < len(data):
        block = _read_block(data, pos)
        blocks[pos] = block
        step = max(block.span, 1)
        pos += step
    if pos != len(data):
        raise MyISAMError(f"block walk overran {myd_path}: stopped at {pos}, file is {len(data)}")

    out = []
    for start in sorted(blocks):
        block = blocks[start]
        if block.deleted or not block.first:
            continue
        body = bytearray(_body(data, block))
        nxt, hops = block.next_pos, 0
        while nxt and len(body) < block.rec_len and hops < _MAX_CONTINUATIONS:
            cont = blocks.get(nxt)
            if cont is None:
                break
            body += _body(data, cont)
            nxt, hops = cont.next_pos, hops + 1
        out.append(bytes(body[: block.rec_len]))
    return out


def _body(data: bytes, block: Block) -> bytes:
    start = block.pos + block.header_len
    return data[start : start + block.data_len]
