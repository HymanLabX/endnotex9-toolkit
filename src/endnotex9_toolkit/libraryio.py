"""Cooperative library locks and recoverable publication of the three X9 files.

The lock coordinates toolkit processes, not EndNote. EndNote must still be closed.
Interrupted publications are never silently resumed over possible user edits.
"""

from __future__ import annotations

import functools
import hashlib
import inspect
import json
import os
import shutil
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from .backups import library_identity

_LOCKS: dict[str, threading.RLock] = {}
_GUARD = threading.Lock()
_HELD = threading.local()


class LibraryIOError(RuntimeError):
    pass


def recovery_dir(enl_path: str | Path) -> Path:
    return Path(enl_path).resolve().with_suffix(".enl.writeback-recovery")


def require_consistent(enl_path: str | Path) -> None:
    if recovery_dir(enl_path).exists():
        raise LibraryIOError(
            "An interrupted writeback needs recovery. Close EndNote and run "
            f'endnotex9-toolkit recover-writeback --library "{Path(enl_path)}"'
        )


@contextmanager
def library_lock(enl_path: str | Path, timeout: float = 30):
    """Reentrant within a thread; exclusive across threads and toolkit processes."""
    key = library_identity(enl_path)
    with _GUARD:
        lock = _LOCKS.setdefault(key, threading.RLock())
    if not lock.acquire(timeout=timeout):
        raise LibraryIOError("Another toolkit operation is writing this library; try again later")
    handle = None
    acquired = False
    held = getattr(_HELD, "paths", None)
    if held is None:
        held = _HELD.paths = set()
    try:
        if key in held:
            yield
            return
        # Keep this file: unlinking a lock file can let another process lock a new inode.
        handle = open(key + ".toolkit.lock", "a+b")
        handle.seek(0, os.SEEK_END)
        if not handle.tell():
            handle.write(b"\0")
            handle.flush()
        deadline = time.monotonic() + timeout
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise LibraryIOError("Another toolkit process holds the library lock") from exc
                time.sleep(0.05)
        held.add(key)
        try:
            yield
        finally:
            held.remove(key)
    finally:
        try:
            if handle is not None:
                try:
                    if acquired:
                        if os.name == "nt":
                            import msvcrt

                            handle.seek(0)
                            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                        else:
                            import fcntl

                            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                finally:
                    handle.close()
        finally:
            lock.release()


def library_operation(fn):
    """Hold the lock across policy checks, backup and publication; previews stay read-only."""
    signature = inspect.signature(fn)

    @functools.wraps(fn)
    def run(*args, **kwargs):
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        enl = bound.arguments["enl_path"] = Path(bound.arguments["enl_path"]).resolve()
        bound.arguments["rdb_dir"] = Path(bound.arguments["rdb_dir"]).resolve()
        if bound.arguments.get("dry_run", False):
            require_consistent(enl)
            return fn(*bound.args, **bound.kwargs)
        with library_lock(enl):
            require_consistent(enl)
            return fn(*bound.args, **bound.kwargs)

    return run


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _targets(rdb: Path, enl: Path) -> list[Path]:
    return [rdb / "refs.MYD", rdb / "refs.MYI", enl]


def source_fingerprint(rdb: str | Path, enl: str | Path) -> str:
    identity = library_identity(enl) + "\n" + library_identity(rdb)
    return _digest(
        (
            identity + "".join(_digest(p.read_bytes()) for p in _targets(Path(rdb), Path(enl)))
        ).encode()
    )


def _durable_write(path: Path, data: bytes) -> None:
    with path.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _replace_bytes(target: Path, data: bytes) -> None:
    fd, name = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp", dir=target.parent)
    staged = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        shutil.copymode(target, staged)
        os.replace(staged, target)
    finally:
        staged.unlink(missing_ok=True)


def recover_publication(rdb: str | Path, enl: str | Path) -> bool:
    """Restore before-images after verifying every live file is from this transaction.

    Caller holds the library lock and has checked that EndNote is closed. A live
    file changed by another program fails closed, preserving all recovery evidence.
    """
    rdb, enl = Path(rdb), Path(enl)
    journal = recovery_dir(enl)
    if not journal.exists():
        return False
    try:
        manifest = json.loads((journal / "transaction.json").read_text(encoding="utf-8"))
        if manifest["library"] != library_identity(enl) or manifest["rdb"] != library_identity(rdb):
            raise ValueError("library identity mismatch")
        entries = manifest["files"]
        targets = _targets(rdb, enl)
        if len(entries) != len(targets):
            raise ValueError("incomplete file manifest")
        originals = []
        for i, (entry, target) in enumerate(zip(entries, targets, strict=True)):
            before = (journal / f"before-{i}").read_bytes()
            if _digest(before) != entry["before"]:
                raise ValueError("damaged recovery snapshot")
            if _digest(target.read_bytes()) not in {entry["before"], entry["after"]}:
                raise ValueError(f"{target.name} was edited after the interrupted write")
            originals.append(before)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise LibraryIOError(
            f"Cannot safely recover {journal}: {exc}; preserve it for review"
        ) from exc
    for target, original in zip(targets, originals, strict=True):
        if target.read_bytes() != original:
            _replace_bytes(target, original)
    shutil.rmtree(journal)
    return True


def publish(rdb: Path, enl: Path, data: bytes, myi: bytes, staged_enl: Path) -> None:
    """Publish verified bytes, rolling back ordinary errors and journaling interruptions."""
    targets = _targets(rdb, enl)
    before = [p.read_bytes() for p in targets]
    after = [data, myi, staged_enl.read_bytes()]
    journal = recovery_dir(enl)
    journal.mkdir(exist_ok=False)
    # No source file is changed until all before-images and the manifest are durable.
    try:
        for i, original in enumerate(before):
            _durable_write(journal / f"before-{i}", original)
        _durable_write(
            journal / "transaction.json",
            json.dumps(
                {
                    "library": library_identity(enl),
                    "rdb": library_identity(rdb),
                    "files": [
                        {"before": _digest(a), "after": _digest(b)}
                        for a, b in zip(before, after, strict=True)
                    ],
                }
            ).encode(),
        )
    except Exception:
        shutil.rmtree(journal)
        raise
    try:
        for target, payload in zip(targets, after, strict=True):
            _replace_bytes(target, payload)
        if any(p.read_bytes() != expected for p, expected in zip(targets, after, strict=True)):
            raise LibraryIOError("Published library bytes failed verification")
    except Exception as exc:
        try:
            recover_publication(rdb, enl)
        except Exception as recovery_error:
            raise LibraryIOError(
                f"Writeback failed ({exc}); recovery also failed ({recovery_error}). "
                f"Keep EndNote closed and preserve {journal}."
            ) from exc
        raise
    shutil.rmtree(journal)
