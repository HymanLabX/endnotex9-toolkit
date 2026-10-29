# New collection: search export → verified PDFs → EndNote import

One project folder per search. Every script takes it as its first argument and reads
`<project>/project.json`. The stages share state files, so any stage can be rerun and a run that
stops halfway loses nothing. Expect several days end to end: the publisher rounds are paced on
purpose. A real run (WoS, 4,419 hits → 1,704 records) took seven days and ended at 87 % with PDF.

## Before starting

- Windows; Google Chrome, which is the browser the user logs in with. The collection rounds open
  links in the user's *own* Chrome with its normal profile, cookies, and institutional access.
  They do not use the dedicated Edge profile of the selected-records mode.
- The institutional network (campus or VPN) for WoS and for subscribed publishers.
- The target EndNote library, and the EndNote version that keeps its old format. EndNote X9.3
  silently converts an X9 library to SQLite, and the reader used here sees only the old tables.
  `library.py` warns when it finds an `sdb` folder.
- The user's go-ahead for each browser round. Rounds open and close Chrome windows and take
  focus, so run them when the user does not mind or is watching.

## Project layout

```text
<project>/
  project.json        settings (below)
  query.txt           the search as run
  raw/*.ris           search exports, untouched
  tiers.csv, tier_<T>.ris          merge.py output for review
  <list>.ris          the working list (default list.ris); every later stage reads it
  removed_trim.csv    what trim.py removed and why
  crossref_cache/     one CrossRef record per DOI
  pdf/                verified PDFs, named after the DOI
  pdf_state.json      written by oa.py only
  pdf_state_manual.json            written by watch.py only
  open_in_chrome_log.jsonl         one line per link opened (dated; the daily quota counts it)
  open.log, watch.log              round output
  missing_pdfs.html / .csv         checklist.py
  import.ris, import_manifest.json build_import.py
```

`project.json` (every key optional):

```json
{
  "list": "list.ris",
  "downloads": "C:/Users/<user>/Downloads",
  "browser": "C:/Program Files/Google/Chrome/Application/chrome.exe",
  "crossref_cache": "crossref_cache",
  "sciencedirect_daily": 100,
  "sciencedirect_gap": 60,
  "publisher_gap": 15,
  "exclude_prefixes": ["10.1063", "10.1116", "10.1134", "10.1080"],
  "blocked_hosts": ["sagepub", "hal.science", "hdl.handle.net"],
  "label": "WoS-<topic>-<yyyymm>"
}
```

`downloads` must be the folder Chrome saves into. `exclude_prefixes` and `blocked_hosts` are
publishers and repositories that refuse scripted browsing (AIP, Pleiades, Taylor & Francis,
SAGE, HAL). Their records go to the manual list instead of being retried; extend the lists when
a round shows another site doing the same.

## Stages

### 1. Query (checkpoint)

Draft the search with the user and save it as `query.txt`. Make it broad and filter locally;
that is cheaper than editing the query on WoS. Exclude a neighbouring field in the **title**
only. For example, `NOT TI=(NiTi ...)` keeps Ni-free papers that mention NiTi as a comparison
in the abstract. The user approves the query before it runs.

### 2. Export

Export the full records (abstract, keywords, times cited) as RIS, 1–1000, 1001–2000, …, into
`raw/`. WoS allows 1000 records per export. If you drive the user's visible browser to do it, the
user handles any login; never type or store credentials.

### 3. Merge and tier (checkpoint)

Write topic rules for this search in the project folder. Copy
`scripts/collection/examples/rules_ti_sma.py` and rewrite the patterns. `classify(rec)` returns
`(tier, why)`, where `X` means excluded. Match element symbols case-sensitively ("Ti-Mo" must not
match "Timoshenko"). Write regexes with the Write/Edit tools, not through a shell heredoc or
`python -c`: a `\b` passed through some shells arrives as a literal backspace, which matches
nothing. The first copy of these rules lost eight relevant records that way.

```powershell
collection/merge.py <project> --rules <project>/rules.py --library <Library.enl>
```

`--library` and `--existing <a.ris>` mark records already held as `DUP`. Show the user the tier
counts and sample `why` reasons from `tiers.csv`, and let them choose which tiers to keep.

### 4. Trim (checkpoint)

```powershell
collection/trim.py <project> --input tier_A.ris tier_B.ris --journal-only --languages English,Chinese --old-lowcite 2015:5 --drop-errata --dry-run
```

The criteria are the user's call; show the dry-run counts, then run without `--dry-run`. WoS
types journal articles from conference special issues as `JOUR` with a `CP` field; they are
kept by `--journal-only`. **Trim before downloading.** Trimming halfway through a run once wasted
about 50 downloads on records that were removed afterwards.

### 5. Metadata

```powershell
collection/crossref_cache.py <project>
collection/nodoi.py <project>            # review nodoi_crossref.csv, then:
collection/nodoi.py <project> --apply
```

The CrossRef cache supplies publisher PDF links and the Elsevier PII the ScienceDirect links are
built from. `nodoi.py` accepts a match only at title similarity ≥ 0.93 and year ± 1. In the
real run 4 of 25 records without a DOI matched; the rest went to document delivery.

