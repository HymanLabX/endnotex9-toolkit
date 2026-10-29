"""Choose where references are read from: the library itself, or the XML export.

Reading the library's `refs.MYD` directly removes the only manual step in the
whole system — nobody has to remember to re-export XML after editing in
EndNote. But that format is reverse-engineered and unversioned by Clarivate,
so the export stays as the fallback: documented, stable, and the thing to
fall back to if an EndNote update ever changes the tables.

The policy here is therefore "library first, export second", with two extra
jobs the export is uniquely good for:

* **Learning reference-type names.** A record stores only EndNote's internal
  type code; the code-to-name table belongs to the application. Joining an
  export against the library by record number recovers the mapping, which is
  then remembered in the index so later library-only reads keep real names.
* **Cross-checking.** `crosscheck` compares the two readers field by field.
  That is how the row codec was validated in the first place, and it stays
  available as a way to notice a format change instead of trusting silence.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from pathlib import Path

from . import db as dbmod
from . import groups, parser, refstable

logger = logging.getLogger(__name__)

_REF_TYPE_META_KEY = "ref_type_names"


class SourceUnavailable(RuntimeError):
    pass


def library_rdb(library_path: str | Path | None) -> Path | None:
    if not library_path:
        return None
    rdb = groups.rdb_dir(library_path)
    return rdb if (rdb / "refs.MYD").exists() else None


# --------------------------------------------------------------------------
# reference type names
# --------------------------------------------------------------------------


def learn_ref_type_names(rdb_dir: str | Path, xml_path: str | Path) -> dict[int, str]:
    """Recover internal type code -> type name by joining the two sources."""
    names_by_record = {ref["rec_number"]: ref["ref_type"] for ref in parser.parse_library(xml_path)}
    mapping: dict[int, str] = {}
    for row in refstable.read_rows(rdb_dir):
        code = int(row.get("reference_type") or 0)
        name = names_by_record.get(int(row.get("id") or 0))
        if name and code not in mapping:
            mapping[code] = name
    return mapping


def stored_ref_type_names(conn) -> dict[int, str]:
    raw = dbmod.get_meta(conn, _REF_TYPE_META_KEY)
    if not raw:
        return {}
    try:
        return {int(k): v for k, v in json.loads(raw).items()}
    except (ValueError, TypeError):
        return {}


def store_ref_type_names(conn, mapping: dict[int, str]) -> None:
    dbmod.set_meta(
        conn,
        _REF_TYPE_META_KEY,
        json.dumps({str(k): v for k, v in mapping.items()}, ensure_ascii=False),
    )


# --------------------------------------------------------------------------
# source selection
# --------------------------------------------------------------------------


def open_references(
    conn,
    xml_path: str | Path | None = None,
    library_path: str | Path | None = None,
) -> tuple[str, Iterator[dict], str]:
    """Return (source name, reference iterator, note).

    Prefers the library. Falls back to the XML export if the library is
    absent or unreadable, and says why in the note.
    """
    rdb = library_rdb(library_path)
    if rdb is not None:
        mapping = stored_ref_type_names(conn)
        note = ""
        if not mapping and xml_path and Path(xml_path).exists():
            try:
                mapping = learn_ref_type_names(rdb, xml_path)
                store_ref_type_names(conn, mapping)
                note = f"learned {len(mapping)} reference-type names from the XML export"
            except Exception:
                logger.exception("could not learn reference-type names from %s", xml_path)
        try:
            # Materialise eagerly: a format problem should surface here,
            # while there is still an export to fall back to, not halfway
            # through writing the index.
            records = list(refstable.read_references(rdb, mapping))
        except Exception as exc:
            logger.warning("reading %s failed (%s); falling back to the XML export", rdb, exc)
            if not (xml_path and Path(xml_path).exists()):
                raise
            return (
                "xml",
                parser.parse_library(xml_path),
                f"library read failed ({exc}); used the XML export instead",
            )
        if not mapping:
            note = (note + "; " if note else "") + (
                "reference-type names unknown — index once with an XML export configured "
                "to learn them"
            )
        return "library", iter(records), note

    if xml_path and Path(xml_path).exists():
        return "xml", parser.parse_library(xml_path), "no readable library configured"

    raise SourceUnavailable(
        "Neither the EndNote library nor an XML export is readable. Set "
        "library_path and/or endnote_xml in the config."
    )


# --------------------------------------------------------------------------
# cross-check
# --------------------------------------------------------------------------

# Fields the two sources are not expected to agree on: the export resolves the
# journal abbreviation through EndNote's term list at export time, and carries
# a type *name* the library does not store.
_CROSSCHECK_SKIP = {"content_hash", "container_title_short", "ref_type"}


def crosscheck(
    rdb_dir: str | Path, xml_path: str | Path, ref_type_names: dict[int, str] | None = None
) -> dict:
    """Compare the direct reader against the XML export, field by field.

    Returns per-field agreement counts. This is the check that caught a
    two-byte field-width error affecting only nine records — the kind of bug
    a spot check would sail straight past.
    """
    from_xml = {ref["rec_number"]: ref for ref in parser.parse_library(xml_path)}
    from_lib = {
        ref["rec_number"]: ref for ref in refstable.read_references(rdb_dir, ref_type_names or {})
    }

    common = sorted(set(from_xml) & set(from_lib))
    fields = sorted(
        (set().union(*(r.keys() for r in from_xml.values())) if from_xml else set())
        - _CROSSCHECK_SKIP
    )
    agree = {field: 0 for field in fields}
    examples: dict[str, list] = {}

    for rec_number in common:
        a, b = from_xml[rec_number], from_lib[rec_number]
        for field in fields:
            if a.get(field) == b.get(field):
                agree[field] += 1
            elif len(examples.setdefault(field, [])) < 2:
                examples[field].append((rec_number, a.get(field), b.get(field)))

    return {
        "records_in_xml": len(from_xml),
        "records_in_library": len(from_lib),
        "only_in_xml": sorted(set(from_xml) - set(from_lib))[:20],
        "only_in_library": sorted(set(from_lib) - set(from_xml))[:20],
        "compared": len(common),
        "agreement": agree,
        "mismatch_examples": examples,
    }
