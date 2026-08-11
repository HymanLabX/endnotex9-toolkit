---
name: lit-review-writer
description: Write a literature review over one slice of an EndNote X9 library through endnotex9-mcp -- an EndNote group, a research direction, a lab's output, or one researcher's papers. Resolve the scope to an explicit frozen record list, census it, read it at an agreed depth, build an evidence table, synthesise a themed review in which every claim carries the record number it came from, audit those citations against the corpus, and emit the bibliography in APA 7 or GB/T 7714. Use when the user invokes /lit-review-writer, or asks to write a review, survey, related-work section, research-landscape summary, 综述 or 文献综述 over a group, topic, lab or author in their library.
---

# lit-review-writer

A review of one slice of the user's own library. Every tool named below is an MCP tool on the
`endnotex9` server, except `cite_audit.py`, which ships with this skill.

The value here is not "write me a review" — a model will do that from its own memory. It is that
every sentence is anchored to a record the user actually holds, at a stated reading depth, with
the parts nobody read named as such.

## The one thing to say up front

**The corpus is closed.** This skill reviews the papers in the chosen slice of the library, not
the literature on the topic. Everything that follows — the themes, the trends, the "gaps" — is a
statement about that collection. Say this to the user before drafting, not in a footnote after:

- A gap found here is a **gap in the collection** until someone checks it against a database. It
  may be a real gap in the field; this skill cannot tell the difference.
- A trend here is a trend in **what the user collected**, which is shaped by what they searched
  for and when they last imported.
- If they want field coverage, that is a database search — outside this skill and outside this
  server. What this skill can do is widen the slice inside the library (§1) and be explicit about
  the edges.

Users who want a review of *the field* and users who want a review of *their reading* both ask
the same question. Find out which one this is before spending the tokens.

## Load only what is needed

- `references/review-structures.md` — before drafting. Which organising pattern fits which scope,
  the section outlines for a direction, a lab, a researcher and a related-work section, and the
  checklist that separates a review from an annotated list.
- `references/evidence-table.md` — before reading the corpus. The evidence row schema, the
  coverage ledger, the `corpus.json` shape, and the citation-marker syntax `cite_audit.py` parses.
- `scripts/cite_audit.py` — at §6, before delivering. Do not read it to use it; §6 is enough.

## 0. Agree the scope and the depth before spending the tokens

Two questions, both cheap to ask and expensive to get wrong.

**What is the slice?**

| Scope | Resolved by | Watch for |
|---|---|---|
| An EndNote group | `list_groups`, then the `group` filter | Smart groups: the reply carries the saved `query`; membership moves when the library does |
| A research direction | `list_topics` + the `topic` filter, or a `search_references` query set | A search-defined scope is a *decision*, not a fact — record the queries |
| A lab / research group | `list_references(author=…)`, one call per member | Name variants and homonyms (§1) |
| One researcher | `list_references(author=…)` | The same, plus: their name appearing as a co-author is still their output |

**How deep?** Depth is per record, and the mix is what costs tokens.

| Depth | What each record gets | Sensible for |
|---|---|---|
| `survey` | metadata for all, abstracts for all | 60+ records, or a first pass over an unfamiliar group |
| `standard` | abstracts for all, full text for the 8–15 that carry the argument | The default. Ask which papers are pivotal if it is not obvious |
| `deep` | full text for every record, through `lit-deep-reader` | Under ~20 records, or a review that has to defend its numbers |

Default to `standard`. State the mix in the delivered review (§3) — a review whose evidence is
90 % abstracts is honest work, but only if it says so.

## 1. Resolve the scope to an explicit record list, then freeze it

Never work from a scope description. Work from a list of record numbers.

**Groups.** `list_groups` first, always — never guess a group name. Show the user the matching
groups and their sizes; if two names are close ("Elastocaloric" vs "Elastocaloric-new"), ask.

**Authors.** `list_references(author="Zhang")` matches a substring of the authors field, which
means it is both leaky and lossy:

- It over-matches homonyms. Two different Y. Zhangs are one string to a substring filter.
- It under-matches name forms. `Zhang, Y.`, `Zhang Y`, `Y. Zhang` and `张宇` are four strings.
  Run the variants you can think of, including the CJK form for a Chinese-language library, and
  merge the results by `rec_number`.
- A lab is several people. Run one call per member, union the results, and expect overlap.

Print the resolved list — first author, year, title, journal — and have the user confirm it
before reading anything. For an author or lab scope this confirmation is not a formality: it is
the only step that can catch a homonym, and the user is the only one who can.

