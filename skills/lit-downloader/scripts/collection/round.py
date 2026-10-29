"""One browser round: start the downloads watcher, open links in the user's Chrome, stop the
watcher. Output goes to open.log and watch.log in the project folder.

Usage: python round.py <project> daily|retry|host <host-substring> [--sd-daily N] [--sd-gap S]
                       [--limit N] [--dry-run]

daily   records never tried yet (failures wait for the retry round); ScienceDirect up to the
        daily quota. Run once a day, on the user's go-ahead.
retry   records that failed once, opened a second time (never a third).
host    one slow or touchy host on its own, e.g. `host sagepub`: also opens records set aside
        before, 120 s apart, held after 2 misses.

Before and after the round, every leftover watcher is stopped: two watchers overwrite each
other's records.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

from collection_common import SCRIPT_DIR, console_utf8, load_project


def stop_watchers() -> list[str]:
    """Stop every python process running this pipeline's watch.py; return their ids."""
    ps = (
        "Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" | "
        "Where-Object { $_.CommandLine -match 'collection[\\\\/]+watch\\.py' } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force; $_.ProcessId }"
    )
    out = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", ps],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    return out.split()


def mode_args(mode: str, host: str) -> list[str]:
    if mode == "daily":
        return ["--skip-missed", "--interleave"]
    if mode == "retry":
        return ["--only-missed", "--max-tries", "2", "--interleave"]
    return [
        "--only-missed",
        "--max-tries",
        "2",
        "--include-untried",
        "--only-host",
        host,
        "--exclude-host",
        "",
        "--min-gap",
        "120",
        "--max-miss",
        "2",
    ]


def main(argv=None) -> int:
    console_utf8()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("project")
    ap.add_argument("mode", choices=["daily", "retry", "host"])
    ap.add_argument("host", nargs="?", default="")
    ap.add_argument("--sd-daily", type=int, help="override sciencedirect_daily for today")
    ap.add_argument("--sd-gap", type=float, help="override sciencedirect_gap (seconds)")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true", help="list what would be opened")
    args = ap.parse_args(argv)
    if args.mode == "host" and not args.host:
        ap.error("host mode needs a host substring, e.g. `host sagepub`")
    project = load_project(args.project)
    opener = [
        sys.executable,
        str(SCRIPT_DIR / "open_links.py"),
        str(project.dir),
        *mode_args(args.mode, args.host),
    ]
    if args.sd_daily is not None:
        opener += ["--elsevier-daily", str(args.sd_daily)]
    if args.sd_gap is not None:
        opener += ["--elsevier-gap", str(args.sd_gap)]
    if args.limit:
        opener += ["--limit", str(args.limit)]
    if args.dry_run:
        return subprocess.run([*opener, "--dry-run"], check=False).returncode

    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    watch_log = (project.dir / "watch.log").open("a", encoding="utf-8")
    left = stop_watchers()
    if left:
        watch_log.write(f"=== {stamp} stopped leftover watcher(s): {' '.join(left)}\n")
    watch_log.write(f"=== {stamp} {args.mode} round\n")
    watch_log.flush()
    watcher = subprocess.Popen(
        [sys.executable, str(SCRIPT_DIR / "watch.py"), str(project.dir)],
        stdout=watch_log,
        stderr=subprocess.STDOUT,
    )
    try:
        time.sleep(10)  # let the watcher load the list
        with Path(project.dir / "open.log").open("a", encoding="utf-8") as log:
            log.write(f"=== {stamp} {args.mode} round\n")
            log.flush()
            code = subprocess.run(opener, stdout=log, stderr=subprocess.STDOUT, check=False)
        return code.returncode
    finally:
        time.sleep(20)  # the last download may still be landing
        watcher.terminate()
        try:
            watcher.wait(10)
        except subprocess.TimeoutExpired:
            watcher.kill()
        left = stop_watchers()
        if left:
            watch_log.write(
                f"=== {time.strftime('%Y-%m-%d %H:%M:%S')} watcher stopped: {' '.join(left)}\n"
            )
        watch_log.close()


if __name__ == "__main__":
    sys.exit(main())
