"""Open PDF links one at a time in the user's own Chrome and wait for the watcher to file each.

Usage: python open_links.py <project> [--skip-missed | --only-missed [--include-untried]]
                            [--interleave] [--limit N] [--dry-run] ...

Normally started by round.py, which also runs the watcher. Each link opens in its own new window;
the script closes only that window (never the user's, never the last one). Every opened link is
appended to open_in_chrome_log.jsonl with its date, which the ScienceDirect daily quota counts.

Pacing: one site at a time with --min-gap seconds between two of its links (ScienceDirect:
--elsevier-gap, and at most --elsevier-daily per calendar day across all runs). A site is held
for the rest of the run at the first block page (CAPTCHA, "Just a moment", 请稍候, a bare-domain
title) or after a run of misses (3 for ScienceDirect, --max-miss otherwise). Holds are per host,
so a publisher serving several DOI prefixes is held as one.
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import json
import re
import subprocess
import sys
import time
from itertools import zip_longest
from urllib.parse import urlparse

from collection_common import (
    OPEN_LOG,
    console_utf8,
    crossref_message,
    doi_prefix,
    elsevier_pii,
    list_dois,
    load_project,
    oa_urls,
    ok_states,
    read_open_log,
)

WM_CLOSE = 0x0010
BLOCKED = re.compile(
    r"just a moment|access denied|too many requests|rate limit|captcha|are you a robot|"
    r"unusual traffic|request blocked|verify you are human|forbidden|error de verificaci|"
    r"请稍候|验证|访问受限|机器人",
    re.I,
)
# Repository copies (HAL, PMC, ...) that refuse scripts but open fine in the browser.
REPO = re.compile(
    r"hal\.science|archives-ouvertes|pmc\.ncbi\.nlm\.nih\.gov|europepmc|arxiv\.org|"
    r"repositor|handle\.net|eprints|core\.ac\.uk|zenodo|osti\.gov",
    re.I,
)
FULLY_OA = ("10.3390/", "10.1155/", "10.3389/")  # use the publisher's own site, not a repository
CHROME_SUFFIX = " - Google Chrome"


def url_for(doi: str, message: dict, repo_urls: list[str], skip_hosts: list[str]) -> str:
    """The link to open for a DOI, or "" when there is none."""
    repo = [u for u in repo_urls if REPO.search(u) and not any(h in u for h in skip_hosts)]
    if repo and not doi.startswith(FULLY_OA):
        return repo[0]
    if doi.startswith("10.1016/"):
        pii = elsevier_pii(message)
        return f"https://www.sciencedirect.com/science/article/pii/{pii}/pdf" if pii else ""
    if doi.startswith("10.1002/"):
        return f"https://onlinelibrary.wiley.com/doi/pdfdirect/{doi}?download=true"
    if doi.startswith("10.3390/"):
        landing = ((message.get("resource") or {}).get("primary") or {}).get("URL", "")
        return landing.rstrip("/") + "/pdf" if "mdpi.com" in landing else ""
    for link in message.get("link", []):
        if link.get("content-type") == "application/pdf":
            return link["URL"].replace("http://", "https://", 1)
    return ""


def is_block_page(title: str, url: str) -> bool:
    page = title.removesuffix(CHROME_SUFFIX).strip()
    # A challenge that never rendered its own title shows just the domain.
    return bool(BLOCKED.search(page)) or page.lower() == urlparse(url).netloc.lower()


def select(dois, done, misses, args) -> list[str]:
    excluded = tuple(p.rstrip("/") + "/" for p in args.exclude if p)
    todo = [
        d
        for d in dois
        if not d.startswith(excluded) and not d.startswith("arxiv-") and d not in done
    ]
    if args.skip_missed:  # failures wait for the retry round; daily rounds open new records only
        todo = [d for d in todo if d not in misses]
    if args.only_missed:
        floor = 0 if args.include_untried else 1
        todo = [d for d in todo if floor <= misses.get(d, 0) < args.max_tries]
    if args.interleave:  # round-robin across publishers so no site sees back-to-back requests
        groups: dict[str, list[str]] = {}
        for d in todo:
            groups.setdefault(doi_prefix(d), []).append(d)
        todo = [d for batch in zip_longest(*groups.values()) for d in batch if d]
    return todo


class Browser:
    """The user's own Chrome, seen through its top-level windows."""

    def __init__(self, exe: str):
        self.exe = exe
        self.user32, self.kernel32 = ctypes.windll.user32, ctypes.windll.kernel32

    def _exe_of(self, hwnd) -> str:
        pid = wt.DWORD()
        self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        h = self.kernel32.OpenProcess(0x1000, False, pid.value)  # QUERY_LIMITED_INFORMATION
        if not h:
            return ""
        try:
            buf, size = ctypes.create_unicode_buffer(1024), wt.DWORD(1024)
            ok = self.kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
            return buf.value if ok else ""
        finally:
            self.kernel32.CloseHandle(h)

    def title(self, hwnd) -> str:
        buf = ctypes.create_unicode_buffer(512)
        self.user32.GetWindowTextW(hwnd, buf, 512)
        return buf.value

    def windows(self) -> set:
        found = set()

        @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
        def cb(hwnd, _):
            cls = ctypes.create_unicode_buffer(64)
            self.user32.GetClassNameW(hwnd, cls, 64)
            # Browser windows end in " - Google Chrome"; bubbles such as "Restore pages?" don't.
            if (
                cls.value == "Chrome_WidgetWin_1"
                and self.user32.IsWindowVisible(hwnd)
                and self.title(hwnd).endswith(CHROME_SUFFIX)
                and self._exe_of(hwnd).lower().endswith("chrome.exe")
            ):
                found.add(hwnd)
            return True

        self.user32.EnumWindows(cb, 0)
        return found

    def open(self, url: str) -> None:
        subprocess.Popen([self.exe, "--new-window", url])

    def alive(self, hwnd) -> bool:
        return bool(hwnd) and bool(self.user32.IsWindow(hwnd))

    def close(self, hwnd) -> None:
        self.user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)


