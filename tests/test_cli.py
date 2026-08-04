"""Setup accepts direct-library and export-only installations."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from endnotex9_mcp import cli, db
from endnotex9_mcp.config import Config


def test_setup_accepts_library_without_xml(tmp_path: Path, monkeypatch):
    library = tmp_path / "Research.enl"
    library.write_bytes(b"marker")
    answers = iter((str(library), "", ""))
    captured = {}

    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    monkeypatch.setattr(cli, "config_dir", lambda: tmp_path / "config")
    monkeypatch.setattr(
        cli,
        "save_config",
        lambda config: (captured.setdefault("config", config), tmp_path / "config.yaml")[1],
    )
    monkeypatch.setattr(
        cli,
        "build_index",
        lambda xml, conn, **kwargs: {
            "source": "library",
            "total": 2,
            "added": 2,
        },
    )
    monkeypatch.setattr(
        cli,
        "index_groups",
        lambda conn, path: {
            "groups": 0,
            "memberships": 0,
        },
    )

    cli.cmd_setup(argparse.Namespace())

    assert captured["config"].library_path == str(library)
    assert captured["config"].endnote_xml == ""


def test_setup_keeps_xml_only_flow(tmp_path: Path, monkeypatch):
    export = tmp_path / "library.xml"
    export.write_text("<xml><records/></xml>", encoding="utf-8")
    answers = iter(("", str(export), "", ""))
    captured = {}

    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    monkeypatch.setattr(cli, "config_dir", lambda: tmp_path / "config")
    monkeypatch.setattr(cli, "default_library_path", lambda _xml: "")
    monkeypatch.setattr(
        cli,
        "save_config",
        lambda config: (captured.setdefault("config", config), tmp_path / "config.yaml")[1],
    )
    monkeypatch.setattr(
        cli,
        "build_index",
        lambda xml, conn, **kwargs: {
            "source": "xml",
            "total": 0,
            "added": 0,
        },
    )

    cli.cmd_setup(argparse.Namespace())

    assert captured["config"].library_path == ""
    assert captured["config"].endnote_xml == str(export)


def test_non_interactive_setup_never_prompts_and_accepts_all_paths(tmp_path: Path, monkeypatch):
    export = tmp_path / "library.xml"
    export.write_text("<xml><records/></xml>", encoding="utf-8")
    pdf_dir = tmp_path / "PDF"
    pdf_dir.mkdir()
    database = tmp_path / "custom.db"
    captured = {}

    monkeypatch.setattr(
        "builtins.input",
        lambda _prompt: pytest.fail("non-interactive setup attempted to prompt"),
    )
    monkeypatch.setattr(
        cli,
        "save_config",
        lambda config: (captured.setdefault("config", config), tmp_path / "config.yaml")[1],
    )
    args = cli.build_parser().parse_args(
        [
            "setup",
            "--xml",
            str(export),
            "--pdf-dir",
            str(pdf_dir),
            "--db-path",
            str(database),
            "--no-watch",
            "--skip-index",
        ]
    )
    cli.cmd_setup(args)

    config = captured["config"]
    assert config.endnote_xml == str(export.resolve())
    assert config.pdf_dir == str(pdf_dir.resolve())
    assert config.db_path == str(database.resolve())
    assert config.watch is False


def test_non_interactive_setup_auto_detects_the_direct_pdf_folder(tmp_path: Path, monkeypatch):
    library = tmp_path / "Research.enl"
    library.write_bytes(b"marker")
    pdf_dir = library.with_suffix(".Data") / "PDF"
    pdf_dir.mkdir(parents=True)
    captured = {}
    monkeypatch.setattr(
        cli,
        "save_config",
        lambda config: (captured.setdefault("config", config), tmp_path / "config.yaml")[1],
    )

    args = cli.build_parser().parse_args(["setup", "--library", str(library), "--skip-index"])
    cli.cmd_setup(args)
    assert captured["config"].pdf_dir == str(pdf_dir.resolve())


def test_non_interactive_setup_rejects_missing_sources(tmp_path: Path):
    args = cli.build_parser().parse_args(
        ["setup", "--xml", str(tmp_path / "missing.xml"), "--skip-index"]
    )
    with pytest.raises(ValueError, match="does not exist"):
        cli.cmd_setup(args)


def test_doctor_reports_a_healthy_xml_installation(tmp_path: Path, monkeypatch):
    export = tmp_path / "library.xml"
    export.write_text("<xml><records/></xml>", encoding="utf-8")
    database = tmp_path / "library.db"
    conn = db.connect(database)
    conn.close()
    config_file = tmp_path / "config.yaml"
    config_file.write_text("configured", encoding="utf-8")
    config = Config(endnote_xml=str(export), db_path=str(database), watch=False)
    monkeypatch.setattr(cli, "config_path", lambda: config_file)
    monkeypatch.setattr(cli, "load_config", lambda: config)

    report = cli.doctor_report()
    assert report["ok"] is True
    assert {check["name"]: check["status"] for check in report["checks"]}["index"] == "pass"


def test_doctor_json_is_machine_readable(tmp_path: Path, monkeypatch, capsys):
    config_file = tmp_path / "missing.yaml"
    monkeypatch.setattr(cli, "config_path", lambda: config_file)
    with pytest.raises(SystemExit) as exc:
        cli.cmd_doctor(argparse.Namespace(json=True))
    assert exc.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["checks"][0]["name"] == "config"


def test_doctor_rejects_an_unsafe_xml_source(tmp_path: Path, monkeypatch):
    export = tmp_path / "unsafe.xml"
    export.write_text(
        '<!DOCTYPE xml [<!ENTITY xxe SYSTEM "file:///not-used">]><xml/>',
        encoding="utf-8",
    )
    database = tmp_path / "library.db"
    conn = db.connect(database)
    conn.close()
    config_file = tmp_path / "config.yaml"
    config_file.write_text("configured", encoding="utf-8")
    monkeypatch.setattr(cli, "config_path", lambda: config_file)
    monkeypatch.setattr(
        cli,
        "load_config",
        lambda: Config(endnote_xml=str(export), db_path=str(database)),
    )

    report = cli.doctor_report()
    assert report["ok"] is False
    statuses = {check["name"]: check["status"] for check in report["checks"]}
    assert statuses["xml"] == "fail"
    assert statuses["source"] == "fail"


def test_doctor_reports_malformed_xml_instead_of_crashing(tmp_path: Path, monkeypatch):
    export = tmp_path / "malformed.xml"
    export.write_text("<xml><records>", encoding="utf-8")
    database = tmp_path / "library.db"
    conn = db.connect(database)
    conn.close()
    config_file = tmp_path / "config.yaml"
    config_file.write_text("configured", encoding="utf-8")
    monkeypatch.setattr(cli, "config_path", lambda: config_file)
    monkeypatch.setattr(
        cli,
        "load_config",
        lambda: Config(endnote_xml=str(export), db_path=str(database)),
    )

    report = cli.doctor_report()
    assert report["ok"] is False
    xml_check = next(check for check in report["checks"] if check["name"] == "xml")
    assert xml_check["status"] == "fail"
    assert "invalid" in xml_check["detail"]
