"""Watch EndNote inputs for changes and auto-reindex.

The XML export remains supported for installations that only have an export,
but a configured ``.enl`` library is watched as well.  EndNote updates its
MyISAM tables by writing temporary files and renaming them into place, so a
single ``modified`` event is not a safe reindex trigger.  We debounce events,
wait for a stable fingerprint, and retry when a table is briefly absent or
locked.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from . import db as dbmod
from . import indexer

logger = logging.getLogger(__name__)

_DEBOUNCE_SECONDS = 2.0
_STABILITY_SECONDS = 0.35
_RETRY_SECONDS = 1.0


def _fingerprint(path: Path) -> tuple[int, int, int] | None:
    """Return a cheap identity/size fingerprint, or ``None`` if unavailable."""
    try:
        stat = path.stat()
    except (FileNotFoundError, OSError):
        return None
    return (stat.st_ino, stat.st_size, stat.st_mtime_ns)


def _stable(paths: Iterable[Path], delay: float) -> bool:
    """Whether all watched files have the same readable fingerprint twice."""
    targets = tuple(paths)
    before = tuple((path, _fingerprint(path)) for path in targets)
    present = tuple(item for item in before if item[1] is not None)
    # A library may be atomically replaced while its .enl marker is briefly
    # absent.  At least one concrete input must exist; optional siblings are
    # allowed to be missing and will be retried by the indexer if required.
    if not present:
        return False
    if delay:
        time.sleep(delay)
    after = tuple((path, _fingerprint(path)) for path, _ in present)
    return all(value is not None for _, value in after) and all(
        dict(present).get(path) == value for path, value in after
    )


class _XmlChangeHandler(FileSystemEventHandler):
    """Debounced handler retained under its historical name for compatibility."""

    def __init__(
        self,
        xml_path: str | Path,
        on_change: Callable[[], None],
        *,
        library_path: str | Path | None = None,
        stability_seconds: float = _STABILITY_SECONDS,
        retry_seconds: float = _RETRY_SECONDS,
    ) -> None:
        self._xml_path = Path(xml_path).expanduser().resolve() if xml_path else None
        self._library_path = Path(library_path).expanduser().resolve() if library_path else None
        watched: list[Path] = []
        if self._xml_path is not None:
            watched.append(self._xml_path)
        if self._library_path is not None:
            watched.extend(
                (
                    self._library_path,
                    self._library_path.with_suffix(".Data") / "rdb" / "refs.MYD",
                    self._library_path.with_suffix(".Data") / "rdb" / "misc.MYD",
                )
            )
        self._watched = tuple(dict.fromkeys(watched))
        self._on_change = on_change
        self._stability_seconds = stability_seconds
        self._retry_seconds = retry_seconds
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()

    @property
    def watched_paths(self) -> tuple[Path, ...]:
        return self._watched

    def _matches(self, event_path: str | Path) -> bool:
        try:
            return Path(event_path).resolve() in self._watched
        except OSError:
            return False

    def _schedule(self, delay: float = _DEBOUNCE_SECONDS) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(delay, self._fire)
            self._timer.daemon = True
            self._timer.start()

    def _fire(self) -> None:
        # Atomic replacement and EndNote's table locking can leave a target
        # absent or changing for a short period.  Keep the timer alive until a
        # complete stable window is observed.
        if not _stable(self._watched, self._stability_seconds):
            self._schedule(self._retry_seconds)
            return
        try:
            self._on_change()
        except (OSError, RuntimeError, ValueError):
            logger.warning("auto-reindex input is busy; retrying", exc_info=True)
            self._schedule(self._retry_seconds)
        except Exception:
            # Keep the watcher alive for parser/indexer errors too; a later
            # EndNote save may produce a valid snapshot.
            logger.exception("auto-reindex failed; retrying")
            self._schedule(self._retry_seconds)

    def on_modified(self, event) -> None:
        if not event.is_directory and self._matches(event.src_path):
            self._schedule()

    def on_created(self, event) -> None:
        if not event.is_directory and self._matches(event.src_path):
            self._schedule()

    def on_moved(self, event) -> None:
        if not event.is_directory and (
            self._matches(event.src_path) or self._matches(event.dest_path)
        ):
            self._schedule()


def watch_and_reindex(
    xml_path: str | Path | None,
    db_path: str | Path,
    library_path: str | Path | None = None,
) -> Observer:
    """Start a background observer for XML and/or a direct EndNote library.

    ``library_path`` is optional to preserve the original XML-only API.
    """
    handler = _XmlChangeHandler(
        xml_path or "", lambda: _reindex(xml_path, db_path, library_path), library_path=library_path
    )
    if not handler.watched_paths:
        raise ValueError("watch_and_reindex needs an XML or .enl path")

    def parent(path: Path) -> Path:
        return path.parent

    observer = Observer()
    for directory in dict.fromkeys(parent(path) for path in handler.watched_paths):
        observer.schedule(handler, str(directory), recursive=False)
    observer.start()
    return observer


def _reindex(
    xml_path: str | Path | None,
    db_path: str | Path,
    library_path: str | Path | None,
) -> None:
    # A fresh connection each time: watchdog runs on a background thread, and
    # sqlite3 connections are not safe to share across threads. The lock is
    # keyed on the database file, so this still excludes a concurrent write on
    # the server's own connection.
    conn = None
    try:
        conn = dbmod.connect(db_path)
        with dbmod.connection_lock(conn):
            stats = indexer.build_index(xml_path, conn, library_path=library_path)
            if library_path:
                # `misc.MYD` is one of the watched files precisely because it
                # holds the groups, so a run that skipped them would be deaf
                # to half of what woke it up.
                try:
                    stats["group_refresh"] = indexer.index_groups(conn, library_path)
                except (OSError, RuntimeError) as exc:
                    # Same policy as `rebuild_index`: a locked or half-written
                    # library must not cost us the reference update as well.
                    stats["group_refresh"] = {"error": str(exc)}
        logger.info("auto-reindexed after EndNote input change: %s", stats)
    finally:
        if conn is not None:
            conn.close()
