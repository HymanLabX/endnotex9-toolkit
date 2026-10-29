"""Expose an EndNote library to any stdio-capable MCP client.

Tool docstrings are the only instructions the calling model gets, so they say
when to reach for a tool, not just what it does.
"""

from __future__ import annotations

import functools
import sqlite3
import sys
import threading

try:  # MCP SDK 2.x
    from mcp.server import MCPServer as _ServerClass
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:  # MCP SDK 1.x, where the same class was called FastMCP
    from mcp.server.fastmcp import FastMCP as _ServerClass
    from mcp.server.fastmcp.exceptions import ToolError

from . import (
    bibtex,
    browse,
    citation,
    classification,
    db,
    indexer,
    resolver,
    sampling,
    sources,
    writeplan,
)
from . import commit as commitmod
from . import pdf as pdfmod
from . import profile as profilemod
from . import search as searchmod
from .config import Config, load_config
from .watcher import watch_and_reindex

mcp = _ServerClass("EndNote X9 Library")


def _tool(fn):
    """Register `fn` as a tool whose failures reach the client with their message.

    Those messages are this server's instructions on failure: an unknown or ambiguous
    selector, setup not yet run, EndNote still holding the library, a field that cannot be
    written. From MCP SDK 2.3 any exception but `ToolError` counts as a crash and the client
    sees only "Error executing tool <name>", so every failure is raised as a `ToolError`.
    """

    @functools.wraps(fn)
    def run(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ToolError:
            raise
        except Exception as exc:
            raise ToolError(str(exc) or repr(exc)) from exc

    return mcp.tool()(run)


_conn: sqlite3.Connection | None = None
_config: Config | None = None
# The SDK dispatches synchronous tool handlers onto a worker thread pool, so
# two first calls can race here. Without this, both open a connection and one
# is dropped without ever being closed.
_INIT_LOCK = threading.Lock()


def _get_config() -> Config:
    global _config
    if _config is None:
        with _INIT_LOCK:
            if _config is None:
                _config = load_config()
    return _config


def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        config = _get_config()
        with _INIT_LOCK:
            if _conn is None:
                _conn = db.connect(config.db_path)
    return _conn


def _resolve(
    rec_number: int | None = None,
    title: str | None = None,
    doi: str | None = None,
) -> dict:
    return resolver.resolve_reference(_get_conn(), rec_number=rec_number, title=title, doi=doi)


def _resolve_batch(
    rec_numbers: list[int] | None,
    references: list[resolver.ReferenceSelector] | None,
) -> list[dict]:
    if rec_numbers is not None and references is not None:
        raise ValueError("Provide either rec_numbers or references, not both.")
    if references is not None:
        return resolver.resolve_many(_get_conn(), references)
    if not rec_numbers:
        raise ValueError("Provide rec_numbers or references.")
    return resolver.resolve_many(
        _get_conn(), [{"rec_number": rec_number} for rec_number in rec_numbers]
    )


# --------------------------------------------------------------------------
# finding things
# --------------------------------------------------------------------------


@_tool
def search_references(
    queries: list[str],
    limit: int = 20,
    year_from: int | None = None,
    year_to: int | None = None,
    ref_type: str | None = None,
    language: str | None = None,
    has_pdf: bool | None = None,
    group: str | None = None,
    topic: str | None = None,
) -> list[dict]:
    """Search the library's titles, authors, abstracts and keywords.

    Pass several phrasings in `queries` to widen recall — in particular, if
    the user asks in Chinese, ALSO include an English translation (and vice
    versa), since the library holds literature in both. Results are merged
    and deduplicated, and records matching every term of a query rank above
    records matching only some.

    Each hit carries an `excerpt` showing the matched context and a
    `has_pdf` flag. Use `get_reference_details` for the full abstract, and
    `search_fulltext` when the answer is likely inside a paper rather than
    in its metadata.

    Optional filters: `year_from`/`year_to` (inclusive), `ref_type` (e.g.
    "Journal Article"), `language` (ISO code, e.g. "en" or "zh"), `has_pdf`,
    `group` (an EndNote group name — see `list_groups`), and `topic` (a topic
    label — see `list_topics`; a prefix like "204->" is enough).

    `limit` is capped at 200. To work through a larger set, narrow the query
    or use `list_references`, which reports `total_matching` and paginates.
    """
    return searchmod.search_references(
        _get_conn(),
        queries,
        limit=limit,
        year_from=year_from,
        year_to=year_to,
        ref_type=ref_type,
        language=language,
        has_pdf=has_pdf,
        group=group,
        topic=topic,
    )


@_tool
def list_references(
    year_from: int | None = None,
    year_to: int | None = None,
    author: str | None = None,
    journal: str | None = None,
    keyword: str | None = None,
    ref_type: str | None = None,
    language: str | None = None,
    label: str | None = None,
    has_pdf: bool | None = None,
    group: str | None = None,
    topic: str | None = None,
    sort: str = "year_desc",
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """Browse the library by filter rather than by search terms.

    Use this for "what do I have by X", "everything since 2020", "papers in
    Acta Materialia", "what's in my Ni-Ti group" — questions about
    membership, not relevance. Substring filters are case-insensitive.
    `sort` is one of year_desc, year_asc, title, added_desc. The reply
    includes `total_matching`, so paginate with `offset` when it exceeds what
    you asked for. `limit` is capped at 200; when a larger value was asked
    for, the reply carries `limit_capped`.
    """
    return browse.list_references(
        _get_conn(),
        year_from=year_from,
        year_to=year_to,
        author=author,
        journal=journal,
        keyword=keyword,
        ref_type=ref_type,
        language=language,
        label=label,
        has_pdf=has_pdf,
        group=group,
        topic=topic,
        sort=sort,
        limit=limit,
        offset=offset,
    )


@_tool
def find_related(
    rec_number: int | None = None,
    limit: int = 10,
    title: str | None = None,
    doi: str | None = None,
) -> list[dict]:
    """Find references that overlap a given one in title and keyword terms.

    A term-overlap neighbourhood, not a semantic one — good for "more like
    this" after a promising hit, and for spotting duplicates.
    """
    ref = _resolve(rec_number=rec_number, title=title, doi=doi)
    return searchmod.find_related(_get_conn(), ref["rec_number"], limit=limit)


@_tool
def list_groups(include_empty: bool = True) -> list[dict]:
    """List the library's own EndNote groups, with their sizes.

    These are the folders in EndNote's sidebar — the user's existing manual
    classification — plus any smart groups, whose saved search is reported as
    `query`. Read this before proposing any classification scheme: the user
    very likely already has one, and it should be extended rather than
    replaced.

    Pass a group's name as the `group` filter of `search_references` or
    `list_references` to work inside it.
    """
    return db.list_groups(_get_conn(), include_empty=include_empty)


@_tool
def list_topics() -> dict:
    """List the topic labels assigned to references, with their counts.

    Topics live in EndNote's Custom 1 field. Unlike `list_groups` — which
    reports the folders the user made by hand — these are labels this tool
    wrote, so they cover the whole library evenly. A record can carry several,
    joined by "; ": a material system plus cross-cutting tags. Pass any label,
    or just its numeric prefix, as the `topic` filter of `search_references`
    or `list_references`.
    """
    return browse.topic_counts(_get_conn())


@_tool
def library_stats(top: int = 15) -> dict:
    """Summarise the whole library: size, year range, metadata coverage,
    reference types, languages, and the most frequent journals, authors and
    keywords.

    Worth calling before a broad search, to learn what the library actually
    covers and how much of it has PDF text available.
    """
    return browse.library_stats(_get_conn(), top=top)


@_tool
def profile_library(top: int = 15, min_df: int = 2, max_terms: int = 50) -> dict:
    """Profile the corpus before proposing a classification taxonomy.

    Returns domain-neutral evidence: metadata coverage, keyword document
    frequency, candidate phrases, keyword co-occurrence, journal/group
    enrichment, and likely import-noise terms. Call this before sampling or
    inventing categories; it is deterministic and does not modify EndNote.
    """
    return profilemod.profile_library(_get_conn(), top=top, min_df=min_df, max_terms=max_terms)


@_tool
def sample_references(
    session_id: str | None = None,
    strategy: str = "random",
    size: int = 20,
    seed: int = 0,
    char_budget: int = 20000,
    group: str | None = None,
    journal: str | None = None,
) -> dict:
    """Return a reproducible, UTF-8-budgeted batch for taxonomy design or audit.

    Strategies: `random`, `group`, `journal`, and `uncertain`. When a
    classification `session_id` is supplied, sampling is bound to that
    session's library fingerprint and fails if the indexed library changed.
    `uncertain` requires an evaluated session and samples its unresolved or
    conflicting records. Each record has a fixed title/abstract/keywords/
    journal/year/type/authors/groups shape.
    """
    fingerprint = None
    uncertain = None
    if session_id:
        session = classification.get_session(_get_conn(), session_id)
        fingerprint = session["library_fingerprint"]
        if strategy == "uncertain":
            if session["status"] not in {"evaluated", "auditing", "audited"}:
                raise ValueError("uncertain sampling requires an evaluated classification session")
            uncertain = [
                item["rec_number"]
                for item in classification.list_unresolved(_get_conn(), session_id)
            ]
    return sampling.sample_references(
        _get_conn(),
        session_id=session_id,
        strategy=strategy,
        size=size,
        seed=seed,
        char_budget=char_budget,
        group=group,
        journal=journal,
        snapshot_fingerprint=fingerprint,
        evaluate=uncertain,
    )


# --------------------------------------------------------------------------
# domain-neutral classification workflow
# --------------------------------------------------------------------------


@_tool
def create_classification_session(seed: int = 0, char_budget: int = 20000) -> dict:
    """Start a versioned classification session for the current library snapshot.

    Call `profile_library` and `sample_references` first. The session records a
    library fingerprint, so later evaluation refuses to run against a changed
    index. Creating a session writes only the local SQLite index, never EndNote.
    """
    return classification.create_session(
        _get_conn(),
        None,
        seed=seed,
        budgets={"char_budget": char_budget},
    )


@_tool
def get_classification_session(session_id: str) -> dict:
    """Read the current state and immutable version hashes of a session."""
    return classification.get_session(_get_conn(), session_id)


@_tool
def save_classification_taxonomy(session_id: str, taxonomy: dict) -> dict:
    """Save a draft facet taxonomy.

    In this first general release exactly one facet may be approved for
    eventual EndNote preview: `mode="single"`, `slot="custom_1"`. Other
    facets may remain unapproved for analysis. Saving is not approval; call
    `approve_classification_taxonomy` only after the user reviews the tree.
    """
    return classification.save_taxonomy(_get_conn(), session_id, taxonomy)


@_tool
def approve_classification_taxonomy(session_id: str, taxonomy_hash: str) -> dict:
    """Approve the exact taxonomy hash the user reviewed.

    This is the explicit Phase-2 human gate. Approval is immutable and is
    required before rules can be saved.
    """
    return classification.approve_taxonomy(_get_conn(), session_id, taxonomy_hash)


@_tool
def save_classification_rules(session_id: str, ruleset: dict) -> dict:
    """Save bounded declarative rules for an approved taxonomy.

    Supported predicates are contains_any, contains_all, prefix, token and
    year_range over bibliographic fields. Arbitrary Python, SQL and regex are
    rejected. Save returns a hash; freeze that exact hash before evaluation.
    """
    return classification.save_ruleset(_get_conn(), session_id, ruleset)


@_tool
def freeze_classification_rules(session_id: str, ruleset_hash: str) -> dict:
    """Freeze the exact rule version to make evaluation reproducible."""
    return classification.freeze_ruleset(_get_conn(), session_id, ruleset_hash)


@_tool
def evaluate_classification_rules(session_id: str) -> dict:
    """Evaluate the approved taxonomy and frozen rules over the library.

    Stores rule evidence plus assigned/unresolved/conflict status in SQLite.
    Repeating the same immutable inputs reuses the same evaluation hash.
    EndNote is never modified.
    """
    return classification.evaluate_rules(
        _get_conn(),
        session_id,
        library_fingerprint=None,
    )


@_tool
def list_unresolved_classifications(session_id: str) -> list[dict]:
    """List unresolved and conflicting records for LLM or human long-tail review."""
    return classification.list_unresolved(_get_conn(), session_id)


@_tool
def create_classification_audit(
    session_id: str,
    size: int = 50,
    seed: int | None = None,
    holdout: bool = True,
) -> dict:
    """Create a reproducible stratified audit sample not used by prior audits."""
    return classification.create_audit_sample(
        _get_conn(), session_id, size=size, seed=seed, holdout=holdout
    )


@_tool
def submit_classification_audit(audit_set_id: str, labels: list[dict]) -> int:
    """Submit human-reviewed labels for an audit set.

    Each item needs `rec_number` and `label`; `facet` is optional for the
    approved single facet. Existing labels are immutable.
    """
    return classification.submit_audit_labels(_get_conn(), audit_set_id, labels)


@_tool
def get_classification_report(audit_set_id: str) -> dict:
    """Report audit-set metrics only: coverage, conflicts and per-label scores.

    The result deliberately does not claim whole-library accuracy.
    """
    return classification.classification_report(_get_conn(), audit_set_id)


@_tool
def prepare_classification_writeback(session_id: str, idempotency_key: str) -> dict:
    """Prepare a read-only Custom 1 writeback diff after audit completion.

    The plan hashes `.enl`, refs.MYD, refs.MYI and misc.MYD and persists only
    a SQLite preview. It does not create a backup, call the dangerous writer,
    replace a file, edit smart groups, or expose a commit capability.
    Conflicts, abstentions, source drift and incomplete coverage fail closed.
    """
    config = _get_config()
    rdb = sources.library_rdb(config.library_path)
    if rdb is None or not config.library_path:
        raise ValueError("A readable .enl library is required to prepare a writeback preview.")
    return writeplan.prepare_writeback(
        _get_conn(), session_id, rdb, config.library_path, idempotency_key
    )


# --------------------------------------------------------------------------
# reading
# --------------------------------------------------------------------------


@_tool
def get_reference_details(
    rec_number: int | None = None,
    title: str | None = None,
    doi: str | None = None,
) -> dict:
    """Full metadata for one reference selected by record number, title, or DOI.

    Provide exactly one selector. Title matching prefers an exact match;
    ambiguous titles or duplicate DOIs return candidates instead of choosing.
    """
    return _resolve(rec_number=rec_number, title=title, doi=doi)


@_tool
def search_fulltext(queries: list[str], limit: int = 15, max_pages_per_ref: int = 3) -> list[dict]:
    """Search inside the text of attached PDFs, not just their metadata.

    Use this for claims, methods, numbers or phrases that would appear in a
    paper's body but never in its abstract. Each hit reports the pages that
    matched and an excerpt from each, which `read_pdf_pages` can then expand.

    Only covers PDFs already extracted by `index_pdfs`; `library_stats`
    reports how many that is.
    """
    return searchmod.search_fulltext(
        _get_conn(), queries, limit=limit, max_pages_per_ref=max_pages_per_ref
    )


@_tool
def read_pdf_pages(
    rec_number: int | None = None,
    title: str | None = None,
    first_page: int = 1,
    last_page: int | None = None,
    doi: str | None = None,
) -> dict:
    """Read extracted PDF text by record number, title, or DOI.

    Provide exactly one selector. Prefer `title` or `doi` when the user gives
    either; use `rec_number` when a precise internal EndNote record is known.

    Output is capped; when the reply says it was truncated, ask for a
    narrower range rather than re-reading from page 1.
    """
    ref = _resolve(rec_number=rec_number, title=title, doi=doi)
    return pdfmod.read_pages(
        _get_conn(),
        ref["rec_number"],
        first_page=first_page,
        last_page=last_page,
        max_chars=_get_config().max_pdf_chars,
    )


# --------------------------------------------------------------------------
# citing
# --------------------------------------------------------------------------


@_tool
def get_citation(
    rec_number: int | None = None,
    style: str = "apa7",
    title: str | None = None,
    doi: str | None = None,
) -> str:
    """Format one reference as a citation.

    Styles: `apa7` (APA 7th) and `gbt7714` (GB/T 7714-2015, the Chinese
    national standard used by mainland journals and theses).
    """
    return citation.format_citation(
        _resolve(rec_number=rec_number, title=title, doi=doi), style=style
    )


@_tool
def get_bibliography(
    rec_numbers: list[int] | None = None,
    style: str = "apa7",
    references: list[resolver.ReferenceSelector] | None = None,
) -> str:
    """Format several references as a reference list.

    Use legacy `rec_numbers`, or `references` for an ordered mixed list such
    as `[{'title': '...'}, {'doi': '...'}]`. GB/T 7714 entries are numbered
    in the order given; APA entries are sorted alphabetically.
    """
    refs = _resolve_batch(rec_numbers, references)
    return citation.format_bibliography(refs, style=style)


@_tool
def get_bibtex(
    rec_numbers: list[int] | None = None,
    include_abstract: bool = False,
    references: list[resolver.ReferenceSelector] | None = None,
) -> str:
    """Export references as BibTeX, ready to paste into a .bib file.

    Use legacy `rec_numbers`, or `references` for mixed record-number/title/
    DOI selectors. Citation keys reuse EndNote's Label field when set;
    otherwise a key is derived from author, year and title.
    """
    refs = _resolve_batch(rec_numbers, references)
    return bibtex.format_bibtex(refs, include_abstract=include_abstract)


# --------------------------------------------------------------------------
# writing back
# --------------------------------------------------------------------------


def _get_rdb():
    config = _get_config()
    rdb = sources.library_rdb(config.library_path)
    if rdb is None or not config.library_path:
        raise ValueError("A readable .enl library is required to write to EndNote.")
    return config, rdb


def _reindex_after_write(config: Config) -> None:
    conn = _get_conn()
    with db.connection_lock(conn):
        indexer.build_index(config.endnote_xml, conn, full=False, library_path=config.library_path)


@_tool
def update_reference_fields(updates: list[dict], dry_run: bool = True) -> dict:
    """Fill in or correct text fields, the author list, or the keyword list.

    Each item of `updates` needs `rec_number` plus one or more fields.
    Plain-text fields take a string: `volume`, `issue`, `pages`,
    `article_number`, `number_of_volumes`, `journal`, `notes`, `title`,
    `abstract`, `doi` (the bare DOI, e.g. `10.1016/j.actamat.2020.106545`).
    `author` and `keywords` take a list of strings and replace
    the whole list, e.g. `[{"rec_number": 42, "volume": "14", "issue": "3",
       "author": ["Zerovnik, Andrej", "Dall'Olio, Stefano"]}]`.
    No other field may be set this way — `year` cannot be changed at all
    (it is a MyISAM index key; writing it needs B-tree surgery this server
    does not implement), and every column not named above stays read-only.

    Defaults to `dry_run=True`, which returns the diff (old value vs. new
    value for every field requested) without touching EndNote. Call again
    with `dry_run=False` only after the diff has been shown to the user and
    they have agreed to it — that call backs up the whole library first (see
    `backup_path` in the result) and only writes fields that actually
    change. Always prefer a source the user trusts (e.g. a DOI lookup, or
    text already present elsewhere on the same record) for the new values;
    never invent bibliographic data.

    **EndNote must be closed before a `dry_run=False` call.** This writes
    the library's files directly; if EndNote still has them open, the write
    can fail outright or, worse, be silently overwritten later by EndNote's
    own stale in-memory copy. A best-effort lock check refuses the write if
    the files look open elsewhere, but tell the user to close EndNote first
    rather than relying on that check alone — ask them to confirm it is
    closed before making this call.

    Writing `author`/`journal`/`keywords` leaves EndNote's own term lists
    (Tools/References → Define Term Lists) stale until the user runs their
    "Update List" command there — this tool never touches those lists
    itself, by design; see SECURITY.md.
    """
    config, rdb = _get_rdb()
    parsed: dict[int, dict[str, object]] = {}
    for index, item in enumerate(updates):
        if not isinstance(item, dict) or "rec_number" not in item:
            raise ValueError(f"updates[{index}] must be an object with a rec_number")
        rec_number = int(item["rec_number"])
        fields = {k: v for k, v in item.items() if k != "rec_number"}
        if not fields:
            raise ValueError(f"updates[{index}] (rec_number={rec_number}) has no fields to write")
        # Splitting one record across several entries is a reasonable thing
        # for a model to emit, so merge them. But a field given two different
        # values is a contradiction, and keeping the last one would silently
        # drop half of what the user is about to approve in the dry-run diff.
        target = parsed.setdefault(rec_number, {})
        for field, value in fields.items():
            if field in target and target[field] != value:
                raise ValueError(
                    f"updates[{index}] sets {field!r} on rec_number={rec_number} to "
                    f"{value!r}, but an earlier entry set it to {target[field]!r}; "
                    "give each field one value"
                )
            target[field] = value
    result = commitmod.update_reference_fields(rdb, config.library_path, parsed, dry_run=dry_run)
    if not dry_run and result["records_written"]:
        _reindex_after_write(config)
    return result


@_tool
def mark_references(
    rec_numbers: list[int],
    marker: str,
    column: str = "custom_7",
    force: bool = False,
    dry_run: bool = True,
) -> dict:
    """Tag references with a marker so the user can bulk-act on them in EndNote.

    Writes `marker` into `column` (one of custom_2..custom_7 — custom_1 is
    reserved for this server's topic classification) for every listed
    record. This never deletes anything: the intended workflow is to mark
    candidates (e.g. the losing half of a duplicate pair, or a "no PDF"
    filter), then have the user create an EndNote Smart Group such as
    "Custom 7 contains DELETE_CANDIDATE" and delete the matches themselves
    inside EndNote, where the action is visible and reversible from the
    trash.

    Defaults to `dry_run=True`, returning how many records would be marked
    and any conflicts (records whose column already holds different,
    non-empty text) without writing. Call again with `dry_run=False` after
    the user has agreed; pass `force=True` only if they explicitly want to
    overwrite existing text in that column. Backs up the whole library
    first — see `backup_path` in the result.

    **EndNote must be closed before a `dry_run=False` call** — same reason
    as `update_reference_fields`: confirm with the user that EndNote is
    closed before making this call, rather than relying solely on the
    best-effort lock check.
    """
    config, rdb = _get_rdb()
    result = commitmod.mark_records(
        rdb, config.library_path, rec_numbers, marker, column=column, force=force, dry_run=dry_run
    )
    if not dry_run and result["records_written"]:
        _reindex_after_write(config)
    return result


# --------------------------------------------------------------------------
# maintenance
# --------------------------------------------------------------------------


@_tool
def rebuild_index(full: bool = False) -> dict:
    """Re-index the library.

    Reads the EndNote library's own tables when configured, so edits made in
    EndNote are picked up with no export step; falls back to the XML export
    otherwise. The reply's `source` says which was used. Groups are refreshed
    at the same time. Incremental by default; `full=True` wipes and rebuilds
    everything, including extracted PDF text.
    """
    config = _get_config()
    conn = _get_conn()
    with db.connection_lock(conn):
        stats = indexer.build_index(
            config.endnote_xml, conn, full=full, library_path=config.library_path
        )
        if config.library_path:
            try:
                stats["group_refresh"] = indexer.index_groups(conn, config.library_path)
            except (OSError, RuntimeError) as exc:
                # A locked or half-written library shouldn't fail the whole
                # re-index; the references are the important half.
                stats["group_refresh"] = {"error": str(exc)}
    return stats


@_tool
def index_pdfs(
    rec_numbers: list[int] | None = None,
    force: bool = False,
    max_files: int | None = None,
    references: list[resolver.ReferenceSelector] | None = None,
) -> dict:
    """Extract text from attached PDFs so `search_fulltext` can reach it.

    Incremental: files whose size and timestamp are unchanged are skipped.
    Pass legacy `rec_numbers`, or `references` containing mixed record-number/
    title/DOI selectors, to process only certain references. Use `max_files`
    for a first run in batches.
    """
    config = _get_config()
    if not config.pdf_dir:
        raise ValueError(
            "No pdf_dir configured. Set it in the config file (usually "
            "<Library>.Data/PDF) and try again."
        )
    if rec_numbers is not None and references is not None:
        raise ValueError("Provide either rec_numbers or references, not both.")
    selected = rec_numbers
    if references is not None:
        selected = [ref["rec_number"] for ref in resolver.resolve_many(_get_conn(), references)]
    conn = _get_conn()
    with db.connection_lock(conn):
        return pdfmod.index_pdfs(
            conn, config.pdf_dir, rec_numbers=selected, force=force, max_files=max_files
        )


def main() -> None:
    try:
        config = _get_config()
    except FileNotFoundError as exc:
        # Start regardless. A server that exits here reaches the user as a bare "connection
        # closed" in their agent, with nothing saying why. One that stays up puts the same
        # message in front of them on the first tool call, and serves the library as soon as
        # setup writes a configuration, since `_get_config` retries until a load succeeds.
        # Only the background re-index watcher waits for the next launch.
        print(f"endnotex9-toolkit: {exc}", file=sys.stderr)
    else:
        if config.watch:
            watch_and_reindex(config.endnote_xml, config.db_path, library_path=config.library_path)
    mcp.run()


if __name__ == "__main__":
    main()
