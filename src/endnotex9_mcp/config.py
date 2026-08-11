"""Load and save endnotex9-mcp configuration.

Config lives at a per-OS app-config directory, same convention as most
local-first CLI tools (APPDATA on Windows, XDG_CONFIG_HOME elsewhere).
"""

from __future__ import annotations

import os
import sys
from dataclasses import asdict, dataclass, fields
from pathlib import Path

import yaml

from . import parser

APP_NAME = "endnotex9-mcp"


@dataclass
class Config:
    # Optional when a direct `.enl` library is configured.
    endnote_xml: str = ""
    pdf_dir: str = ""
    db_path: str = ""
    # Path to the `.enl` file itself. Only groups need it — they live in the
    # library's own tables, not in the XML export.
    library_path: str = ""
    # Cap on how much extracted PDF text one read_pdf_pages call returns.
    max_pdf_chars: int = 20000
    watch: bool = True


def config_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = str(Path.home() / "Library" / "Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
    return Path(base) / APP_NAME


def config_path() -> Path:
    return config_dir() / "config.yaml"


# HKCU\<this>\File1 is EndNote for Windows's most-recently-used library,
# most recent first — see detect_open_library().
_ENDNOTE_RECENT_LIBRARIES_KEY = r"Software\ISI ResearchSoft\EndNote\Recent Libraries"

# Win32 error codes from CreateFileW, which is how `_is_held_by_another_process`
# tells "another process has this" apart from "you may not write this".
_ERROR_ACCESS_DENIED = 5
_ERROR_SHARING_VIOLATION = 32


def _is_held_by_another_process(path: str) -> bool:
    """Whether `path` is open elsewhere, as opposed to merely not writable.

    `open(path, "r+b")` cannot tell those apart. A sharing violation and a
    read-only file both arrive as `PermissionError` with `errno` 13 and no
    `winerror` set — the CRT reports one errno for both — so the plain-open
    probe this used to run called a read-only *closed* library "open".
    `load_config` would then silently retarget `library_path` at it purely
    because it was the most recent MRU entry, which is the one outcome
    `detect_open_library`'s lock check exists to prevent.

    `CreateFileW` reports the real Win32 code instead: 32
    (`ERROR_SHARING_VIOLATION`) means another process holds the file and is
    the only answer that counts as "EndNote has it open"; 5
    (`ERROR_ACCESS_DENIED`) is a read-only attribute, a read-only share or
    an ACL, and says nothing about EndNote either way.
    """
    import ctypes

    if not hasattr(ctypes, "WinDLL"):
        # Off Windows there is no CreateFileW to ask. Reached only when a
        # caller (or a test) has claimed win32; `ctypes.wintypes` would
        # raise on import a line later anyway, so say why instead. The
        # caller reads this as "cannot tell" and keeps the configured path.
        raise OSError("CreateFileW is available on Windows only")
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
    generic_write, open_existing = 0x40000000, 3
    handle = kernel32.CreateFileW(str(path), generic_write, 0, None, open_existing, 0, None)
    if handle == ctypes.c_void_p(-1).value:  # INVALID_HANDLE_VALUE
        return ctypes.get_last_error() == _ERROR_SHARING_VIOLATION
    kernel32.CloseHandle(handle)
    return False


def detect_open_library() -> str:
    """The `.enl` library currently open in EndNote X9, if any.

    A static `library_path` written by `setup` goes stale the moment the
    user opens a different library in EndNote itself — this project has no
    way to hear about that otherwise, so `load_config` calls this on every
    load and prefers whatever it finds here.

    EndNote for Windows records recently opened libraries in the registry,
    most-recent first, at ``HKCU\\<_ENDNOTE_RECENT_LIBRARIES_KEY>\\File1``.
    That alone only proves "most recently opened" — closing EndNote leaves
    the same entry sitting there, which would just trade one kind of
    staleness for another. The signal actually trusted here is a file-lock
    check in the same spirit as `writeback._ensure_unlocked`: EndNote holds
    a library's `.enl` in a share mode that makes a write open fail with a
    sharing violation for as long as the library is open. Only a `File1`
    path that is both on disk and currently locked this way is returned; a
    merely-recent but now-closed library returns "", so a stale MRU entry
    can never override a working static config on a machine where EndNote
    is not (or no longer) running.

    A sharing violation specifically — see `_is_held_by_another_process`.
    Any failure to open is *not* good enough: a read-only `.enl`, on a
    read-only share or with the read-only attribute set, fails the same way
    while nothing has it open, and taking that as "EndNote is running here"
    would retarget `library_path` at a library the user never opened.

    Windows only, and silent about it: other platforms, a missing registry
    key, or any read failure all just return "" so callers fall back to the
    configured `library_path` unconditionally. EndNote's other clients do
    not share this registry layout, and this project's write-side
    guardrails were only ever verified against EndNote for Windows anyway.
    """
    if sys.platform != "win32":
        return ""
    try:
        import winreg
    except ImportError:
        return ""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _ENDNOTE_RECENT_LIBRARIES_KEY) as key:
            candidate, _ = winreg.QueryValueEx(key, "File1")
    except OSError:
        return ""
    candidate = (candidate or "").strip()
    if not candidate or not Path(candidate).is_file():
        return ""
    try:
        held = _is_held_by_another_process(candidate)
    except OSError:
        return ""
    return candidate if held else ""


