"""Command-line entry point: setup / index / index-pdfs / status / serve."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from . import browse, db, libraryio, sources, writeback
from . import pdf as pdfmod
from .config import (
    Config,
    config_dir,
    config_path,
    default_library_path,
    default_pdf_dir,
    detect_open_library,
    library_data_dir,
    load_config,
    save_config,
    setup_command,
)
from .indexer import build_index, index_groups


def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    answer = input(f"{prompt}{suffix}: ").strip().strip('"')
    return answer or default


def _absolute(value: str | None) -> str:
    return str(Path(value).expanduser().resolve()) if value else ""


def _setup_is_non_interactive(args: argparse.Namespace) -> bool:
    explicit = ("library", "endnote_xml", "pdf_dir", "db_path")
    return bool(
        getattr(args, "non_interactive", False) or getattr(args, "use_open_library", False)
    ) or any(getattr(args, name, None) is not None for name in explicit)


def _validate_non_interactive_paths(library: str, xml_path: str, pdf_dir: str) -> None:
    if not (library or xml_path):
        raise ValueError("non-interactive setup requires --library and/or --xml")
    if library and Path(library).suffix.casefold() != ".enl":
        raise ValueError("--library must point to an EndNote .enl file")
    for label, candidate in (("--library", library), ("--xml", xml_path)):
        if candidate and not Path(candidate).is_file():
            raise ValueError(f"{label} does not exist or is not a file: {candidate}")
    if pdf_dir and not Path(pdf_dir).is_dir():
        raise ValueError(f"--pdf-dir does not exist or is not a directory: {pdf_dir}")


def cmd_setup(args: argparse.Namespace) -> None:
    print("endnotex9-toolkit setup\n")
    non_interactive = _setup_is_non_interactive(args)
    if non_interactive:
        library = _absolute(getattr(args, "library", None))
        if getattr(args, "use_open_library", False):
            if library:
                raise ValueError("Choose either --library or --use-open-library")
            library = _absolute(detect_open_library())
            if not library:
                raise ValueError("No open EndNote X9 library detected; pass --library explicitly")
        xml_path = _absolute(getattr(args, "endnote_xml", None))
        requested_pdf = getattr(args, "pdf_dir", None)
        if requested_pdf is None:
            direct_pdf = Path(library).with_suffix(".Data") / "PDF" if library else None
            guessed_pdf = (
                str(direct_pdf)
                if direct_pdf is not None and direct_pdf.is_dir()
                else default_pdf_dir(xml_path)
                if xml_path
                else ""
            )
            pdf_dir = _absolute(guessed_pdf)
        else:
            pdf_dir = _absolute(requested_pdf)
        _validate_non_interactive_paths(library, xml_path, pdf_dir)
    else:
        print("Give the path to your EndNote library (.enl) or an XML export.")
        print("The library is read directly when available; XML is an optional fallback")
        print("and is useful once for learning reference-type names.\n")

        # Keep accepting an XML path at the first prompt so existing setup
        # transcripts remain useful.
        first_path = _ask("Path to your EndNote library (.enl) [optional]")
        library = first_path if first_path.lower().endswith(".enl") else ""
        xml_path = first_path if first_path and not library else ""
        if not xml_path:
            xml_path = _ask("Path to your EndNote XML export [optional]")
        if not (xml_path or library):
            print("Error: provide at least an .enl library or an XML export.", file=sys.stderr)
            sys.exit(1)
        for candidate in (xml_path, library):
            if candidate and not Path(candidate).exists():
                print(
                    f"Warning: {candidate} does not exist yet — "
                    "you can fix this later in the config file."
                )

        direct_pdf = Path(library).with_suffix(".Data") / "PDF" if library else None
        pdf_default = (
            default_pdf_dir(xml_path)
            if xml_path
            else (str(direct_pdf) if direct_pdf and direct_pdf.is_dir() else "")
        )
        pdf_dir = _ask("PDF attachment folder (<Library>.Data/PDF)", pdf_default)
        if not library and xml_path:
            library = _ask(
                "EndNote library (.enl), for reading its groups", default_library_path(xml_path)
            )

    library, xml_path, pdf_dir = _absolute(library), _absolute(xml_path), _absolute(pdf_dir)
    config = Config(
        endnote_xml=xml_path,
        pdf_dir=pdf_dir,
        library_path=library,
        db_path=_absolute(getattr(args, "db_path", None))
        or str(library_data_dir(library or xml_path, config_dir()) / "library.db"),
        max_pdf_chars=getattr(args, "max_pdf_chars", 20000),
        watch=not getattr(args, "no_watch", False),
    )
    if config.max_pdf_chars <= 0:
        raise ValueError("--max-pdf-chars must be greater than zero")
    path = save_config(config)
    print(f"\nConfig written to {path}")

    should_index = not getattr(args, "skip_index", False)
    if should_index and (
        (xml_path and Path(xml_path).exists()) or (library and Path(library).exists())
    ):
        print("Indexing references...")
        conn = _connect_index(config)
        stats = build_index(xml_path, conn, full=True, library_path=library)
        print(
            f"  indexed {stats['total']} references from the "
            f"{'library itself' if stats['source'] == 'library' else 'XML export'}"
            f" ({stats['added']} added)."
        )

        if library:
            try:
                group_stats = index_groups(conn, library)
                print(
                    f"  read {group_stats['groups']} EndNote groups"
                    f" ({group_stats['memberships']} memberships)."
                )
            except (OSError, RuntimeError) as exc:
                print(f"  could not read groups: {exc}")

        # Text extraction is left to an explicit command: it runs at roughly
        # half a second per file, and a wizard that silently blocks for
        # several minutes looks like it has hung.
        attachments = pdfmod.count_attachments(conn)
        if attachments and not pdfmod.backend_available():
            print(f"  {attachments} PDF attachments found. {pdfmod.PDF_EXTRA_HINT}")
        elif attachments:
            print(f"  {attachments} PDF attachments found. To make their text searchable, run:")
            print(f"      endnotex9-toolkit index-pdfs      (~{attachments // 120 + 1} min)")

    print("\nNext: register the server with an MCP client.\n")
    print("  Codex:")
    print("    codex mcp add endnotex9 -- endnotex9-toolkit serve")
    print("\n  Claude Desktop / Claude Code:")
    print('    {"mcpServers": {"endnotex9": {"command": "endnotex9-toolkit", "args": ["serve"]}}}')
    print("\nUse the executable's absolute path if the client cannot find it on PATH.")


def _doctor_check(name: str, status: str, detail: str, **data) -> dict:
    return {"name": name, "status": status, "detail": detail, **data}


def _connect_index(config: Config, *, adopt_source: bool = False) -> sqlite3.Connection:
    conn = db.connect(config.db_path)
    try:
        if adopt_source:
            sources.adopt_legacy_index(conn, config.endnote_xml, config.library_path)
        else:
            sources.validate_index_source(conn, config.endnote_xml, config.library_path)
    except Exception:
        conn.close()
        raise
    return conn


def doctor_report() -> dict:
    """Inspect configuration and index readiness without changing either."""
    path = config_path()
    checks: list[dict] = []
    if not path.is_file():
        checks.append(
            _doctor_check(
                "config",
                "fail",
                f"No configuration found at {path}; run {setup_command()}",
            )
        )
        return {"ok": False, "config_path": str(path), "checks": checks}

    checks.append(_doctor_check("config", "pass", f"Configuration found at {path}"))
    try:
        config = load_config()
    except Exception as exc:
        checks.append(_doctor_check("config_parse", "fail", str(exc)))
        return {"ok": False, "config_path": str(path), "checks": checks}

    library = Path(config.library_path).expanduser() if config.library_path else None
    direct_rdb = sources.library_rdb(config.library_path)
    direct_readable = library is not None and library.is_file() and direct_rdb is not None
    if library is None:
        checks.append(_doctor_check("library", "skip", "No .enl library configured"))
    elif not library.is_file():
        checks.append(_doctor_check("library", "fail", f"Library file is missing: {library}"))
    elif direct_rdb is None:
        checks.append(
            _doctor_check("library", "fail", f"Library rdb tables are missing beside {library}")
        )
    else:
        checks.append(_doctor_check("library", "pass", f"Direct X9 tables found at {direct_rdb}"))
    if library is not None and libraryio.recovery_dir(library).exists():
        checks.append(
            _doctor_check(
                "writeback_recovery",
                "fail",
                "Interrupted writeback: close EndNote and run recover-writeback",
            )
        )

    xml_path = Path(config.endnote_xml).expanduser() if config.endnote_xml else None
    xml_readable = False
    if xml_path is None:
        checks.append(_doctor_check("xml", "skip", "No XML fallback configured"))
    elif not xml_path.is_file():
        checks.append(_doctor_check("xml", "fail", f"XML export is missing: {xml_path}"))
    else:
        try:
            parser_source = default_library_path(str(xml_path))
            xml_readable = True
            checks.append(
                _doctor_check(
                    "xml",
                    "pass",
                    f"XML export is readable: {xml_path}",
                    recorded_library=parser_source or None,
                )
            )
        except (OSError, SyntaxError, ValueError) as exc:
            checks.append(_doctor_check("xml", "fail", f"XML export is unsafe or invalid: {exc}"))

    if not direct_readable and not xml_readable:
        checks.append(_doctor_check("source", "fail", "No readable EndNote source is available"))
    else:
        checks.append(_doctor_check("source", "pass", "At least one readable source is available"))

    database = Path(config.db_path).expanduser()
    if not database.is_file():
        checks.append(
            _doctor_check(
                "index", "fail", f"Index is missing; run `endnotex9-toolkit index`: {database}"
            )
        )
    else:
        try:
            uri = database.resolve().as_uri() + "?mode=ro"
            with sqlite3.connect(uri, uri=True) as conn:
                sources.validate_index_source(conn, config.endnote_xml, config.library_path)
                schema_row = conn.execute(
                    "SELECT value FROM meta WHERE key='schema_version'"
                ).fetchone()
                count = conn.execute("SELECT COUNT(*) FROM refs").fetchone()[0]
                indexed_row = conn.execute(
                    "SELECT value FROM meta WHERE key='last_indexed_at'"
                ).fetchone()
            schema_version = int(schema_row[0]) if schema_row else None
            status = "pass" if schema_version == db.SCHEMA_VERSION else "fail"
            checks.append(
                _doctor_check(
                    "index",
                    status,
                    f"Index contains {count} references; schema {schema_version}",
                    references=count,
                    schema_version=schema_version,
                    expected_schema_version=db.SCHEMA_VERSION,
                    last_indexed_at=indexed_row[0] if indexed_row else None,
                )
            )
        except (OSError, sqlite3.Error, TypeError, ValueError, sources.SourceUnavailable) as exc:
            checks.append(_doctor_check("index", "fail", f"Index cannot be read: {exc}"))

    if not config.pdf_dir:
        checks.append(_doctor_check("pdf_directory", "skip", "No PDF directory configured"))
        checks.append(_doctor_check("pdf_backend", "skip", "PDF extraction is not configured"))
    else:
        pdf_dir = Path(config.pdf_dir).expanduser()
        directory_status = "pass" if pdf_dir.is_dir() else "fail"
        checks.append(
            _doctor_check(
                "pdf_directory",
                directory_status,
                f"PDF directory {'found' if pdf_dir.is_dir() else 'is missing'}: {pdf_dir}",
            )
        )
        backend = pdfmod.backend_available()
        checks.append(
            _doctor_check(
                "pdf_backend",
                "pass" if backend else "warn",
                "PyMuPDF is available" if backend else pdfmod.PDF_EXTRA_HINT,
            )
        )

    return {
        "ok": not any(check["status"] == "fail" for check in checks),
        "config_path": str(path),
        "config": {
            "library_path": config.library_path or None,
            "endnote_xml": config.endnote_xml or None,
            "pdf_dir": config.pdf_dir or None,
            "db_path": config.db_path,
            "watch": config.watch,
        },
        "checks": checks,
    }


def cmd_doctor(args: argparse.Namespace) -> None:
    report = doctor_report()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for check in report["checks"]:
            print(f"{check['status'].upper():>4}  {check['name']:<14} {check['detail']}")
        print(f"\nOverall: {'ready' if report['ok'] else 'needs attention'}")
    if not report["ok"]:
        sys.exit(1)


def cmd_index(args: argparse.Namespace) -> None:
    config = load_config()
    conn = _connect_index(config, adopt_source=getattr(args, "adopt_source", False))
    stats = build_index(config.endnote_xml, conn, full=args.full, library_path=config.library_path)
    for key, value in stats.items():
        print(f"{key:>10}: {value}")
    if config.library_path:
        try:
            for key, value in index_groups(conn, config.library_path).items():
                print(f"{key:>10}: {value}")
        except (OSError, RuntimeError) as exc:
            print(f"Warning: could not read groups: {exc}", file=sys.stderr)


def cmd_index_pdfs(args: argparse.Namespace) -> None:
    config = load_config()
    if not config.pdf_dir:
        print(f"Error: no pdf_dir set in {config_path()}.", file=sys.stderr)
        sys.exit(1)
    conn = _connect_index(config)

    def progress(done: int, total: int) -> None:
        print(f"\r  {done}/{total} attachments...", end="", flush=True)

    try:
        stats = pdfmod.index_pdfs(
            conn,
            config.pdf_dir,
            force=args.force,
            max_files=args.max_files,
            on_progress=progress,
        )
    except KeyboardInterrupt:
        # Each file is committed as it finishes, so a stop here keeps
        # everything extracted so far; re-running resumes.
        print("\nInterrupted — progress so far is saved; re-run to resume.")
        return
    print("\r" + " " * 40 + "\r", end="")
    for key, value in stats.items():
        print(f"{key:>18}: {value}")


def cmd_crosscheck(_args: argparse.Namespace) -> None:
    """Compare the direct library reader against the XML export.

    Worth running after an EndNote upgrade: the library's table format is
    reverse-engineered, so this is how a change gets noticed deliberately
    rather than by silently indexing wrong data.
    """
    config = load_config()
    if not (config.library_path and config.endnote_xml):
        print("Error: both library_path and endnote_xml must be configured.", file=sys.stderr)
        sys.exit(1)
    conn = _connect_index(config)
    rdb = sources.library_rdb(config.library_path)
    if rdb is None:
        print(f"Error: no readable library at {config.library_path}", file=sys.stderr)
        sys.exit(1)

    mapping = sources.stored_ref_type_names(conn) or sources.learn_ref_type_names(
        rdb, config.endnote_xml
    )
    report = sources.crosscheck(rdb, config.endnote_xml, mapping)
    total = report["compared"]
    print(f"XML export : {report['records_in_xml']} references")
    print(f"library    : {report['records_in_library']} references")
    print(f"compared   : {total}")
    if report["only_in_xml"]:
        print(f"  only in the export : {report['only_in_xml']}")
    if report["only_in_library"]:
        print(f"  only in the library: {report['only_in_library']}")

    disagreeing = {f: c for f, c in report["agreement"].items() if c != total}
    if not disagreeing:
        print(f"\nAll {len(report['agreement'])} fields agree on all {total} records.")
        return
    print("\nFields that disagree:")
    for field, count in sorted(disagreeing.items(), key=lambda kv: kv[1]):
        print(f"  {field:<24} {count}/{total}")
        for rec, from_xml, from_lib in report["mismatch_examples"].get(field, []):
            print(f"     [{rec}] export : {str(from_xml)[:70]!r}")
            print(f"           library: {str(from_lib)[:70]!r}")
    sys.exit(2)


def cmd_status(_args: argparse.Namespace) -> None:
    config = load_config()
    conn = _connect_index(config)
    stats = browse.library_stats(conn, top=5)
    open_library = detect_open_library()
    auto_detected = bool(open_library) and open_library == config.library_path
    print(f"config     : {config_path()}")
    print(
        f"library    : {config.library_path or '(not set)'}"
        f"  {'<- primary source' if sources.library_rdb(config.library_path) else ''}"
        f"{' (auto-detected: currently open in EndNote)' if auto_detected else ''}"
    )
    print(f"XML export : {config.endnote_xml}  <- fallback / cross-check")
    print(f"PDF folder : {config.pdf_dir or '(not set)'}")
    print(f"database   : {config.db_path}")
    print(f"last read  : {db.get_meta(conn, 'last_source') or 'never'}")
    print(f"references : {stats['total_references']}")
    print(f"groups     : {stats['groups']}")
    print(
        f"  with PDF : {stats['with_pdf_attachment']}"
        f" ({stats['pdfs_text_indexed']} text-extracted,"
        f" {stats['pdf_pages_indexed']} pages)"
    )
    print(f"years      : {stats['year_range']}")
    print(f"indexed at : {stats['last_indexed_at'] or 'never'}")
    print(f"PDF backend: {'PyMuPDF available' if pdfmod.backend_available() else 'not installed'}")


def cmd_serve(_args: argparse.Namespace) -> None:
    from .server import main as serve_main

    serve_main()


def cmd_recover_writeback(args: argparse.Namespace) -> None:
    enl = Path(args.library).expanduser().resolve()
    if enl.suffix.casefold() != ".enl":
        raise ValueError("--library must point to an EndNote .enl file")
    rdb = enl.with_suffix(".Data") / "rdb"
    with libraryio.library_lock(enl):
        writeback._ensure_unlocked([enl, rdb / "refs.MYD", rdb / "refs.MYI"])
        recovered = libraryio.recover_publication(rdb, enl)
    print(json.dumps({"library": str(enl), "recovered": recovered}))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="endnotex9-toolkit")
    sub = parser.add_subparsers(dest="command", required=True)

    p_setup = sub.add_parser("setup", help="configure a library interactively or with flags")
    p_setup.add_argument("--library", help="path to an EndNote .enl library")
    p_setup.add_argument(
        "--use-open-library", action="store_true", help="explicitly select the open EndNote library"
    )
    p_setup.add_argument("--xml", dest="endnote_xml", help="optional EndNote XML export")
    p_setup.add_argument("--pdf-dir", help="attachment directory, usually <Library>.Data/PDF")
    p_setup.add_argument("--db-path", help="custom local SQLite index path")
    p_setup.add_argument("--max-pdf-chars", type=int, default=20000)
    p_setup.add_argument("--no-watch", action="store_true", help="disable background reindexing")
    p_setup.add_argument("--skip-index", action="store_true", help="write config without indexing")
    p_setup.add_argument(
        "--non-interactive",
        action="store_true",
        help="never prompt; requires --library and/or --xml",
    )
    p_setup.set_defaults(func=cmd_setup)

    p_index = sub.add_parser("index", help="(re)build the reference index")
    p_index.add_argument("--full", action="store_true", help="wipe and rebuild from scratch")
    p_index.add_argument(
        "--adopt-source",
        action="store_true",
        help="bind a legacy index only after exact source-hash verification",
    )
    p_index.set_defaults(func=cmd_index)

    p_pdfs = sub.add_parser("index-pdfs", help="extract text from attached PDFs")
    p_pdfs.add_argument("--force", action="store_true", help="re-extract even if unchanged")
    p_pdfs.add_argument("--max-files", type=int, default=None, help="stop after N files")
    p_pdfs.set_defaults(func=cmd_index_pdfs)

    sub.add_parser(
        "crosscheck",
        help="verify the direct library reader against the XML export",
    ).set_defaults(func=cmd_crosscheck)

    sub.add_parser("status", help="show index statistics").set_defaults(func=cmd_status)
    p_doctor = sub.add_parser("doctor", help="diagnose configuration and index readiness")
    p_doctor.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    p_doctor.set_defaults(func=cmd_doctor)
    p_recover = sub.add_parser(
        "recover-writeback", help="restore an interrupted write; close EndNote first"
    )
    p_recover.add_argument("--library", required=True, help="exact .enl library to recover")
    p_recover.set_defaults(func=cmd_recover_writeback)
    sub.add_parser(
        "serve", help="start the stdio MCP server (normally launched by your MCP client)"
    ).set_defaults(func=cmd_serve)

    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        args.func(args)
    except (
        FileNotFoundError,
        SyntaxError,
        ValueError,
        pdfmod.PdfBackendMissing,
        libraryio.LibraryIOError,
        writeback.WritebackError,
        sources.SourceUnavailable,
    ) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