**Pagination.** `list_references` caps `limit` at 200 and reports `total_matching` (and
`limit_capped` when it trimmed your request). Page with `offset` until you have `total_matching`
records. A review built on the first 200 of 340 records, silently, is the worst failure this
skill has.

**Freeze it.** Write the resolved records to `corpus.json` in the working directory — the shape
is in `references/evidence-table.md`. That file is the contract for everything below: §3 fills
it, §6 audits against it, and the coverage ledger counts it. If the user imports papers halfway
through, do not quietly extend it; finish the review against the frozen corpus and say what
arrived late, or start again.

## 2. Census the slice before reading it

Cheap, purely descriptive, and it shapes the structure you will choose in §4. From `corpus.json`
alone: how many records; the year span and where the mass sits; the reference-type mix (journal
articles vs conference papers vs theses vs patents); the venue concentration; the language mix;
and how many carry a PDF (`has_pdf`).

For a lab or researcher scope, also: co-author concentration, and whether the output clusters in
one venue.

Two cautions:

- `library_stats` and `profile_library` describe the **whole library**, not your slice. They are
  useful for context ("this group is 12 % of the library"), and misleading if quoted as if they
  were scoped. Compute the slice's numbers from `corpus.json`.
- Report the PDF count now, because it caps what §3 can do: 12 full texts available out of 63
  records is the ceiling on `deep`, whatever depth was agreed.

## 3. Read at the agreed depth, one evidence row per record

Abstracts in bulk are cheaper through `sample_references(strategy="group", group=…, size=N,
char_budget=…)` than through N calls to `get_reference_details`: it returns title, abstract,
keywords, journal, year, type, authors and groups in one budgeted reply. It fits records to a
UTF-8 byte budget and **truncates fields to do it**, reporting which — so when the reply says a
record's abstract was truncated, re-read that one with `get_reference_details` rather than
building a claim on half an abstract. (`list_references` items carry no abstract at all.)

For full-text depth: `index_pdfs(references=[{"rec_number": …}, …])` on the chosen records, then
`read_pdf_pages` in passes and `search_fulltext` for specific claims and numbers. `lit-deep-reader`
already documents how to do that well; for a `deep` review, run that skill per paper and harvest
its notes rather than reimplementing it here.

Every record in the corpus gets a row, including the ones you did not read. The schema is in
`references/evidence-table.md`; the field that must never be fudged is `depth`:

| `depth` | Means | May support |
|---|---|---|
| `fulltext` | You read pages of the PDF | Numbers, methods, sample sizes, limitations, comparisons |
| `abstract` | You read the abstract only | The paper's *claims*, its topic, its stated method |
| `metadata` | No abstract, no PDF — title and venue only | Membership in a theme. Nothing else |

A `metadata` record can be counted and named. It cannot be characterised. Never write "X showed
that…" from a title.

## 4. Find the structure in the evidence, not in a template

Choose the organising pattern **after** the evidence table exists, from what the table shows —
thematic, chronological, methodological, by material system or by open question. The patterns,
and the section outlines for each scope type, are in `references/review-structures.md`. Read it
before drafting.

Three things worth deciding out loud, with the user, before you write:

- **The spine.** One organising axis, chosen because the evidence has that shape. A review with
  three axes at once reads as three reviews interleaved.
- **The audience.** A related-work section for the user's own paper is a different document from
  a briefing on a lab they are about to visit — shorter, positioning-led, and it ends at their
  own contribution.
- **The language.** Match the user's. For a bilingual library, give each technical term in both
  forms on first use, and keep the record's own title language in the bibliography.

## 5. Draft, with every claim traceable

Mark every claim inline with the record it came from: `[#1204]`, or `[#1204, #88]` for several.
That marker is the record number, it is what §6 audits, and it is what `--renumber` converts into
the final numbering. Do not write a draft with prose citations and add markers afterwards; the
whole gate depends on the marker being written at the moment the claim is.

What makes it a review rather than a list — the long form is in `references/review-structures.md`:

- **Synthesise, do not enumerate.** Each body paragraph should relate at least two records to
  each other — agreeing, disagreeing, extending, measuring the same thing differently. A
  paragraph that can only be about one record should say so explicitly.
- **Keep disagreements.** Where two records conflict, name both and say what differs — the
  material, the conditions, the measurement. Do not average them into a bland sentence. The
  conflicts are usually the most useful part of the review.
- **Numbers carry their provenance.** A number belongs in the review only if you read it, at
  `fulltext` depth, in that record. Cite it as the record plus the page (`[#1204], PDF p. 5`).
  Never restate a number from memory of the literature, and never lift one from an abstract and
  present it as a measured result.
