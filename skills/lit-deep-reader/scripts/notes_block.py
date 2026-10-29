"""Compose the Notes value that saves a reading note without losing the old one.

`update_reference_fields` replaces the Notes field wholesale -- it has no
append mode, and it cannot have one, because the caller is the only party
that knows whether the new text supersedes the old or sits beside it. So
every "save my reading note" ends with a model concatenating strings by
hand, and the failure that follows is silent: an import dump, a
supervisor's comment or last month's reading is gone, the diff looked
plausible, and nobody notices until they go looking for the old text.

This script does that concatenation once, in code, with the case analysis
written down:

* no block yet          -> append after the existing text, byte for byte
* block already present -> replace that block in place, keep both sides
* two blocks present    -> refuse; guessing which one is the current
                           reading is exactly the wrong instinct here
* --replace             -> drop everything else, but only when asked

It never writes to the library. It reads the current Notes value and
prints the merged one plus the `update_reference_fields` payload; the write
goes through the MCP tool under its own dry-run-then-confirm gate.
"""

from __future__ import annotations

import argparse
import json
import sys
from importlib.util import find_spec
from pathlib import Path


def package_root() -> Path | None:
    """The endnotex9-toolkit tree this script ships inside, or None if detached.

    Do not count parent directories here. The same script runs from a source
    checkout and from an installed plugin, where the agent has copied the
    plugin somewhere else entirely, so walk up looking for the package
    instead of assuming a depth.
    """
    for parent in Path(__file__).resolve().parents:
        if (parent / "src" / "endnotex9_toolkit" / "__init__.py").is_file():
            return parent
    return None


PACKAGE_ROOT = package_root()
if PACKAGE_ROOT is not None:
    sys.path.insert(0, str(PACKAGE_ROOT / "src"))

# EndNote's text fields separate paragraphs with a bare CR, the same
# character `refstable` uses to separate repeated values. Anything composed
# here is normalised to it, so a note written on Windows with CRLF does not
# land in the field as a row of empty paragraphs.
PARAGRAPH = "\r"

OPEN_MARKER = "[lit-deep-reader]"
CLOSE_MARKER = "[/lit-deep-reader]"


class NotesError(RuntimeError):
    """A merge that would lose or corrupt text. Always fatal."""


def _normalise(text: str) -> str:
    """Fold CRLF/LF/CR to EndNote's paragraph separator.

    Idempotent, and a no-op on text already stored by EndNote -- which
    matters more than it looks: the untouched half of a merged value has to
    come out byte-identical, or every save shows a whole-field diff and the
    user stops reading them.
    """
    return text.replace("\r\n", PARAGRAPH).replace("\n", PARAGRAPH)


def _split_lines(text: str) -> list[str]:
    return _normalise(text).split(PARAGRAPH)


def merge_notes(existing: str, body: str, replace: bool = False) -> dict:
    """The merged Notes value for `body`, given whatever is in `existing`.

    Returns the merged text plus enough context for the caller to explain
    the change: which of the four cases applied, and how much of the
    existing text is carried through.
    """
    body = _normalise(body).strip(PARAGRAPH).strip()
    if not body:
        raise NotesError("the note body is empty")
    if OPEN_MARKER in body or CLOSE_MARKER in body:
        raise NotesError(
            f"the note body contains {OPEN_MARKER!r} or {CLOSE_MARKER!r}; those delimit the "
            "block and cannot appear inside it -- pass the note text alone"
        )

    existing = _normalise(existing)
    block = PARAGRAPH.join([OPEN_MARKER, body, CLOSE_MARKER])

    opens = existing.count(OPEN_MARKER)
    closes = existing.count(CLOSE_MARKER)

    if replace:
        return {
            "mode": "replace",
            "merged": block,
            "existing_chars": len(existing),
            "preserved_chars": 0,
            "had_block": bool(opens),
        }

    if opens != closes:
        raise NotesError(
            f"the record's Notes field has {opens} opening and {closes} closing markers; "
            "it was edited by hand and this script will not guess where the block ends"
        )
    if opens > 1:
        raise NotesError(
            f"the record's Notes field already holds {opens} lit-deep-reader blocks; "
            "remove the stale one in EndNote first, or pass --replace to drop everything"
        )

    if opens == 1:
        start = existing.index(OPEN_MARKER)
        end = existing.index(CLOSE_MARKER, start) + len(CLOSE_MARKER)
        if end <= start:
            raise NotesError("the closing marker precedes the opening one; fix the field by hand")
        before, after = existing[:start], existing[end:]
        merged = before + block + after
        return {
            "mode": "update",
            "merged": merged,
            "existing_chars": len(existing),
            "preserved_chars": len(before) + len(after),
            "had_block": True,
        }

    stripped = existing.strip(PARAGRAPH).strip()
    if not stripped:
        # A field holding only whitespace is empty for every purpose the
        # user cares about; carrying its blank paragraphs forward just
        # pushes the note out of view in EndNote's display.
        return {
            "mode": "insert",
            "merged": block,
            "existing_chars": len(existing),
            "preserved_chars": 0,
            "had_block": False,
        }

    merged = stripped + PARAGRAPH + PARAGRAPH + block
    return {
        "mode": "insert",
        "merged": merged,
        "existing_chars": len(existing),
        "preserved_chars": len(stripped),
        "had_block": False,
    }