### 6. Open access

```powershell
collection/oa.py <project> --dry-run
collection/oa.py <project>
```

OpenAlex and Semantic Scholar batch lookups, no key and no email. It never touches a record that
already has a PDF and never refetches a URL it has tried, so rerun it before the retry rounds:
repositories add copies over time. Expect roughly 10 % from open access.

### 7. Browser rounds

```powershell
collection/round.py <project> daily --dry-run      # what would open
collection/round.py <project> daily                # run in the background
```

- `daily` opens only records never tried; ScienceDirect up to `sciencedirect_daily` per calendar
  day, counted across runs. One round a day by default. Raise the quota for one day only when
  the user says so, e.g. `--sd-daily 200 --sd-gap 90` for a second round. 200 a day worked in
  the real run.
- `retry` opens records that failed once, a second time; never a third.
- `host sagepub` runs one touchy host on its own, 120 s apart, held after 2 misses.

Watch `open.log` while a round runs. Report progress to the user about every 15 minutes, and
straight away on `HOLD`, a traceback, or the final `opened N, filed M` line.

- `GOT` / `MISS [window title]`: the PDF was filed / not filed in time.
- `HOLD <host>`: that site is stopped for the rest of the round. A block page ("请稍候…",
  "Just a moment", CAPTCHA, a title that is only the domain) holds it at once; otherwise a run
  of misses does (3 for ScienceDirect).
- On a ScienceDirect hold, stop and tell the user. A block on the first link has always been
  temporary; retrying 30 minutes later, with the user's consent, worked each time.

`round.py` starts the watcher, stops it 20 s after the opener ends, and before and after every
round stops any watcher left from an earlier one. Two watchers at once overwrite each other's
records. If the round is killed from outside, check that no `watch.py` is left running.

A MISS whose PDF lands late is filed anyway and is not retried. Slow downloads are harmless.

### 8. What is left

```powershell
collection/status.py <project>
collection/checklist.py <project> --lang zh        # or en
```

`status.py` splits the missing records by cause: no DOI, no link, never tried, failed once,
failed twice+. `missing_pdfs.html` lists them by publisher with links for the user to click in
their own browser. `missing_pdfs.csv` is the same list for the library's document delivery. If
the user downloads by hand before the import, run the watcher on the folder they saved to:

```powershell
collection/watch.py <project> --dir <folder> --once --since-min 100000
```

### 9. Import (checkpoint)

The user decides when. PDFs found after this point have to be attached by hand. Build:

```powershell
collection/build_import.py <project> --library <Library.enl> --journal-case --dry-run
collection/build_import.py <project> --library <Library.enl> --journal-case --move-orphans
```

The library's database is only read; its attachment folder is written to. Records it already
holds (by DOI, or by title for records without a DOI) are left out. Each PDF is copied to
`<Library>.Data/PDF/<n>/` and linked as `internal-pdf://<n>/<file>`. `--move-orphans` moves
aside folders copied earlier for records since removed, into `<project>/orphan_pdf_backup/`;
nothing is deleted. A label (`LB`) marks the batch so the user can find it later.

Then the user, in the EndNote version that keeps the library's format, does this:

1. Open the library.
2. File → Import → File, and choose `import.ris`.
3. Import Option **Reference Manager (RIS)**, Duplicates **Import All**, Text Translation
   **Unicode (UTF-8)**.

Records appear only when the "Importing…" bar finishes; for 1,700 records that took about five
minutes. EndNote then indexes the PDFs ("Indexing PDFs, n of N"), about 12 minutes for 1,500
PDFs. Tell the user not to close EndNote or cancel either window. The sidebar count refreshes
only at the end. After that:

```powershell
collection/verify_import.py <project> --library <Library.enl>
```

It reports DOIs present, links present, missing files, and DOIs imported twice.
`attach_after_import.csv` lists PDFs filed after the build, with the EndNote record number for
each. The user attaches each by hand: drag the PDF onto the record, or References → File
Attachments → Attach File. Never import the same file twice. Never reimport records to attach
PDFs.

## Lessons from the first run

| What happened | Rule now |
|---|---|
| A watcher outlived its round by a day; two watchers overwrote each other and lost 2 records | `round.py` stops leftover watchers; the watcher merges with the file before writing |
| The opener read the state file while the watcher was writing it, and crashed | Atomic replace on write; torn reads are retried |
| A Korean window title crashed the round on a GBK console | Every script forces UTF-8 output with replacement |
| SAGE serves several DOI prefixes; holding by prefix let 8 links through in 6 minutes | Holds are per host; a bare-domain title counts as a block page |
| ScienceDirect showed "请稍候…" on the first link of a second round | Held at once; retried 35 minutes later, with consent: 99/99 filed |
| Trimming after downloads had started | Trim before stage 5 |
| A regex `\b` became a backspace on its way through a shell heredoc | Write rules files with the editor tools |
| X9.3 converted the library and the reader saw stale tables | Use the old-format EndNote; `library.py` warns on `sdb` |
