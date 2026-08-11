"""The lit-deep-reader skill's Notes-field merge.

`update_reference_fields` replaces the Notes field, so the only thing
standing between a saved reading note and a wiped field is the string this
script composes. That makes it worth the same suspicion as the lit-clean
scripts: the failure is silent, the diff looks plausible, and the text that
disappears is the user's own.

The merge is a pure function of (existing, body), so everything below runs
without an EndNote library. The CLI is exercised through `--existing-file`
for the same reason.

The script is imported by path rather than as a package: it is a Claude
skill, run as `python notes_block.py`.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "lit-deep-reader"
    / "scripts"
    / "notes_block.py"
)
sys.path.insert(0, str(SCRIPT.parent))

import notes_block  # noqa: E402

CR = notes_block.PARAGRAPH
OPEN, CLOSE = notes_block.OPEN_MARKER, notes_block.CLOSE_MARKER

BODY = "Read 2026-08-10 (standard).\nClaim: 12.5 K at 4% strain, PDF p.5."


def _block(body: str) -> str:
    return CR.join([OPEN, notes_block._normalise(body).strip(), CLOSE])


# --------------------------------------------------------------------------
# the four cases
# --------------------------------------------------------------------------


def test_empty_notes_get_the_block_alone():
    result = notes_block.merge_notes("", BODY)
    assert result["mode"] == "insert"
    assert result["merged"] == _block(BODY)
    assert result["preserved_chars"] == 0


def test_whitespace_only_notes_count_as_empty():
    # Blank paragraphs carried forward push the note out of view in
    # EndNote's field, and there is nothing in them to preserve.
    result = notes_block.merge_notes(CR + CR + "   " + CR, BODY)
    assert result["mode"] == "insert"
    assert result["merged"] == _block(BODY)


def test_existing_notes_are_kept_in_full():
    existing = "PubMed import dump" + CR + "supervisor: check the fatigue numbers"
    result = notes_block.merge_notes(existing, BODY)
    assert result["mode"] == "insert"
    assert result["merged"].startswith(existing)
    assert result["merged"].endswith(_block(BODY))
    # The whole of the old value survives -- this is the assertion the
    # entire script exists for.
    assert existing in result["merged"]
    assert result["preserved_chars"] == len(existing)


def test_a_second_reading_replaces_the_block_in_place():
    first = notes_block.merge_notes("kept before" + CR + CR + "kept after", BODY)
    # Manufacture text on both sides of the block, then re-read the paper.
    surrounded = "kept before" + CR + CR + _block(BODY) + CR + CR + "kept after"
    second = notes_block.merge_notes(surrounded, "Read 2026-09-01 (deep).")
    assert second["mode"] == "update"
    assert second["merged"].startswith("kept before")
    assert second["merged"].endswith("kept after")
    assert "2026-08-10" not in second["merged"], "the stale block should be gone"
    assert "2026-09-01" in second["merged"]
    assert second["merged"].count(OPEN) == 1
    assert first["mode"] == "insert"


def test_rerunning_with_the_same_body_is_idempotent():
    once = notes_block.merge_notes("kept", BODY)["merged"]
    twice = notes_block.merge_notes(once, BODY)["merged"]
    assert once == twice


def test_replace_drops_everything_but_only_when_asked():
    existing = "text the user typed"
    kept = notes_block.merge_notes(existing, BODY)
    dropped = notes_block.merge_notes(existing, BODY, replace=True)
    assert existing in kept["merged"]
    assert existing not in dropped["merged"]
    assert dropped["mode"] == "replace"
    assert dropped["preserved_chars"] == 0


# --------------------------------------------------------------------------
# refusals -- each one is a case where guessing would lose text
# --------------------------------------------------------------------------


def test_two_blocks_are_refused_rather_than_guessed_between():
    existing = _block("first reading") + CR + CR + _block("second reading")
    with pytest.raises(notes_block.NotesError, match="2 lit-deep-reader blocks"):
        notes_block.merge_notes(existing, BODY)
    # --replace is the documented way out, and it says what it costs.
    assert notes_block.merge_notes(existing, BODY, replace=True)["mode"] == "replace"


def test_unbalanced_markers_are_refused():
    with pytest.raises(notes_block.NotesError, match="edited by hand"):
        notes_block.merge_notes(OPEN + CR + "half a block", BODY)
    with pytest.raises(notes_block.NotesError, match="edited by hand"):
        notes_block.merge_notes("stray close" + CR + CLOSE, BODY)


def test_a_body_containing_the_markers_is_refused():
    # Otherwise the next run finds three markers and cannot parse its own
    # output.
    with pytest.raises(notes_block.NotesError, match="cannot appear inside"):
        notes_block.merge_notes("", f"a note mentioning {OPEN}")
    with pytest.raises(notes_block.NotesError, match="cannot appear inside"):
        notes_block.merge_notes("", f"a note mentioning {CLOSE}")


def test_an_empty_body_is_refused():
    with pytest.raises(notes_block.NotesError, match="empty"):
        notes_block.merge_notes("existing", "   \n  \n ")


# --------------------------------------------------------------------------
# line endings and text that must survive unchanged
# --------------------------------------------------------------------------


def test_line_endings_are_folded_to_endnotes_paragraph_separator():
    result = notes_block.merge_notes("", "one\r\ntwo\nthree")
    assert "\n" not in result["merged"]
    assert result["merged"] == CR.join([OPEN, "one", "two", "three", CLOSE])


def test_a_value_endnote_already_stored_comes_back_byte_identical():
    # Normalisation must be a no-op on CR-separated text, or every save
    # shows a whole-field diff and the user stops reading them.
    existing = "line one" + CR + "line two"
    assert notes_block._normalise(existing) == existing
    assert notes_block.merge_notes(existing, BODY)["merged"].startswith(existing)


def test_cjk_notes_pass_through_untouched():
    # Fullwidth punctuation on purpose (hence the noqa): it is what a real
    # Chinese note contains, and it is the half most likely to be mangled.
    existing = "导师批注：核对疲劳循环次数。"  # noqa: RUF001
    body = "精读 2026-08-10（标准）。\n结论：4% 应变下 ΔT_ad 为 12.5 K，PDF 第 5 页。"  # noqa: RUF001
    result = notes_block.merge_notes(existing, body)
    assert existing in result["merged"]
    assert "ΔT_ad 为 12.5 K" in result["merged"]
    assert result["preserved_chars"] == len(existing)


# --------------------------------------------------------------------------
# the CLI
# --------------------------------------------------------------------------


def _run(tmp_path: Path, existing: str, body: str, *extra: str) -> subprocess.CompletedProcess:
    existing_file = tmp_path / "existing.txt"
    note_file = tmp_path / "note.txt"
    existing_file.write_text(existing, encoding="utf-8")
    note_file.write_text(body, encoding="utf-8")
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--rec",
            "42",
            "--note-file",
            str(note_file),
            "--existing-file",
            str(existing_file),
            *extra,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def test_cli_emits_a_ready_to_use_update_payload(tmp_path):
    proc = _run(tmp_path, "keep me", BODY, "--json")
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["rec_number"] == 42
    assert payload["mode"] == "insert"
    assert payload["update"]["dry_run"] is True, "the script must never propose a live write"
    update = payload["update"]["updates"][0]
    assert update["rec_number"] == 42
    assert update["notes"].startswith("keep me")
    assert OPEN in update["notes"]


def test_cli_refuses_and_exits_nonzero_on_an_unmergeable_field(tmp_path):
    proc = _run(tmp_path, _block("a") + CR + _block("b"), BODY, "--json")
    assert proc.returncode != 0
    assert "lit-deep-reader blocks" in (proc.stderr + proc.stdout)


def test_cli_says_out_loud_when_replace_drops_text(tmp_path):
    proc = _run(tmp_path, "text the user typed", BODY, "--replace")
    assert proc.returncode == 0, proc.stderr
    assert "ALL DROPPED" in proc.stdout


def test_cli_reads_the_body_from_stdin(tmp_path):
    existing_file = tmp_path / "existing.txt"
    existing_file.write_text("", encoding="utf-8")
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--rec",
            "7",
            "--note-file",
            "-",
            "--existing-file",
            str(existing_file),
            "--json",
        ],
        input=BODY,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["rec_number"] == 7
