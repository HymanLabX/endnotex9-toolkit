# The corpus file, the evidence table, and the ledger

Three artefacts, in the order they are made. `corpus.json` fixes what is in scope, the evidence
table records what each record says and how well it is known, and the coverage ledger — computed
from the other two — is what ships beside the review so a reader can see its edges.

## `corpus.json`

Write it once, in the working directory, as soon as the scope is confirmed (SKILL.md §1). Two
shapes are accepted by `cite_audit.py`:

**The reply from `list_references`, saved as-is** — convenient for a single-page scope:

```json
{"total_matching": 63, "offset": 0, "returned": 63,
 "references": [
   {"rec_number": 1204, "title": "…", "authors": "Zhang, Y.; Li, M.", "year": "2021",
    "container_title": "Acta Materialia", "doi": "10.1016/…", "has_pdf": true}
 ]}
```

**A list you assembled** — needed whenever the scope came from several calls (paged results, one
call per lab member, a union of queries), which is the usual case:

```json
[
  {"rec_number": 1204, "title": "…", "year": "2021", "authors": "Zhang, Y.; Li, M.",
   "has_pdf": true, "depth": "fulltext", "themes": ["fatigue"], "source": "group:Elastocaloric"},
  {"rec_number": 88, "title": "…", "year": "2018", "has_pdf": false, "depth": "abstract"}
]
```

Only `rec_number` is required; everything else makes the audit's output more useful.
`depth` is read for the coverage breakdown, so fill it in as §3 proceeds rather than at the end.
`source` earns its place in a multi-query scope: when the user later asks why a record is in the
review, it is the answer.

Rules for the file:

- **One record, one entry.** Merge by `rec_number` when unioning; duplicate entries inflate every
  count downstream.
- **It is frozen.** Once drafting starts, do not add records to it silently. A record found
  mid-draft either goes in with the corpus re-frozen and the census recomputed, or is left out
  and named in the ledger.
- **It is not the library.** It is the slice. Nothing outside it may be cited.

## Evidence rows

One row per record, including the ones nobody read. Markdown while working, since it goes in the
delivered artefact; the columns that matter:

| Col | Holds | Notes |
|---|---|---|
| `rec` | `[#1204]` | The marker form, so the table's own rows pass the audit |
| `cite` | `Zhang 2021` | Short label used in prose |
| `year` | `2021` | |
| `venue` | `Acta Mater.` | Abbreviate consistently |
| `type` | journal / conf / thesis / patent / review | Reviews and patents behave differently as evidence |
| `question` | What the record set out to answer | One clause |
| `method` | Materials, n, instrument, model, code | Blank at `abstract` depth unless the abstract states it |
| `result` | The finding **with its number and page** | `12.5 K at 4 % strain, PDF p. 5` — only at `fulltext` depth |
| `baseline` | What it was compared against | "none reported" is a finding |
| `limits` | Stated and visible | |
| `themes` | The spine's labels | Several allowed; this is what §4 clusters on |
| `depth` | `fulltext` / `abstract` / `metadata` | The one field never to fudge |

At `abstract` depth, `question`, `themes` and the paper's *claim* are fillable; `result`,
`baseline` and `limits` usually are not. Leave them empty rather than inferring them — an empty
cell is information, a plausible guess is not.

At `metadata` depth (no abstract, no PDF), fill `cite`, `year`, `venue`, `type` and at most a
tentative theme from the title. Nothing else. These records are counted in the review, may be
named in a list, and may not be characterised.

A worked row:

```markdown
| rec | cite | year | venue | type | question | method | result | baseline | limits | themes | depth |
|---|---|---|---|---|---|---|---|---|---|---|---|
| [#1204] | Zhang 2021 | 2021 | Acta Mater. | journal | Fatigue life of NiTi under cyclic strain | NiTi wire, n = 5, DSC + IR, 4 % strain | ΔT_ad 12.5 K, stable to 10³ cycles, PDF p. 5, 7 | Cu-based alloy, 8 K | single composition, no error bars | fatigue; NiTi | fulltext |
| [#88] | Li 2018 | 2018 | Scr. Mater. | journal | Composition effect on transformation | — | authors report improved stability (abstract) | — | — | NiTi | abstract |
```

Note what the second row does: it reports a *claim*, attributed as a claim, and leaves the rest
empty. That is what an abstract supports.

## Recording disagreement

Disagreements are the most valuable output of the table and the easiest to lose, because each
half sits in a different row. Keep a short second table:

```markdown
| Question | Position A | Position B | What differs | Strength |
|---|---|---|---|---|
| Cyclic stability of NiTi | degrades after 10³ [#1204] | stable to 10⁴ [#77] | strain amplitude (4 % vs 2 %), different wire supplier | both fulltext, n = 5 vs n = 3 |
```

`What differs` is the column that does the work. If it is empty, the disagreement may be real and
unexplained — say that in the review rather than dropping the row.

## The coverage ledger

Computed from `corpus.json` plus `cite_audit.py`'s output, and delivered with the review:

```markdown
## Coverage ledger

**Scope:** EndNote group "Elastocaloric" (63 records), resolved 2026-08-11
**Read:** 11 full text · 44 abstract · 8 metadata only
**Cited in this review:** 38 of 63

| Not cited | Why |
|---|---|
| 12 records | metadata only or off-theme (list: [#…], [#…]) |
| 8 records | duplicated by a later record from the same authors |
| 5 records | reviews and editorials, used for context only |

**Known edges:** the corpus holds nothing after 2024-11 (last import); non-English records are
under-represented (3 of 63); 8 records have neither abstract nor PDF.
```

Three properties make it worth shipping: the counts add up to the corpus size, every uncited
record is accounted for by a reason rather than by silence, and the edges are stated as
properties of the collection. `cite_audit.py --json` gives the counts and the uncited list;
the reasons are yours to supply.

## Citation markers

The syntax `cite_audit.py` parses, and the only citation form allowed in a working draft:

| Form | Meaning |
|---|---|
| `[#1204]` | Record 1204 |
| `[#1204, #88]` | Both, in that order |
| `[#1204; #88]` | The same — semicolons are accepted |
| `[#1204], PDF p. 5` | A number's provenance: the record, then where in it |

The marker is the **EndNote record number**, not a position in the reference list. Numbering
against the bibliography happens once, at the end, through `--renumber`.

Markers are matched anywhere in the draft, including inside tables — which is why the evidence
table's `rec` column uses the marker form: the table then contributes to the audit instead of
sitting outside it. "Anywhere" includes fenced code blocks, so an example marker shown in one
must name a record that is really in the corpus, or the audit will report it as unknown.

`--renumber` rewrites markers to `[1]`, `[2]`, … in order of first appearance and prints the
matching record order for `get_bibliography(references=[…], style="gbt7714")`, which numbers in
the order given. It refuses while any marker names a record outside the corpus. For APA 7, which
sorts the bibliography alphabetically, do not renumber: convert markers to author–year in a final
pass, and run the audit on the marked draft before that pass, not after.
