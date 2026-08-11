"""Check a review draft against the corpus it claims to review.

A review written from a library slice fails in one direction far more often
than the other: not by getting a record wrong, but by citing something that
was never in the slice at all -- a record number carried over from an
earlier session, a digit typed wrong, or a paper the model knows from the
field and attaches a plausible number to. All three read perfectly. None of
them survive a set membership test, which is what this script is.

It answers four questions about a draft and a corpus file:

* which cited record numbers are NOT in the corpus       -> fatal, exit 1
* which corpus records the draft never cites             -> the coverage ledger
* which prose blocks carry no citation at all            -> a heuristic, for review
* what the citation order is, for a numbered bibliography -> --renumber

It reads two files and writes at most one. It never touches the EndNote
library, imports nothing from `endnotex9_mcp`, and makes no network calls;
the whole thing is a function of (draft text, corpus list).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# `[#1204]`, `[#1204, #88]`, `[#1204; #88]`. The `#` is what keeps this from
# matching a bibliography's own `[1]` markers, so a renumbered draft can be
# fed back in without the numbers being mistaken for record numbers.
CITATION = re.compile(r"\[#\s*(\d+(?:\s*[,;]\s*#?\s*\d+)*)\s*\]")
_DIGITS = re.compile(r"\d+")
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
_FENCE = re.compile(r"^\s*(?:```|~~~)")
# Anything with a letter, digit or CJK character in it. Used to skip blocks
# that are only a horizontal rule or a row of punctuation.
_SUBSTANTIVE = re.compile(r"[^\W_]", re.UNICODE)

# Sections where an uncited paragraph is the norm rather than a finding.
# Matched case-insensitively against the heading text, and inherited by any
# deeper heading beneath them.
DEFAULT_EXEMPT = (
    r"reference|bibliograph|works cited|参考文献|文献列表",
    r"coverage|ledger|覆盖|清单",
    r"scope|corpus|语料|检索|范围",
    r"appendix|附录|acknowledg|致谢",
)


class AuditError(RuntimeError):
    """A corpus file that cannot be read as a record list. Always fatal."""


# --------------------------------------------------------------------------
# the corpus
# --------------------------------------------------------------------------


def parse_corpus(data: object) -> tuple[dict[int, dict], list[int]]:
    """`({rec_number: record}, duplicates)` from whatever shape was saved.

    Accepts the reply of `list_references` as-is, a hand-assembled list of
    record dicts, or a bare list of record numbers -- the three things that
    actually end up in a `corpus.json`. Duplicates are reported rather than
    raised: a corpus unioned from several calls collects them honestly, and
    silently keeping both would inflate every count downstream.
    """
    if isinstance(data, dict):
        for key in ("references", "records", "corpus"):
            value = data.get(key)
            if isinstance(value, list):
                data = value
                break
        else:
            raise AuditError(
                "the corpus file is an object with no `references`, `records` or `corpus` list; "
                "save the list_references reply as-is, or write a list of records"
            )
    if not isinstance(data, list):
        raise AuditError("the corpus file must hold a list of records")
    if not data:
        raise AuditError("the corpus file holds no records")

    records: dict[int, dict] = {}
    duplicates: list[int] = []
    for position, entry in enumerate(data):
        if isinstance(entry, dict):
            raw = entry.get("rec_number", entry.get("rec"))
            record = dict(entry)
        elif isinstance(entry, (int, str)):
            raw = entry
            record = {}
        else:
            raise AuditError(f"corpus entry {position} is neither a record nor a record number")
        if raw is None or isinstance(raw, bool):
            raise AuditError(f"corpus entry {position} has no rec_number")
        try:
            rec_number = int(str(raw).strip())
        except ValueError as exc:
            raise AuditError(
                f"corpus entry {position} has a non-numeric rec_number {raw!r}"
            ) from exc
        record["rec_number"] = rec_number
        if rec_number in records:
            duplicates.append(rec_number)
            continue
        records[rec_number] = record
    return records, duplicates


def _depth(record: dict) -> str:
    """The reading depth recorded for a record, or `unread` if none was."""
    value = str(record.get("depth") or "").strip().lower()
    return value if value in {"fulltext", "abstract", "metadata"} else "unrecorded"


# --------------------------------------------------------------------------
# the draft
# --------------------------------------------------------------------------


def find_citations(draft: str) -> list[dict]:
    """Every citation marker in order, with its line number and records."""
    found = []
    for match in CITATION.finditer(draft):
        found.append(
            {
                "line": draft.count("\n", 0, match.start()) + 1,
                "text": match.group(0),
                "rec_numbers": [int(n) for n in _DIGITS.findall(match.group(1))],
            }
        )
    return found


def cited_order(draft: str) -> list[int]:
    """Record numbers in order of first appearance, deduplicated."""
    order: list[int] = []
    seen: set[int] = set()
    for citation in find_citations(draft):
        for rec_number in citation["rec_numbers"]:
            if rec_number not in seen:
                seen.add(rec_number)
                order.append(rec_number)
    return order


def _is_exempt(heading: str, patterns: tuple[str, ...]) -> bool:
    lowered = heading.lower()
    return any(re.search(pattern, lowered) for pattern in patterns)


def iter_blocks(draft: str, exempt: tuple[str, ...] = DEFAULT_EXEMPT):
    """Prose blocks of the draft, with the heading each one sits under.

    A block is a run of non-blank lines. Fenced code is skipped entirely and
    heading lines are not blocks themselves. Anything before the *second*
    heading is marked `front_matter`: the first heading is the document
    title, and what sits under it is the scope and depth line, which has
    nothing to cite. The body starts where the second heading does.
    """
    lines = draft.splitlines()
    heading: str | None = None
    headings_seen = 0
    exempt_level: int | None = None
    in_code = False
    block: list[str] = []
    start = 0

    def flush():
        nonlocal block, start
        if block:
            yielded = {
                "line": start,
                "heading": heading,
                "exempt": exempt_level is not None,
                "front_matter": headings_seen <= 1,
                "text": "\n".join(block),
            }
            block = []
            return yielded
        return None

    for number, line in enumerate(lines, start=1):
        if _FENCE.match(line):
            done = flush()
            if done:
                yield done
            in_code = not in_code
            continue
        if in_code:
            continue
        match = _HEADING.match(line)
        if match:
            done = flush()
            if done:
                yield done
            level = len(match.group(1))
            text = match.group(2)
            # A subheading inside an exempt section stays exempt; a heading at
            # the same level or shallower ends the section.
            if exempt_level is None or level <= exempt_level:
                exempt_level = level if _is_exempt(text, exempt) else None
            heading = text
            headings_seen += 1
            continue
        if not line.strip():
            done = flush()
            if done:
                yield done
            continue
        if not block:
            start = number
        block.append(line)

    done = flush()
    if done:
        yield done


def blocks_without_citation(draft: str, exempt: tuple[str, ...] = DEFAULT_EXEMPT) -> list[dict]:
    """Body blocks carrying no marker -- a heuristic, with honest false positives.

    Section framing, transitions and the author's own synthesis legitimately
    carry none. The point is to make the model look at each one, not to
    forbid them.
    """
    flagged = []
    for block in iter_blocks(draft, exempt):
        if block["exempt"] or block["front_matter"]:
            continue
        if not _SUBSTANTIVE.search(block["text"]):
            continue
        if CITATION.search(block["text"]):
            continue
        preview = block["text"].splitlines()[0].strip()
        flagged.append(
            {
                "line": block["line"],
                "heading": block["heading"],
                "preview": preview[:96] + ("…" if len(preview) > 96 else ""),
            }
        )
    return flagged


# --------------------------------------------------------------------------
# the audit
# --------------------------------------------------------------------------


def audit(draft: str, records: dict[int, dict], exempt: tuple[str, ...] = DEFAULT_EXEMPT) -> dict:
    """The full report. `ok` is false only for citations outside the corpus."""
    order = cited_order(draft)
    cited = [rec for rec in order if rec in records]
    unknown = [rec for rec in order if rec not in records]
    cited_set = set(cited)
    first_line: dict[int, int] = {}
    for citation in find_citations(draft):
        for rec_number in citation["rec_numbers"]:
            first_line.setdefault(rec_number, citation["line"])

    uncited = [
        {
            "rec_number": rec_number,
            "title": record.get("title"),
            "year": record.get("year"),
            "depth": _depth(record),
        }
        for rec_number, record in sorted(records.items())
        if rec_number not in cited_set
    ]

    corpus_by_depth: dict[str, int] = {}
    cited_by_depth: dict[str, int] = {}
    for rec_number, record in records.items():
        depth = _depth(record)
        corpus_by_depth[depth] = corpus_by_depth.get(depth, 0) + 1
        if rec_number in cited_set:
            cited_by_depth[depth] = cited_by_depth.get(depth, 0) + 1

    return {
        "corpus_size": len(records),
        "cited": cited,
        "unknown": [{"rec_number": rec, "line": first_line[rec]} for rec in unknown],
        "uncited": uncited,
        "coverage": {
            "corpus": len(records),
            "cited": len(cited),
            "uncited": len(uncited),
            "corpus_by_depth": corpus_by_depth,
            "cited_by_depth": cited_by_depth,
        },
        "blocks_without_citation": blocks_without_citation(draft, exempt),
        "ok": not unknown,
    }


# --------------------------------------------------------------------------
# renumbering
# --------------------------------------------------------------------------


def _run(run: list[int]) -> str:
    # Three or more consecutive numbers become a range, two do not -- `[1-2]`
    # is longer than `[1,2]` and no clearer, which is why numbered styles
    # print it the same way.
    return f"{run[0]}-{run[-1]}" if len(run) >= 3 else ",".join(str(value) for value in run)


def _collapse(numbers: list[int]) -> str:
    """`[1,2,4,5,6]` -> `1,2,4-6`, the way a numbered style prints a run."""
    parts: list[str] = []
    run = [numbers[0]]
    for value in numbers[1:]:
        if value == run[-1] + 1:
            run.append(value)
            continue
        parts.append(_run(run))
        run = [value]
    parts.append(_run(run))
    return ",".join(parts)


def renumber(draft: str) -> tuple[str, list[int]]:
    """Rewrite `[#rec]` as `[n]` in order of first appearance.

    Returns the rewritten draft and the record order behind it, which is
    exactly what `get_bibliography(references=[…], style="gbt7714")` needs:
    that style numbers entries in the order it is given them, so the two
    line up with no further bookkeeping.
    """
    order = cited_order(draft)
    position = {rec_number: index for index, rec_number in enumerate(order, start=1)}

    def replace(match: re.Match) -> str:
        numbers = sorted({position[int(n)] for n in _DIGITS.findall(match.group(1))})
        return f"[{_collapse(numbers)}]"

    return CITATION.sub(replace, draft), order


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _read(source: str) -> str:
    if source == "-":
        return sys.stdin.read()
    return Path(source).read_text(encoding="utf-8")


def _print_report(report: dict, draft_path: str) -> None:
    coverage = report["coverage"]
    print(f"draft  : {draft_path}")
    print(f"corpus : {report['corpus_size']} records")
    print(f"cited  : {coverage['cited']} of {coverage['corpus']}  ({coverage['uncited']} uncited)")
    if coverage["cited_by_depth"]:
        breakdown = ", ".join(f"{k} {v}" for k, v in sorted(coverage["cited_by_depth"].items()))
        print(f"         cited by depth: {breakdown}")
    print()

    duplicates = report.get("duplicates") or []
    if duplicates:
        listed = ", ".join(str(rec) for rec in sorted(set(duplicates)))
        print(f"! {len(duplicates)} duplicate corpus entries (kept once): {listed}")
        print()

    if report["unknown"]:
        print(f"FAIL: {len(report['unknown'])} citation(s) name a record outside the corpus")
        for item in report["unknown"]:
            print(f"  line {item['line']}: [#{item['rec_number']}]")
        print("  Every one of these is a typo, an out-of-scope citation, or an invented record.")
        print("  Fix the draft before showing it to anyone.")
        print()
    else:
        print("OK: every cited record is in the corpus")
        print()

    blocks = report["blocks_without_citation"]
    if blocks:
        print(f"{len(blocks)} block(s) carry no citation -- check each one:")
        for item in blocks[:20]:
            print(f"  line {item['line']} (under {item['heading']!r}): {item['preview']}")
        if len(blocks) > 20:
            print(f"  … and {len(blocks) - 20} more")
        print()

    if report["uncited"]:
        print(f"{len(report['uncited'])} corpus record(s) are not cited -- this is the ledger:")
        for item in report["uncited"][:20]:
            title = (item["title"] or "")[:60]
            print(f"  [#{item['rec_number']}] {item['year'] or '----'} {title} ({item['depth']})")
        if len(report["uncited"]) > 20:
            print(f"  … and {len(report['uncited']) - 20} more")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Audit a review draft's citations against its frozen corpus.",
    )
    ap.add_argument("--draft", required=True, help="the review markdown, or '-' for stdin")
    ap.add_argument("--corpus", required=True, help="corpus.json — the frozen record list")
    ap.add_argument(
        "--renumber",
        metavar="PATH",
        help="write a copy with [#rec] rewritten as [n] in citation order ('-' for stdout)",
    )
    ap.add_argument(
        "--exempt",
        action="append",
        metavar="REGEX",
        help="heading pattern whose sections are exempt from the uncited-block check; "
        "repeatable, and replaces the defaults when given",
    )
    ap.add_argument(
        "--strict",
        action="store_true",
        help="also exit non-zero when prose blocks carry no citation",
    )
    ap.add_argument("--json", action="store_true", help="emit the report as JSON on stdout")
    args = ap.parse_args()

    # CJK titles and Greek symbols are ordinary content here; the default
    # Windows console encoding is not enough for them.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    draft = _read(args.draft)
    try:
        records, duplicates = parse_corpus(
            json.loads(Path(args.corpus).read_text(encoding="utf-8"))
        )
    except json.JSONDecodeError as exc:
        raise SystemExit(f"error: {args.corpus} is not valid JSON ({exc})") from exc
    except AuditError as exc:
        raise SystemExit(f"error: {exc}") from exc

    exempt = tuple(args.exempt) if args.exempt else DEFAULT_EXEMPT
    report = audit(draft, records, exempt)
    report["duplicates"] = duplicates
    report["draft"] = args.draft

    if args.renumber:
        if report["unknown"]:
            raise SystemExit(
                "error: refusing to renumber a draft that cites records outside the corpus; "
                "fix the citations first"
            )
        rewritten, order = renumber(draft)
        if args.renumber == "-":
            sys.stdout.write(rewritten)
        else:
            Path(args.renumber).write_text(rewritten, encoding="utf-8")
        report["renumbered"] = {"path": args.renumber, "order": order}

    if args.json:
        json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    else:
        _print_report(report, args.draft)
        if "renumbered" in report:
            order = report["renumbered"]["order"]
            print()
            print(f"renumbered -> {args.renumber}")
            print("Pass this order to get_bibliography(style='gbt7714'), which numbers as given:")
            print("  " + json.dumps([{"rec_number": rec} for rec in order], ensure_ascii=False))

    if report["unknown"] or (args.strict and report["blocks_without_citation"]):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