def read_existing_notes(rec_number: int) -> str:
    """The record's current Notes value, read straight from `refs.MYD`.

    Read-only, and deliberately not through the SQLite index: the index can
    lag a hand edit made in EndNote, and merging into a stale value is how
    you lose the paragraph the user typed this morning.
    """
    if PACKAGE_ROOT is None and find_spec("endnotex9_toolkit") is None:
        raise SystemExit(
            "Cannot find endnotex9_toolkit. Run this script from an endnotex9-toolkit checkout "
            "or plugin directory, or pass --existing-file to work without the library."
        )
    from endnotex9_toolkit import config as configmod
    from endnotex9_toolkit import groups, refstable

    cfg = configmod.load_config()
    if not cfg.library_path:
        raise SystemExit("No library_path configured; run `endnotex9-toolkit setup` first.")
    rdb = groups.rdb_dir(cfg.library_path)
    if not (rdb / "refs.MYD").exists():
        raise SystemExit(f"No refs.MYD under {rdb} -- is library_path correct?")

    for row in refstable.read_rows(rdb):
        if int(row.get("id") or 0) != rec_number:
            continue
        value = row.get("notes")
        if isinstance(value, (bytes, bytearray)):
            return value.decode("utf-8", "replace")
        return "" if value is None else str(value)
    raise SystemExit(f"No record {rec_number} in this library.")


def _read_body(source: str) -> str:
    if source == "-":
        return sys.stdin.read()
    return Path(source).read_text(encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Compose the merged EndNote Notes value for a lit-deep-reader note.",
    )
    ap.add_argument("--rec", type=int, required=True, help="EndNote record number")
    ap.add_argument(
        "--note-file",
        required=True,
        help="file holding the note block body, or '-' to read it from stdin",
    )
    ap.add_argument(
        "--existing-file",
        help="read the current Notes value from this file instead of the library "
        "(for dry runs away from the machine holding the .enl)",
    )
    ap.add_argument(
        "--replace",
        action="store_true",
        help="discard everything already in the Notes field. Only with the user's agreement.",
    )
    ap.add_argument("--json", action="store_true", help="emit the payload as JSON on stdout")
    args = ap.parse_args()

    body = _read_body(args.note_file)
    if args.existing_file is not None:
        existing = Path(args.existing_file).read_text(encoding="utf-8")
    else:
        existing = read_existing_notes(args.rec)

    try:
        result = merge_notes(existing, body, replace=args.replace)
    except NotesError as exc:
        raise SystemExit(f"error: {exc}") from exc

    payload = {
        "rec_number": args.rec,
        "mode": result["mode"],
        "existing_chars": result["existing_chars"],
        "preserved_chars": result["preserved_chars"],
        "merged_chars": len(result["merged"]),
        "update": {
            "updates": [{"rec_number": args.rec, "notes": result["merged"]}],
            "dry_run": True,
        },
    }

    if args.json:
        json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return

    lost = result["existing_chars"] and not result["preserved_chars"]
    print(f"record {args.rec}: {result['mode']}")
    print(f"  existing notes : {result['existing_chars']} chars")
    print(
        f"  carried through: {result['preserved_chars']} chars"
        + ("  <-- ALL DROPPED" if lost else "")
    )
    print(f"  merged value   : {payload['merged_chars']} chars")
    print()
    print("--- merged Notes value ---")
    for line in _split_lines(result["merged"]):
        print(line)
    print("--- end ---")
    print()
    print("Pass this to update_reference_fields with dry_run=True first, show the diff, and")
    print("have the user close EndNote before the dry_run=False call.")


if __name__ == "__main__":
    main()