- **Claim versus shown.** Abstract-depth evidence supports "the authors report", not "it is the
  case". Keep that distinction in the sentence, not in a caveat at the end.
- **Gaps are about the collection.** Write "nothing in this group addresses X" — not "no work
  addresses X". The first is something you verified; the second is not.

## 6. Audit the citations before delivering

`cite_audit.py` is a pure text check: it reads the draft and `corpus.json` and reports every
marker that names a record outside the corpus, every corpus record the draft never cites, and
every prose block carrying no citation at all. It touches neither the library nor the network.

```powershell
uv run --directory "$env:CLAUDE_PLUGIN_ROOT" python "$env:CLAUDE_PLUGIN_ROOT/skills/lit-review-writer/scripts/cite_audit.py" --draft review.md --corpus corpus.json --json
```

Use the shell's own syntax for that variable: `$env:CLAUDE_PLUGIN_ROOT` in PowerShell, as above,
and `${CLAUDE_PLUGIN_ROOT}` in bash. PowerShell reads `${CLAUDE_PLUGIN_ROOT}` as a *shell*
variable, not an environment one, and silently substitutes an empty string. If the variable is
unset — you are in an endnotex9-mcp checkout rather than the installed plugin — substitute the
checkout root. The script imports nothing from the package, so plain `python cite_audit.py` works
too.

How to read the three findings:

- **`unknown`** — the draft cites a record number that is not in the corpus. Exit code 1. Either
  a typo, or a citation to something outside the agreed scope, or a fabricated record. Fix it
  before the user sees the draft; never deliver a review that fails this check.
- **`uncited`** — corpus records the review never mentions. Expected, and not an error: a 60-paper
  group does not yield 60 cited papers. It becomes the coverage ledger (`references/evidence-table.md`),
  which ships *with* the review. Scan the list before you accept it — a whole theme sitting in
  the uncited pile usually means the spine missed something.
- **`blocks_without_citation`** — prose blocks with no marker. A heuristic, and it has honest
  false positives: a transition, your own synthesis, a section framing. Either cite it or keep it
  and know why. Front matter (everything before the second heading — the title and its scope
  line) is skipped, as are sections whose heading matches `--exempt`; the defaults cover
  references, coverage, scope and appendix headings in English and Chinese.

Then, for a numbered style, renumber:

```powershell
… cite_audit.py --draft review.md --corpus corpus.json --renumber review.numbered.md
```

This rewrites `[#1204]` as `[1]` in order of first appearance and prints the matching ordered
record list. It refuses to run while `unknown` markers exist.

## 7. Bibliography, then hand over

- `get_bibliography(references=[{"rec_number": …}, …], style="gbt7714")` numbers entries **in the
  order given** — pass the ordered list `--renumber` printed and the numbers line up.
- `style="apa7"` sorts alphabetically instead, so numbering is meaningless there. For APA, leave
  the markers as author–year in the prose (`Zhang et al., 2021`) and keep `[#…]` only in the
  working draft, running the audit on that draft before the final pass.
- `get_bibtex(references=[…])` when the user wants a `.bib` to feed a manuscript.

Deliver three artefacts, not one: the **review**, the **evidence table**, and the **coverage
ledger** (records in scope, records read at each depth, records cited, records not cited). The
ledger is what makes the review checkable a year later, and it is the part a generic review
writer cannot produce.

Optional, and only if the user asks: `mark_references(rec_numbers=[…], marker="REVIEW-2026-08",
column="custom_5")` tags the cited records so they can be pulled up as a smart group in EndNote.
That is a write — `dry_run=True` first, show the counts and conflicts, have the user close
EndNote, then `dry_run=False`, and report `records_written` and `backup_path`. Per-paper reading
notes belong in `lit-deep-reader`, which owns the Notes-field merge.

## What this skill will never do

- **Never cite a record that is not in the frozen corpus.** `cite_audit.py` is the gate; a draft
  that fails it is not delivered.
- **Never state a finding or a number it did not read** in that record's own abstract or full
  text. Not from the model's memory of the field, not from a title, not from another paper's
  description of it.
- **Never present the library slice as the field's literature**, and never turn "absent from this
  collection" into "absent from the field".
- **Never report abstract-depth evidence as a full-text reading**, or let the depth mix go
  unstated in the delivered review.
- **Never write to EndNote as part of drafting.** The optional tagging in §7 is `mark_references`'
  own gated path, taken only when the user asks for it.
