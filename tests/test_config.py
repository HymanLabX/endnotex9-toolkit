"""Configuration loading and the setup wizard's path guessing."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest
import yaml

from endnotex9_toolkit import config, parser


def test_library_path_is_read_from_the_export(xml_path):
    assert parser.library_path(xml_path) == "C:\\Test.enl"


def test_library_path_of_an_export_without_the_attribute(tmp_path: Path):
    path = tmp_path / "bare.xml"
    path.write_text(
        "<xml><records><record><rec-number>1</rec-number></record></records></xml>",
        encoding="utf-8",
    )
    assert parser.library_path(path) == ""


@pytest.mark.parametrize("declaration", ("DOCTYPE", "doctype", "ENTITY"))
def test_xml_parser_rejects_dtd_and_entity_declarations(tmp_path: Path, declaration: str):
    secret = tmp_path / "secret.txt"
    secret.write_text("must not be read", encoding="utf-8")
    export = tmp_path / "unsafe.xml"
    if declaration.casefold() == "doctype":
        prolog = f'<!{declaration} xml [<!ENTITY xxe SYSTEM "{secret.as_uri()}">]>'
    else:
        prolog = f'<!{declaration} xxe SYSTEM "{secret.as_uri()}">'
    export.write_text(
        f'<?xml version="1.0"?>{prolog}<xml><records><record>'
        "<rec-number>1</rec-number><titles><title>&xxe;</title></titles>"
        "</record></records></xml>",
        encoding="utf-8",
    )

    with pytest.raises(parser.UnsafeXmlError, match="DTD or entity"):
        list(parser.parse_library(export))


def test_library_path_uses_the_same_hardened_xml_reader(tmp_path: Path):
    export = tmp_path / "unsafe.xml"
    export.write_text(
        '<!DOCTYPE xml [<!ENTITY xxe SYSTEM "file:///not-used">]>'
        '<xml><database path="C:\\Research.enl"/></xml>',
        encoding="utf-8",
    )
    with pytest.raises(parser.UnsafeXmlError, match="DTD or entity"):
        parser.library_path(export)


def test_pdf_dir_guess_follows_the_recorded_library_path(tmp_path: Path):
    # The export is saved in one place, the library lives in another — the
    # common case, and the reason a sibling-folder guess is not enough.
    library = tmp_path / "elsewhere" / "My Library.enl"
    pdf_dir = tmp_path / "elsewhere" / "My Library.Data" / "PDF"
    pdf_dir.mkdir(parents=True)

    export = tmp_path / "exports" / "library.xml"
    export.parent.mkdir()
    export.write_text(
        f'<xml><records><record><database name="My Library.enl" '
        f'path="{library}">My Library.enl</database>'
        f"<rec-number>1</rec-number></record></records></xml>",
        encoding="utf-8",
    )

    assert config.default_pdf_dir(str(export)) == str(pdf_dir)


def test_pdf_dir_guess_falls_back_to_a_sibling_data_folder(tmp_path: Path):
    pdf_dir = tmp_path / "My Library.Data" / "PDF"
    pdf_dir.mkdir(parents=True)
    export = tmp_path / "library.xml"
    export.write_text("<xml><records></records></xml>", encoding="utf-8")

    assert config.default_pdf_dir(str(export)) == str(pdf_dir)


def test_pdf_dir_guess_gives_up_quietly(tmp_path: Path):
    assert config.default_pdf_dir(str(tmp_path / "absent.xml")) == ""


# Every `load_config` here is otherwise a live check against whatever
# EndNote actually has open on the machine running the tests — see
# `detect_open_library`. Tests that just want to describe the on-disk
# config, not whoever's dev machine happens to run them, pass this in
# explicitly; `detect_open_library`'s own behaviour gets separate tests
# below that don't.
def _no_endnote_running(monkeypatch):
    monkeypatch.setattr(config, "detect_open_library", lambda: "")


def test_config_round_trip(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(config, "config_dir", lambda: tmp_path)
    _no_endnote_running(monkeypatch)
    saved = config.Config(endnote_xml="lib.xml", pdf_dir="pdfs", db_path=str(tmp_path / "x.db"))
    config.save_config(saved)
    assert config.load_config() == saved


def test_config_ignores_unknown_keys(tmp_path: Path, monkeypatch):
    # A config written by a newer version must not stop an older one loading.
    monkeypatch.setattr(config, "config_dir", lambda: tmp_path)
    _no_endnote_running(monkeypatch)
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump({"endnote_xml": "lib.xml", "future_option": 42}), encoding="utf-8"
    )
    loaded = config.load_config()
    assert loaded.endnote_xml == "lib.xml"
    assert loaded.db_path.endswith("library.db")  # filled in when absent


def test_config_accepts_a_direct_library_without_xml(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(config, "config_dir", lambda: tmp_path)
    _no_endnote_running(monkeypatch)
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump({"library_path": "C:/Research.enl"}), encoding="utf-8"
    )
    loaded = config.load_config()
    assert loaded.endnote_xml == ""
    assert loaded.library_path == "C:/Research.enl"


def test_missing_config_points_at_the_wizard(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(config, "config_dir", lambda: tmp_path / "empty")
    with pytest.raises(FileNotFoundError, match="endnotex9-toolkit setup"):
        config.load_config()


def test_setup_command_outside_a_plugin_tree_is_the_bare_command(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(config, "_SOURCE_ROOT", tmp_path)
    assert config.setup_command() == "`endnotex9-toolkit setup`"


def test_missing_config_inside_a_plugin_names_the_uv_command(tmp_path: Path, monkeypatch):
    # A plugin user has no `endnotex9-toolkit` on PATH, so the hint has to be runnable as given.
    plugin = tmp_path / "plugin"
    (plugin / ".claude-plugin").mkdir(parents=True)
    (plugin / ".claude-plugin" / "plugin.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(config, "_SOURCE_ROOT", plugin)
    monkeypatch.setattr(config, "config_dir", lambda: tmp_path / "empty")
    with pytest.raises(FileNotFoundError) as excinfo:
        config.load_config()
    command = f'uv run --directory "{plugin}" --extra pdf endnotex9-toolkit setup'
    assert command in str(excinfo.value)


def test_config_dir_keeps_using_the_directory_setup_wrote_before_the_rename(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setattr(config.sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert config.config_dir() == tmp_path / "endnotex9-toolkit"

    (tmp_path / "endnotex9-mcp").mkdir()
    assert config.config_dir() == tmp_path / "endnotex9-mcp"

    (tmp_path / "endnotex9-toolkit").mkdir()
    assert config.config_dir() == tmp_path / "endnotex9-toolkit"


# --------------------------------------------------------------------------
# detect_open_library / load_config's live override
# --------------------------------------------------------------------------


def test_load_config_prefers_the_currently_open_library_over_the_saved_one(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setattr(config, "config_dir", lambda: tmp_path)
    stale = tmp_path / "Old Library.enl"
    stale.write_bytes(b"")
    open_now = tmp_path / "New Library.enl"
    open_now.write_bytes(b"")
    config.save_config(config.Config(library_path=str(stale)))

    monkeypatch.setattr(config, "detect_open_library", lambda: str(open_now))

    loaded = config.load_config()
    assert loaded.library_path == str(open_now)


def test_load_config_leaves_pdf_dir_alone_when_no_sibling_data_folder_exists(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setattr(config, "config_dir", lambda: tmp_path)
    open_now = tmp_path / "New Library.enl"
    open_now.write_bytes(b"")
    config.save_config(config.Config(library_path="", pdf_dir="/configured/pdfs"))

    monkeypatch.setattr(config, "detect_open_library", lambda: str(open_now))

    loaded = config.load_config()
    assert loaded.library_path == str(open_now)
    assert loaded.pdf_dir == "/configured/pdfs"  # no <Library>.Data/PDF beside open_now


def test_load_config_derives_pdf_dir_from_the_detected_library(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(config, "config_dir", lambda: tmp_path)
    open_now = tmp_path / "New Library.enl"
    open_now.write_bytes(b"")
    pdf_dir = tmp_path / "New Library.Data" / "PDF"
    pdf_dir.mkdir(parents=True)
    config.save_config(config.Config(library_path="", pdf_dir=""))

    monkeypatch.setattr(config, "detect_open_library", lambda: str(open_now))

    loaded = config.load_config()
    assert loaded.pdf_dir == str(pdf_dir)


def test_load_config_is_a_noop_when_the_detected_library_matches_the_saved_one(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setattr(config, "config_dir", lambda: tmp_path)
    library = tmp_path / "Library.enl"
    library.write_bytes(b"")
    config.save_config(config.Config(library_path=str(library), pdf_dir="/configured/pdfs"))

    monkeypatch.setattr(config, "detect_open_library", lambda: str(library))

    loaded = config.load_config()
    assert loaded.library_path == str(library)
    assert loaded.pdf_dir == "/configured/pdfs"  # untouched -- nothing changed


def test_load_config_falls_back_to_the_saved_library_when_nothing_is_detected(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setattr(config, "config_dir", lambda: tmp_path)
    config.save_config(config.Config(library_path="C:/Research.enl"))

    monkeypatch.setattr(config, "detect_open_library", lambda: "")

    loaded = config.load_config()
    assert loaded.library_path == "C:/Research.enl"


def test_detect_open_library_is_a_noop_off_windows(monkeypatch):
    monkeypatch.setattr(config.sys, "platform", "linux")
    assert config.detect_open_library() == ""


# --------------------------------------------------------------------------
# _is_held_by_another_process
#
# The probe used to be `open(path, "r+b")` and treat any failure as "EndNote
# has this open". A read-only `.enl` fails identically -- errno 13, no
# `winerror` -- while nothing holds it, so a closed read-only library was
# reported as open and `load_config` retargeted `library_path` at it.
# --------------------------------------------------------------------------

pytestmark_win32 = pytest.mark.skipif(
    sys.platform != "win32", reason="the lock probe is a Win32 call"
)


@pytestmark_win32
def test_an_unheld_library_is_not_reported_as_open(tmp_path: Path):
    library = tmp_path / "free.enl"
    library.write_bytes(b"x")
    assert config._is_held_by_another_process(str(library)) is False


@pytestmark_win32
def test_a_read_only_library_nobody_has_open_is_not_reported_as_open(tmp_path: Path):
    library = tmp_path / "readonly.enl"
    library.write_bytes(b"x")
    os.chmod(library, stat.S_IREAD)
    try:
        # ERROR_ACCESS_DENIED, not ERROR_SHARING_VIOLATION: unwritable says
        # nothing about whether EndNote is running.
        assert config._is_held_by_another_process(str(library)) is False
    finally:
        os.chmod(library, stat.S_IWRITE)


@pytestmark_win32
def test_a_library_held_by_another_process_is_reported_as_open(tmp_path: Path):
    library = tmp_path / "held.enl"
    library.write_bytes(b"x")
    # Open it the way EndNote does: readable by others, writable by nobody
    # else. A plain `open(..., "rb")` would not reproduce this -- Python
    # shares the file freely and the probe would (correctly) see no lock.
    #
    # Imported here rather than at module scope: `ctypes.wintypes` raises on
    # import off Windows, where this whole section is skipped anyway.
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    generic_read, file_share_read, open_existing = 0x80000000, 0x00000001, 3
    handle = kernel32.CreateFileW(
        str(library), generic_read, file_share_read, None, open_existing, 0, None
    )
    assert handle != ctypes.c_void_p(-1).value, "could not stage the lock"
    try:
        assert config._is_held_by_another_process(str(library)) is True
    finally:
        kernel32.CloseHandle(handle)


def test_detect_open_library_ignores_a_recent_but_unlocked_library(tmp_path: Path, monkeypatch):
    """MRU proves 'recently opened', not 'open now' -- see the docstring."""
    monkeypatch.setattr(config.sys, "platform", "win32")
    closed = tmp_path / "Closed Library.enl"
    closed.write_bytes(b"")

    class _FakeKey:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    fake_winreg = type(
        "winreg",
        (),
        {
            "HKEY_CURRENT_USER": None,
            "OpenKey": staticmethod(lambda *a, **kw: _FakeKey()),
            "QueryValueEx": staticmethod(lambda key, name: (str(closed), 1)),
        },
    )
    monkeypatch.setitem(__import__("sys").modules, "winreg", fake_winreg)

    # `closed` opens cleanly (nothing has it locked) -> not reported as open.
    assert config.detect_open_library() == ""


def test_detect_open_library_returns_a_locked_mru_entry(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(config.sys, "platform", "win32")
    open_now = tmp_path / "Open Library.enl"
    open_now.write_bytes(b"")

    class _FakeKey:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    fake_winreg = type(
        "winreg",
        (),
        {
            "HKEY_CURRENT_USER": None,
            "OpenKey": staticmethod(lambda *a, **kw: _FakeKey()),
            "QueryValueEx": staticmethod(lambda key, name: (str(open_now), 1)),
        },
    )
    monkeypatch.setitem(__import__("sys").modules, "winreg", fake_winreg)

    # Stubbed at the probe, not at `builtins.open`. Faking the lock with a
    # `PermissionError` from `open` is what let the read-only bug through:
    # it is the same exception an unwritable-but-closed library raises, so a
    # test written that way passes whether or not the code can tell the two
    # apart. `_is_held_by_another_process` has its own Win32-level tests.
    monkeypatch.setattr(config, "_is_held_by_another_process", lambda path: True)

    assert config.detect_open_library() == str(open_now)
