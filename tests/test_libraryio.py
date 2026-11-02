from __future__ import annotations

import subprocess
import sys

import pytest

from endnotex9_toolkit import libraryio


def test_library_lock_excludes_another_process_and_is_reentrant(tmp_path):
    enl = tmp_path / "Library.enl"
    code = """
import sys
from endnotex9_toolkit.libraryio import library_lock, LibraryIOError
try:
    with library_lock(sys.argv[1], timeout=0.1):
        print('acquired')
except LibraryIOError:
    print('busy')
"""
    with libraryio.library_lock(enl), libraryio.library_lock(enl):
        result = subprocess.run(
            [sys.executable, "-c", code, str(enl)], capture_output=True, text=True, timeout=10
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "busy"
    result = subprocess.run(
        [sys.executable, "-c", code, str(enl)], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "acquired"


def test_lock_is_released_after_an_exception(tmp_path):
    enl = tmp_path / "Library.enl"
    with pytest.raises(ValueError), libraryio.library_lock(enl):
        raise ValueError("synthetic")
    with libraryio.library_lock(enl, timeout=0):
        pass
