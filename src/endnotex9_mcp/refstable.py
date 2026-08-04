"""The `refs` table: its row format, and reading references straight from it.

This is the read side of what writeback.py writes. Having it means the XML
export is no longer required to index a library — EndNote's own `refs.MYD`
can be read directly, so there is no "remember to re-export" step.

The row format was reverse-engineered and is verified against the XML export
on every load when one is available (see sources.py): all 1561 rows of the
reference library decode consuming exactly the record, with `title` and
`year` matching the export byte for byte.

Two fields the export has and this table does not:

* the journal abbreviation (`periodical/abbr-1` in the export) lives in the
  separate `jterms` journals term list, not on the record;
* the reference *type name*. Records carry only EndNote's internal type code,
  and the code-to-name table belongs to the application, not the library. The
  mapping is therefore learned from an XML export once and remembered — the
  one thing the export is still uniquely good for. Until then, callers receive
  the stable identifier ``endnote:type:<code>``.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

from . import myisam

# `refs` columns in storage order, read out of refs.frm. Everything except
# the two in FIXED_WIDTHS is a blob with a 3-byte little-endian length.
REFS_COLUMNS = (
    "id",
    "reference_type",
    "text_styles",
    "author",
    "year",
    "title",
    "pages",
    "secondary_title",
    "volume",
    "number",
    "number_of_volumes",
    "secondary_author",
    "place_published",
    "publisher",
    "subsidiary_author",
    "edition",
    "keywords",
    "type_of_work",
    "date",
    "abstract",
    "label",
    "url",
    "tertiary_title",
    "tertiary_author",
    "notes",
    "isbn",
    "custom_1",
    "custom_2",
    "custom_3",
    "custom_4",
    "alternate_title",
    "accession_number",
    "call_number",
    "short_title",
    "custom_5",
    "custom_6",
    "section",
    "original_publication",
    "reprint_edition",
    "reviewed_item",
    "author_address",
    "image",
    "caption",
    "custom_7",
    "electronic_resource_number",
    "link_to_pdf",
    "translated_author",
    "translated_title",
    "name_of_database",
    "database_provider",
    "research_notes",
    "language",
    "access_date",
    "last_modified_date",
)

# `reference_type` is two bytes, not four. It is only present on non-journal
# records (a Journal Article is internal type 0, which the bitmap omits), so a
# wrong width here shifts exactly those records and nothing else.
FIXED_WIDTHS = {"id": 4, "reference_type": 2}

# Bytes of packing bitmap ahead of the fields (base_info.pack_bits). Bit i,
# least-significant first, is SET when column i is absent.
PACK_BITS = 7

# EndNote separates repeated values inside one field with a carriage return.
VALUE_SEPARATOR = "\r"


class RowError(RuntimeError):
    pass


def decode_row(record: bytes) -> dict:
    bitmap = int.from_bytes(record[:PACK_BITS], "little")
    pos = PACK_BITS
    row: dict = {}
    for index, column in enumerate(REFS_COLUMNS):
        if bitmap >> index & 1:
            continue
        width = FIXED_WIDTHS.get(column)
        if width:
            row[column] = int.from_bytes(record[pos : pos + width], "little")
            pos += width
        else:
            length = int.from_bytes(record[pos : pos + 3], "little")
            pos += 3
            row[column] = record[pos : pos + length]
            pos += length
    if pos != len(record):
        raise RowError(f"row decode consumed {pos} of {len(record)} bytes")
    return row


def encode_row(row: dict) -> bytes:
    bitmap = 0
    body = bytearray()
    for index, column in enumerate(REFS_COLUMNS):
        value = row.get(column)
        present = value is not None and value != b"" and not (column in FIXED_WIDTHS and value == 0)
        if not present:
            bitmap |= 1 << index
            continue
        width = FIXED_WIDTHS.get(column)
        if width:
            body += int(value).to_bytes(width, "little")
        else:
            body += len(value).to_bytes(3, "little") + value
    return bitmap.to_bytes(PACK_BITS, "little") + bytes(body)


def read_rows(rdb_dir: str | Path) -> Iterator[dict]:
    """Every live row of `refs`, decoded."""
    myd = Path(rdb_dir) / "refs.MYD"
    if not myd.exists():
        raise FileNotFoundError(f"No refs.MYD under {rdb_dir}")
    for record in myisam.read_records(myd):
        yield decode_row(record)


# --------------------------------------------------------------------------
# journal abbreviations, from the `jterms` term list
# --------------------------------------------------------------------------


def read_journal_abbreviations(rdb_dir: str | Path) -> dict[str, str]:
    """Map full journal name -> its first abbreviation.

    `jterms` rows are `(list_id, list_id_2..4, term, abbr1..3)`. MyISAM omits
    zero integers, so the leading integer count varies; the same
    try-each-arity trick the `misc` table needs applies here.
    """
    myd = Path(rdb_dir) / "jterms.MYD"
    if not myd.exists():
        return {}

    out: dict[str, str] = {}
    for record in myisam.read_records(myd):
        strings = _trailing_strings(record)
        if len(strings) >= 2 and strings[0] and strings[1]:
            out.setdefault(strings[0], strings[1])
    return out


def _trailing_strings(record: bytes) -> list[str]:
    """Pull the length-prefixed strings out of a row, whatever its integer
    prefix turns out to be. Used for the small term-list tables, where the
    column layout matters less than the values."""
    for int_count in (4, 3, 2, 1, 0):
        pos = 1 + 4 * int_count
        values: list[str] = []
        ok = True
        while pos < len(record):
            if pos + 3 > len(record):
                ok = False
                break
            length = int.from_bytes(record[pos : pos + 3], "little")
            pos += 3
            if pos + length > len(record):
                ok = False
                break
            try:
                values.append(record[pos : pos + length].decode("utf-8"))
            except UnicodeDecodeError:
                ok = False
                break
            pos += length
        if ok and pos == len(record) and values:
            return values
    return []


# --------------------------------------------------------------------------
# rows -> reference dicts
# --------------------------------------------------------------------------

# EndNote column -> the CSL-aligned field name used everywhere else in this
# package. Same names parser.py produces, so either source can feed the index.
_SCALAR_MAP = {
    "title": "title",
    "short_title": "title_short",
    "secondary_title": "container_title",
    "tertiary_title": "collection_title",
    "year": "year",
    "volume": "volume",
    "number": "issue",
    "pages": "pages",
    "section": "article_number",
    "number_of_volumes": "number_of_volumes",
    "abstract": "abstract",
    "publisher": "publisher",
    "place_published": "publisher_place",
    "edition": "edition",
    "label": "label",
    "notes": "note",
    "research_notes": "research_notes",
    "accession_number": "accession_number",
    "access_date": "access_date",
    "language": "language",
    "custom_1": "custom_1",
}

_PDF_PREFIX = "internal-pdf://"


def _text(row: dict, column: str) -> str:
    value = row.get(column)
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace").strip()
    return "" if value is None else str(value)


def _values(row: dict, column: str) -> list[str]:
    raw = _text(row, column)
    return [part.strip() for part in raw.split(VALUE_SEPARATOR) if part.strip()]


def row_to_reference(
    row: dict,
    ref_type_names: dict[int, str] | None = None,
    journal_abbreviations: dict[str, str] | None = None,
) -> dict:
    """Turn a decoded `refs` row into the same shape parser.py yields."""
    from . import parser  # local import: parser imports lxml, this module need not

    type_code = int(row.get("reference_type") or 0)
    # A direct library row stores only a numeric code. Keep a stable explicit
    # value when no XML-derived mapping is available instead of silently
    # returning an empty string.
    ref_type = (ref_type_names or {}).get(type_code)
    if not ref_type:
        ref_type = f"endnote:type:{type_code}"
    ref: dict = {
        "rec_number": int(row.get("id") or 0),
        "ref_type": ref_type,
        "authors": _values(row, "author"),
        "editors": _values(row, "secondary_author"),
        "keywords": _values(row, "keywords"),
        "urls": _values(row, "url"),
        "pdf_files": _pdf_files(row),
    }
    for column, field in _SCALAR_MAP.items():
        ref[field] = _text(row, column)

    ref["doi"] = parser.normalize_doi(_text(row, "electronic_resource_number"))
    ref["isbn"] = parser._clean_serial_number(_text(row, "isbn"))
    ref["language"] = parser._normalize_language(ref["language"])
    ref["url"] = ref["urls"][0] if ref["urls"] else ""

    abbreviations = journal_abbreviations or {}
    ref["container_title_short"] = abbreviations.get(ref["container_title"], "") or _text(
        row, "alternate_title"
    )

    ref["content_hash"] = parser._content_hash(
        {k: v for k, v in ref.items() if k != "content_hash"}
    )
    return ref


def _pdf_files(row: dict) -> list[str]:
    """Attachment paths relative to `<Library>.Data/PDF`.

    The stored value is the same `internal-pdf://<folder-id>/<name>` form the
    XML export carries; the folder id is part of the location, so it is kept.
    """
    paths = []
    for raw in _values(row, "link_to_pdf"):
        candidate = raw[len(_PDF_PREFIX) :] if raw.startswith(_PDF_PREFIX) else raw
        candidate = candidate.strip().strip("/")
        if candidate and not re.match(r"^[a-z]+://", candidate):
            paths.append(candidate)
    return paths


def read_references(
    rdb_dir: str | Path, ref_type_names: dict[int, str] | None = None
) -> Iterator[dict]:
    """Yield one reference dict per record, straight from the library."""
    abbreviations = read_journal_abbreviations(rdb_dir)
    for row in read_rows(rdb_dir):
        yield row_to_reference(row, ref_type_names, abbreviations)