def main(argv=None) -> int:
    console_utf8()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("project")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--wait", type=float, default=30, help="seconds to wait for each PDF")
    ap.add_argument("--delay", type=float, default=2)
    ap.add_argument("--exclude", default=None, help="DOI prefixes to skip (default: project.json)")
    ap.add_argument("--exclude-host", default=None, help="URL host substrings to skip")
    ap.add_argument("--only-host", default="", help="open only URLs on these host substrings")
    ap.add_argument("--interleave", action="store_true")
    ap.add_argument("--min-gap", type=float, default=None)
    ap.add_argument("--elsevier-gap", type=float, default=None)
    ap.add_argument("--elsevier-daily", type=int, default=None)
    ap.add_argument("--max-miss", type=int, default=5)
    ap.add_argument("--skip-missed", action="store_true")
    ap.add_argument("--only-missed", action="store_true")
    ap.add_argument("--max-tries", type=int, default=2)
    ap.add_argument("--include-untried", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="list what would be opened")
    args = ap.parse_args(argv)
    project = load_project(args.project)
    args.exclude = args.exclude.split(",") if args.exclude is not None else project.exclude_prefixes
    skip_hosts = [
        h
        for h in (
            args.exclude_host.split(",") if args.exclude_host is not None else project.blocked_hosts
        )
        if h
    ]
    only_hosts = [h for h in args.only_host.split(",") if h]
    min_gap = project.publisher_gap if args.min_gap is None else args.min_gap
    sd_gap = project.sciencedirect_gap if args.elsevier_gap is None else args.elsevier_gap
    sd_daily = project.sciencedirect_daily if args.elsevier_daily is None else args.elsevier_daily

    entries = read_open_log(project)
    misses: dict[str, int] = {}
    for e in entries:
        if not e["got"]:
            misses[e["doi"]] = misses.get(e["doi"], 0) + 1
    today = time.strftime("%Y-%m-%d")
    sd_left = max(
        0,
        sd_daily
        - sum(1 for e in entries if e.get("date") == today and "sciencedirect.com" in e["url"]),
    )
    print(
        f"ScienceDirect today: {sd_daily - sd_left} opened earlier, {sd_left} left of {sd_daily}",
        flush=True,
    )
    repo_urls = oa_urls(project)
    todo = select(list_dois(project), set(ok_states(project)), misses, args)
    browser = None if args.dry_run else Browser(project.browser)
    log_path = project.dir / OPEN_LOG
    n = got = sd_opened = 0
    last_open, streak, held = {}, {}, set()
    for doi in todo:
        if args.limit and n >= args.limit:
            break
        url = url_for(doi, crossref_message(project, doi), repo_urls.get(doi, []), skip_hosts)
        if not url:
            print(f"NOURL {doi}", flush=True)
            continue
        if any(h in url for h in skip_hosts) or (
            only_hosts and not any(h in url for h in only_hosts)
        ):
            continue
        site = urlparse(url).netloc.lower()
        is_sd = "sciencedirect.com" in site
        if site in held or (is_sd and sd_opened >= sd_left):
            continue
        n += 1
        sd_opened += is_sd
        if args.dry_run:
            print(f"PLAN {doi}  {url}", flush=True)
            continue
        wait_more = last_open.get(site, 0) + (sd_gap if is_sd else min_gap) - time.time()
        if wait_more > 0:
            time.sleep(wait_more)  # keep each site at a human reading pace
        last_open[site] = time.time()
        before = browser.windows()
        browser.open(url)
        own = None

        def spot(before=before):
            new = browser.windows() - before
            # Only a single unambiguous new window is ours; if the user opened one too, leave both.
            return next(iter(new)) if len(new) == 1 else (0 if len(new) > 1 else None)

        deadline = time.time() + args.wait
        while time.time() < deadline and doi not in ok_states(project):
            time.sleep(1)
            if own is None:
                own = spot()
        hit = doi in ok_states(project)
        got += hit
        late = time.time() + 6  # a direct download leaves an empty window that may appear late
        while own is None and time.time() < late:
            time.sleep(1)
            own = spot()
        title = browser.title(own) if browser.alive(own) else ""
        # Closing Chrome's only window would quit the browser; keep that one.
        if browser.alive(own) and browser.windows() - {own}:
            time.sleep(1)
            browser.close(own)
        if not hit and is_block_page(title, url):
            held.add(site)
            print(f"HOLD {site} (block page: {title[:80]})", flush=True)
        streak[site] = 0 if hit else streak.get(site, 0) + 1
        if site not in held and streak[site] >= (min(3, args.max_miss) if is_sd else args.max_miss):
            held.add(site)  # a run of misses usually means throttling; leave the site alone
            print(f"HOLD {site} after {streak[site]} consecutive misses", flush=True)
        print(
            f"{'GOT ' if hit else 'MISS'} {doi}  {url}" + ("" if hit else f"  [{title[:70]}]"),
            flush=True,
        )
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "doi": doi,
                        "url": url,
                        "got": hit,
                        "title": title[:200],
                        "date": time.strftime("%Y-%m-%d"),
                        "t": time.strftime("%H:%M:%S"),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
        time.sleep(args.delay)
    print(f"opened {n}, filed {got}, held {sorted(held)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
