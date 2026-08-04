"""Read EndNote's group tree out of the library's `misc` table.

Groups are the one thing EndNote's XML export leaves out entirely — a
1561-record export contains no group element at all — which is why the rest
of this package, built on the export, could not see them. They are, however,
sitting in `<Library>.Data/rdb/misc.MYD`, a MyISAM key/value table whose rows
are `(code, subcode, code_name, subcode_name, value)`:

    code 15  GroupSpec     XML: the group's id, name, and rules
    code 16  GroupRecList  binary: the record numbers in that group
    code 17  GroupsetSpec  XML: a group set (the folders in the sidebar)
    code 18  GroupsetList  XML: the order of the sets

A `GroupSpec` and its `GroupRecList` share a subcode, so membership joins on
that — no id resolution needed. `<rules>` distinguishes the two kinds: a
plain group carries `TYPE;3`, a smart group carries `TYPE;4` plus the saved
`SEARCH` query.

Everything here is read-only; see myisam.py for why writing is not offered.
"""

from __future__ import annotations

import html
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path

from . import myisam

# `misc` row codes, named after the code_name EndNote stores alongside them.
CODE_GROUP_SPEC = 15
CODE_GROUP_REC_LIST = 16
CODE_GROUPSET_SPEC = 17

_NAME_RE = re.compile(r"<name>(.*?)</name>", re.S)
_ID_RE = re.compile(r"<id>(.*?)</id>", re.S)
_RULE_RE = re.compile(r"<rule>(.*?)</rule>", re.S)
_MEMBER_RE = re.compile(r"<member>(.*?)</member>", re.S)
_FLAT_QUERY_RE = re.compile(r"<FlatQuery>(.*?)</FlatQuery>", re.S)

# A GroupRecList value is a 4-byte format version (big-endian) followed by a
# 4-byte member count and that many record numbers, both little-endian.
_REC_LIST_HEADER = 8


@dataclass
class Group:
    name: str
    uuid: str = ""
    smart: bool = False
    query: str = ""
    rec_numbers: list[int] = field(default_factory=list)
    group_set: str = ""

    @property
    def size(self) -> int:
        return len(self.rec_numbers)


def rdb_dir(library_path: str | Path) -> Path:
    """`…/My Library.enl` -> `…/My Library.Data/rdb`."""
    return Path(library_path).with_suffix(".Data") / "rdb"


def _decode_row(record: bytes) -> tuple[list[int], str, bytes] | None:
    """Split one `misc` row into (ints, code_name, value).

    MyISAM packs a dynamic row as a flag byte, then the fields, with zero
    integers omitted — so the number of leading 4-byte integers varies per
    row. Rather than decode the packing bitmap, try each arity and keep the
    one whose fields consume the record exactly; a wrong guess essentially
    never lands on the right total length with a printable name.
    """
    for int_count in (2, 1, 0):
        pos = 1 + 4 * int_count
        if pos >= len(record):
            continue
        name_len = record[pos]
        pos += 1
        name = record[pos : pos + name_len]
        pos += name_len
        if pos + 3 > len(record):
            continue
        value_len = int.from_bytes(record[pos : pos + 3], "little")
        pos += 3
        if pos + value_len != len(record):
            continue
        try:
            code_name = name.decode("ascii")
        except UnicodeDecodeError:
            continue
        if not code_name.isprintable() or not code_name:
            continue
        ints = [int.from_bytes(record[1 + 4 * i : 5 + 4 * i], "little") for i in range(int_count)]
        return ints, code_name, record[pos : pos + value_len]
    return None


def _member_numbers(value: bytes) -> list[int]:
    if len(value) < _REC_LIST_HEADER:
        return []
    count = struct.unpack_from("<I", value, 4)[0]
    available = (len(value) - _REC_LIST_HEADER) // 4
    count = min(count, available)
    return list(struct.unpack_from(f"<{count}I", value, _REC_LIST_HEADER))


def _text(pattern: re.Pattern, xml: str) -> str:
    match = pattern.search(xml)
    return html.unescape(match.group(1)).strip() if match else ""


def read_groups(rdb_path: str | Path) -> list[Group]:
    """Read every group, with its members, from a library's `rdb` folder.

    Raises FileNotFoundError if the folder has no `misc` table, and
    myisam.MyISAMError if the file cannot be walked (most likely because
    EndNote was mid-write — retry with EndNote closed).
    """
    rdb_path = Path(rdb_path)
    misc = rdb_path / "misc.MYD"
    if not misc.exists():
        raise FileNotFoundError(f"No misc.MYD under {rdb_path} — is that a library's rdb folder?")

    specs: dict[int, str] = {}
    rec_lists: dict[int, bytes] = {}
    set_specs: list[str] = []

    for record in myisam.read_records(misc):
        decoded = _decode_row(record)
        if decoded is None:
            continue
        ints, code_name, value = decoded
        if code_name == "GroupSpec" and len(ints) == 2:
            specs[ints[1]] = value.decode("utf-8", "replace")
        elif code_name == "GroupRecList" and len(ints) == 2:
            rec_lists[ints[1]] = value
        elif code_name == "GroupsetSpec":
            set_specs.append(value.decode("utf-8", "replace"))

    set_names = _group_set_names(set_specs)

    groups = []
    for subcode in sorted(specs):
        xml = specs[subcode]
        uuid = _text(_ID_RE, xml)
        rules = _RULE_RE.findall(xml)
        smart = any("SEARCH" in rule for rule in rules)
        groups.append(
            Group(
                name=_text(_NAME_RE, xml) or f"(unnamed {subcode})",
                uuid=uuid,
                smart=smart,
                query=html.unescape(_text(_FLAT_QUERY_RE, html.unescape(xml))),
                rec_numbers=_member_numbers(rec_lists.get(subcode, b"")),
                group_set=set_names.get(uuid, ""),
            )
        )
    return groups


def _group_set_names(set_specs: list[str]) -> dict[str, str]:
    """Map each group's uuid onto the name of the set that contains it."""
    owner: dict[str, str] = {}
    for xml in set_specs:
        set_name = _text(_NAME_RE, xml)
        for member in _MEMBER_RE.findall(xml):
            owner[html.unescape(member).strip()] = set_name
    return owner
