"""Configuration loading and the setup wizard's path guessing."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from endnotex9_mcp import config, parser


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


def test_config_round_trip(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(config, "config_dir", lambda: tmp_path)
    saved = config.Config(endnote_xml="lib.xml", pdf_dir="pdfs", db_path=str(tmp_path / "x.db"))
    config.save_config(saved)
    assert config.load_config() == saved


def test_config_ignores_unknown_keys(tmp_path: Path, monkeypatch):
    # A config written by a newer version must not stop an older one loading.
    monkeypatch.setattr(config, "config_dir", lambda: tmp_path)
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump({"endnote_xml": "lib.xml", "future_option": 42}), encoding="utf-8"
    )
    loaded = config.load_config()
    assert loaded.endnote_xml == "lib.xml"
    assert loaded.db_path.endswith("library.db")  # filled in when absent


def test_config_accepts_a_direct_library_without_xml(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(config, "config_dir", lambda: tmp_path)
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump({"library_path": "C:/Research.enl"}), encoding="utf-8"
    )
    loaded = config.load_config()
    assert loaded.endnote_xml == ""
    assert loaded.library_path == "C:/Research.enl"


def test_missing_config_points_at_the_wizard(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(config, "config_dir", lambda: tmp_path / "empty")
    with pytest.raises(FileNotFoundError, match="endnotex9-mcp setup"):
        config.load_config()
