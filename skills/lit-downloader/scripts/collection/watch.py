"""Watch the browser's download folder; match each new PDF to a working-list record, validate it,
and file it as pdf/<doi>.pdf.

Usage: python watch.py <project> [--dir <folder>] [--once] [--since-min 1]

--dir        folder to watch (default: `downloads` from project.json)
--once       process what is there and exit (e.g. a folder of PDFs the user downloaded by hand,
             with --since-min large enough to include them)
--since-min  only consider files saved in the last N minutes before start-up (default 1), so
             older files in the folder are left alone

A PDF is matched by the DOI printed in it, the Elsevier PII in its file name
(1-s2.0-<PII>-main.pdf), a file name made of the DOI suffix or arXiv id, or, failing those, by
title. It is filed only after validate_pdf accepts it; anything else stays where it is.
Only this process writes pdf_state_manual.json, and it merges with the file on disk before each
write, so a record filed by hand meanwhile is never lost. Run one watcher at a time.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

from collection_common import (
    STATE_BROWSER,
    console_utf8,
    crossref_message,
    list_records,
    load_project,
    load_validator,
    oa_urls,
    ok_states,
    read_json,
    rec_doi,
    rec_title,
    safe_name,
    write_json_atomic,
)

from endnotex9_toolkit.pdf_identity import MissingDoiError

DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"<>]+", re.I)
PUBLISHER_HOSTS = re.compile(
    r"doi\.org|sciencedirect|springer|wiley|mdpi|iop|tandfonline|sagepub|"
    r"acs\.org|aip\.org|aps\.org|nature\.com|jstage"
)


def _key(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


class Matcher:
    """Everything that can identify a downloaded file, built once from the project."""

    def __init__(self, project):
        self.titles = {rec_doi(r): rec_title(r) for r in list_records(project) if rec_doi(r)}
        self.pii = {}
        for d in self.titles:
            if d.startswith("10.1016/"):
                for alt in crossref_message(project, d).get("alternative-id", []):
                    self.pii[alt.upper().lstrip("S")] = d
        # Springer and others name downloads after the DOI suffix; arXiv after the arXiv id;
        # repositories after the last part of the link that was opened.
        self.names = {}
        for d in self.titles:
            k = _key(d.split("/", 1)[1]) if "/" in d else ""
            if len(k) >= 8:
                self.names.setdefault(k, d)
        self.arxiv, tails, self.has_repo = {}, {}, set()
        for d, urls in oa_urls(project).items():
            if d not in self.titles:
                continue
            for u in urls:
                m = re.search(r"arxiv\.org/(?:abs|pdf)/(?:[a-z\-]+/)?(\d{4}\.\d{4,5}|\d{7})", u)
                if m:
                    self.arxiv[m.group(1)] = d
                seg = u.split("?")[0].rstrip("/").rsplit("/", 1)[-1]
                seg = re.sub(r"\.pdf$", "", seg, flags=re.I).lower()
                if len(seg) >= 5:
                    tails.setdefault(seg, set()).add(d)
                if not PUBLISHER_HOSTS.search(u):
                    self.has_repo.add(d)
        self.tails = {k: next(iter(v)) for k, v in tails.items() if len(v) == 1}

    def from_name(self, name: str) -> tuple[str | None, bool]:
        """(doi, is_author_version) from the file name alone."""
        pii = re.search(r"1-s2.0-(S?[0-9A-Z]{15,17})", name, re.I)
        if pii and pii.group(1).upper().lstrip("S") in self.pii:
            return self.pii[pii.group(1).upper().lstrip("S")], False
        stem = re.sub(r"\s*\(\d+\)$", "", Path(name).stem)
        ax = re.fullmatch(r"(\d{4}\.\d{4,5}|\d{7})(?:v\d+)?", stem)
        if ax and ax.group(1) in self.arxiv:
            return self.arxiv[ax.group(1)], True
        if _key(stem) in self.names:
            return self.names[_key(stem)], False
        if stem.lower() in self.tails:
            return self.tails[stem.lower()], True
        return None, False


def candidates(
    m: Matcher, text: str, name_doi: str | None, done: set[str], similarity
) -> list[str]:
    found = {x.rstrip(".,;)]").lower() for x in DOI_RE.findall(text)}
    cands = [d for d in found if d in m.titles]
    if name_doi and name_doi not in cands:
        cands.insert(0, name_doi)
    if not cands:
        low = text[:6000].lower()
        for d, t in m.titles.items():
            if d in done or not t:
                continue
            words = re.findall(r"[a-z]{6,}", t.lower())[:6]
            # Cheap pre-filter before the sliding-window comparison.
            if words and sum(w in low for w in words) >= max(1, len(words) - 1):
                if similarity(t, text[:6000]) >= 0.92:
                    cands.append(d)
    return cands


def pick(m: Matcher, data: bytes, cands, found_in_text, name_doi, validate_pdf):
    """Pages 1-3 can cite other listed papers, so try every candidate and keep the best."""
    best, errors, author = None, [], set()
    for d in cands:
        try:
            # Without DOI text in the file, the file name plus the title check is the evidence.
            info = validate_pdf(
                data, "" if (d == name_doi and d not in found_in_text) else d, m.titles[d]
            )
        except RuntimeError as exc:
            if isinstance(exc, MissingDoiError) and d in m.has_repo:
                try:
                    info = validate_pdf(data, "", m.titles[d])
                except RuntimeError as exc2:
                    errors.append(f"{d}: {exc2}")
                    continue
                if info["title_similarity"] < 0.92:
                    errors.append(
                        f"{d}: author version but title similarity {info['title_similarity']}"
                    )
                    continue
                info["doi_match"] = False
                author.add(d)
            else:
                errors.append(f"{d}: {exc}")
                continue
        if d == name_doi:  # the publisher's file name names the article itself
            return (d, info), errors, author
        if best is None or info["title_similarity"] > best[1]["title_similarity"]:
            best = (d, info)
    return best, errors, author


def main(argv=None) -> int:
    console_utf8()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("project")
    ap.add_argument("--dir")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--since-min", type=float, default=1)
    args = ap.parse_args(argv)
    project = load_project(args.project)
    watch = Path(args.dir or project.downloads)
    project.pdf_dir.mkdir(exist_ok=True)
    state_path = project.dir / STATE_BROWSER
    validate_pdf, similarity = load_validator()
    import fitz

    m = Matcher(project)
    started = time.time() - args.since_min * 60
    seen = set()

    def save(doi, entry):
        disk = read_json(state_path)  # merge, never overwrite what someone else filed
        disk[doi] = entry
        write_json_atomic(state_path, disk)
        return sum(v.get("status") == "ok" for v in disk.values())

    print(f"watching {watch} for PDFs ...", flush=True)
    while True:
        for f in sorted(watch.glob("*.pdf"), key=lambda p: p.stat().st_mtime):
            st = f.stat()
            key = (f.name, st.st_size)
            if key in seen or st.st_mtime < started or time.time() - st.st_mtime < 3:
                continue  # already handled, older than this run, or still being written
            seen.add(key)
            try:
                data = f.read_bytes()
                with fitz.open(stream=data, filetype="pdf") as doc:
                    text = "\n".join(doc[i].get_text() for i in range(min(3, doc.page_count)))
            except Exception as exc:  # any unreadable file is just skipped
                print(f"SKIP {f.name}: unreadable ({exc})", flush=True)
                continue
            done = set(ok_states(project))
            found = {x.rstrip(".,;)]").lower() for x in DOI_RE.findall(text)}
            name_doi, name_author = m.from_name(f.name)
            cands = candidates(m, text, name_doi, done, similarity)
            if not cands:
                print(f"NOMATCH {f.name}", flush=True)
                continue
            best, errors, author = pick(m, data, cands, found, name_doi, validate_pdf)
            if best is None:
                print(f"BAD  {f.name}: {'; '.join(errors)[:200]}", flush=True)
                continue
            doi, info = best
            if doi in done:
                print(f"DUP  {f.name} -> {doi} (already have it)", flush=True)
                continue
            target = project.pdf_dir / (safe_name(doi) + ".pdf")
            target.write_bytes(data)
            f.unlink()
            preprint = doi in author or (doi == name_doi and name_author)
            source = "manual-browser-preprint" if preprint else "manual-browser"
            total = save(doi, {"status": "ok", "source": source, "file": target.name, **info})
            print(f"OK   {f.name} -> {target.name}  (browser total {total})", flush=True)
        if args.once:
            return 0
        time.sleep(3)


if __name__ == "__main__":
    sys.exit(main())