def _pdf_dir_for_library(library_path: str) -> str:
    """The sibling `<Library>.Data/PDF` folder, EndNote's own convention."""
    candidate = Path(library_path).with_suffix(".Data") / "PDF"
    return str(candidate) if candidate.is_dir() else ""


def default_library_path(endnote_xml: str) -> str:
    """The `.enl` file the export came from, as recorded inside the export."""
    xml_path = Path(endnote_xml).expanduser()
    if not xml_path.exists():
        return ""
    try:
        recorded = parser.library_path(xml_path)
    except OSError:
        return ""
    return recorded if recorded and Path(recorded).exists() else ""


def default_pdf_dir(endnote_xml: str) -> str:
    """Guess the attachment folder, so setup can offer it as the default.

    EndNote keeps attachments in `<Library>.Data/PDF` beside the `.enl` file.
    Two ways to find that: follow the library path recorded inside the export
    itself, or — if the export was saved next to the library — look for a
    sibling `.Data` folder. The alternative is the user hunting down a path
    with a `.Data` suffix they have never had reason to open.
    """
    xml_path = Path(endnote_xml).expanduser()

    if xml_path.exists():
        try:
            recorded = parser.library_path(xml_path)
        except OSError:
            recorded = ""
        if recorded:
            candidate = Path(recorded).with_suffix(".Data") / "PDF"
            if candidate.is_dir():
                return str(candidate)

    siblings = sorted(xml_path.parent.glob("*.Data/PDF"))
    return str(siblings[0]) if siblings else ""


def load_config() -> Config:
    path = config_path()
    if not path.exists():
        raise FileNotFoundError(
            f"No configuration found at {path}. Run `endnotex9-mcp setup` first."
        )
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    # Ignore unknown keys so a config written by a newer version still loads.
    known = {f.name for f in fields(Config)}
    config = Config(**{k: v for k, v in data.items() if k in known})
    if not config.db_path:
        config.db_path = str(config_dir() / "library.db")

    # Live check, every load — see detect_open_library() for why this isn't
    # just whatever `setup` last wrote to disk.
    open_library = detect_open_library()
    if open_library and os.path.normcase(open_library) != os.path.normcase(config.library_path):
        config.library_path = open_library
        pdf_dir = _pdf_dir_for_library(open_library)
        if pdf_dir:
            config.pdf_dir = pdf_dir

    return config


def save_config(config: Config) -> Path:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(asdict(config), allow_unicode=True), encoding="utf-8")
    return path
