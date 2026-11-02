"""Identify completed, library-owned backups before offering them for removal."""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

MANIFEST = "endnotex9-backup.json"


def library_identity(path: str | Path) -> str:
    return os.path.normcase(str(Path(path).expanduser().resolve()))


def record_backup(destination: Path, library: Path) -> None:
    """Written last: an interrupted copy must never look like a completed backup."""
    (destination / MANIFEST).write_text(
        json.dumps(
            {
                "format": 1,
                "library": library_identity(library),
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def _created_at(directory: Path, library: Path) -> float | None:
    # Reject links/junctions outside this one backup root, even if their names match.
    root = library.resolve().parent / "backups"
    if (directory / ".backup-pending").exists():
        return None
    if directory.is_symlink() or directory.resolve().parent != root.resolve():
        return None
    if not all(
        p.is_file()
        for p in (directory / library.name, directory / "rdb/refs.MYD", directory / "rdb/refs.MYI")
    ):
        return None
    manifest = directory / MANIFEST
    if manifest.exists():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            if data.get("format") != 1 or data.get("library") != library_identity(library):
                return None
            created = datetime.fromisoformat(data["created_at"])
            return created.timestamp() if created.tzinfo is not None else None
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return None
    # Compatibility with backups made before manifests were introduced. Require both
    # the exact library name/timestamp convention and the expected backup contents.
    match = re.fullmatch(
        re.escape(library.stem) + r"-(\d{8}T\d{6}(?:\d{6})?Z)(?:-\d+)?", directory.name
    )
    if not match:
        return None
    stamp = match[1]
    try:
        fmt = "%Y%m%dT%H%M%S%fZ" if len(stamp) > 16 else "%Y%m%dT%H%M%SZ"
        return datetime.strptime(stamp, fmt).replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def excess_backups(
    library_path: str | Path, keep_recent: int, age_days: float
) -> tuple[list[Path], list[Path]]:
    if keep_recent < 0 or age_days < 0:
        raise ValueError("backup retention count and age must be non-negative")
    library = Path(library_path).resolve()
    root = library.parent / "backups"
    if not root.is_dir():
        return [], []
    entries = []
    for directory in root.iterdir():
        if directory.is_dir():
            created = _created_at(directory, library)
            if created is not None:
                entries.append((created, directory))
    entries.sort(key=lambda item: (item[0], item[1].name), reverse=True)
    cutoff = time.time() - age_days * 86400
    prunable, kept = [], []
    for index, (created, directory) in enumerate(entries):
        (prunable if index >= keep_recent and created < cutoff else kept).append(directory)
    return prunable, kept
