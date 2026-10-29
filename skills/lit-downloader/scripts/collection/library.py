"""Read-only access to an EndNote X9 library for the collection pipeline.

Only the refs table is read, through `endnotex9_toolkit.refstable`; nothing here writes to the
library. Attachments go in through EndNote's own RIS import, never by editing its files.
"""

from __future__ import annotations

import sys
from pathlib import Path


def _package_src() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "src" / "endnotex9_toolkit" / "__init__.py").is_file():
            return parent / "src"
    raise SystemExit(
        "Cannot find endnotex9_toolkit; run from an endnotex9-toolkit checkout or plugin."
    )


def data_dir(enl: str | Path) -> Path:
    enl = Path(enl)
    if enl.suffix.lower() != ".enl" or not enl.is_file():
        raise SystemExit(f"not an EndNote library: {enl}")
    data = enl.with_suffix(".Data")
    if not (data / "rdb" / "refs.MYD").is_file():
        raise SystemExit(f"no {data / 'rdb' / 'refs.MYD'}; is this an EndNote X9 library?")
    if (data / "sdb").is_dir():
        # X9.3 converts a library to SQLite; its rdb tables then stop changing.
        print(
            f"WARNING: {data / 'sdb'} exists. EndNote X9.3 converted this library and the old "
            "tables read here may be stale. Open it with the EndNote version that keeps the "
            "old format.",
            file=sys.stderr,
        )
    return data


def library_references(enl: str | Path) -> list[dict]:
    """One dict per live record: rec_number, doi (lower case), title, pdf_files."""
    sys.path.insert(0, str(_package_src()))
    from endnotex9_toolkit import refstable

    rdb = data_dir(enl) / "rdb"
    refs = []
    for row in refstable.read_rows(rdb):
        ref = refstable.row_to_reference(row)
        refs.append(
            {
                "rec_number": ref["rec_number"],
                "doi": (ref["doi"] or "").lower(),
                "title": ref.get("title", ""),
                "pdf_files": ref["pdf_files"],
            }
        )
    return refs


def referenced_folders(refs: list[dict]) -> set[str]:
    """Attachment folder ids (the `<n>` of `internal-pdf://<n>/<file>`) the library uses."""
    return {p.split("/", 1)[0] for ref in refs for p in ref["pdf_files"] if "/" in p}
