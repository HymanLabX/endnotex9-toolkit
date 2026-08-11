---
name: lit-deep-reader
description: Close-read one paper already in an EndNote X9 library through endnotex9-mcp -- resolve the record, extract and read its PDF in passes, pull out the problem, method, data, results, limitations and the numbers that matter, verify specific claims against the full text, place it among neighbouring references, and optionally save a compact reading note back into the record's Notes field without losing what is already there. Use when the user invokes /lit-deep-reader, or asks to read, digest, summarize in depth, appraise, or 精读 a specific paper in their library.
---

# lit-deep-reader

One paper, read properly, from the copy already in the user's library. Every tool named below is
an MCP tool on the `endnotex9` server.

This is not a summarizer. A summary of the abstract is what the user already has. The deliverable
is a reading that could only come from the full text: the actual method, the actual numbers, what
the authors compared against, and what they did not show.

## Load only what is needed

- `references/reading-passes.md` — before reading the PDF: how to find a section's page range,
  what to do about truncation, two-column and scanned files, figures and tables, and bilingual
  search. Read it the first time a paper does not come out cleanly.
- `references/note-templates.md` — before writing the note. Holds the full markdown template per
  depth, the compact EndNote Notes block, and the claim-check table.
- `scripts/notes_block.py` — only when saving the note back to EndNote (§7).

## 0. Agree the depth before spending the tokens

Ask which of these the user wants if they have not said. Default to **standard**.

| Depth | Reads | Answers | Typical cost |
|---|---|---|---|
| `triage` | pages 1–2 and the conclusion | Is this worth my time, and what is it actually about? | 2–3 calls |
| `standard` | orientation, method, results, ends | The full reading note below | 5–8 calls |
| `deep` | standard, plus every table and figure caption, the claim checks in §4, and the neighbourhood in §6 | Could I build on this, and do its numbers hold up? | 12+ calls |

A `triage` reading is honest work, but say in the output that it was a triage. Never present two
pages as a full reading.

## 1. Resolve exactly one record — do not guess

Take the user's reference (a title, a DOI, an EndNote record number, or "the one I have
selected") and call `get_reference_details` with **one** selector. If the reply comes back with
candidates instead of a record, the selector was ambiguous: show the candidates and ask. Never
pick the first one, and never proceed on a title match you had to guess at.

If the user names a paper that is not in the library, stop and say so. This skill reads what is
already there; getting a missing PDF is `lit-downloader`'s job, and importing a missing record is
the user's.

Keep the `rec_number` from this step. Everything below uses it.

## 2. Make sure the full text is actually available

Extraction is incremental and opt-in, so a record can be in the library with no text indexed:

```text
index_pdfs(references=[{"rec_number": 42}])
```

Read the reply. If nothing was extracted, the record has no attached PDF (or PyMuPDF is not
installed — the `pdf` extra). Say which, and offer `lit-downloader` for the first case. Do not
fall back to writing a summary from the abstract while implying you read the paper; if the user
wants an abstract-level answer, tell them that is what they are getting and label the output
`abstract only`.

A PDF that extracts to almost nothing is a scan, not a failure of this skill — see
`references/reading-passes.md`.

## 3. Read it in passes, not in one gulp

`read_pdf_pages` is capped (20 000 characters by default) and sets `truncated` in the reply. Work
in passes rather than re-reading from page 1 each time:

1. **Orientation** — pages 1–2. Problem, claimed contribution, and the shape of the paper. Note
   where each later section starts; that is what the next three passes aim at.
2. **Method** — the methods/experimental range you just located.
3. **Results** — the pages carrying the figures and tables the abstract's claims rest on.
4. **Ends** — discussion, limitations, conclusion; also where funding, conflicts and
   data-availability statements live.

Page numbers in the reply are **PDF page indices**, which often differ from the printed journal
page numbers. Cite them as `PDF p. 4`, and say so once, so the user can find what you quote.

## 4. Verify the claims that matter

For a specific claim, number or phrase, `search_fulltext` is faster than paging: it reports which
pages matched and an excerpt from each, which `read_pdf_pages` can then expand.

```text
search_fulltext(queries=["adiabatic temperature change", "绝热温变", "ΔT_ad"])
```

At `deep` depth, run this deliberately rather than opportunistically. Take the two or three
claims the paper's value rests on — the headline number, the comparison against prior work, the
stated novelty — and for each one record: the claim, where it is asserted, where it is
*supported*, and the verdict. Three verdicts only, and the third is the useful one:

- **supported** — the body text carries the evidence, at the page you cite.
- **partly supported** — the evidence exists but is narrower than the claim (one condition, one
  sample, no error bars).
- **not located** — you could not find support in the extracted text. Say that, and say whether
  the likely cause is a figure that did not extract as text.

`not located` is not an accusation, and must not be presented as one. It is a statement about
what the extracted text shows.

## 5. What to produce

The template lives in `references/note-templates.md`. The sections, and what makes each of them
worth writing:

- **Problem and claim** — what gap, and what the authors say they achieved. One short paragraph.
- **Method** — enough that the user could judge whether it applies to their own work: materials,
  sample counts, instruments and settings, the model or code, the statistics.
- **Key results with numbers** — the actual values and units, with page numbers. Not "significant
  improvement".
- **Comparisons and baselines** — what it was measured against. A result with no baseline is a
  finding about the paper, so say it plainly.
- **Limitations** — both the ones the authors state and the ones visible in the method (sample
  size, single condition, no error bars, simulation-only).
- **What it is good for** — how the user would cite or use this, in one or two lines.
- **Open questions** — what you could not settle from the text, marked as such rather than
  smoothed over.

Two rules on honesty here, because they are what makes this skill worth running:

- **Never state a number you did not see in the extracted text.** If a figure carries the value
  and the extraction only got the caption, say the value is in the figure and unreadable as text.
- **Distinguish what the authors claim from what they show.** Those diverge often enough to
  matter, and noticing it is most of the value of reading the full text at all.

## 6. Place it in the library

`find_related` gives a term-overlap neighbourhood — nearby references the user already owns. Use
it for one short "how this sits against what you already have" note, and mention when a neighbour
looks like a duplicate (that is `lit-clean`'s dedup flow, not this skill's job).

`get_citation(style="apa7" | "gbt7714")` when the user wants the formatted reference to paste;
`get_bibtex` when they want the `.bib` entry.

## 7. Optionally save the note back — with the usual gate

EndNote's Notes field is one plain-text blob and `update_reference_fields` **replaces** it. A
record that already carries an import dump, a supervisor's comment or an earlier reading would
lose it. `notes_block.py` composes the merged value instead: it wraps the note in
`[lit-deep-reader] … [/lit-deep-reader]` markers, updates that block in place if one is already
there, and otherwise appends it after whatever the record already holds.

```powershell
uv run --directory "$env:CLAUDE_PLUGIN_ROOT" python "$env:CLAUDE_PLUGIN_ROOT/skills/lit-deep-reader/scripts/notes_block.py" --rec 42 --note-file note.txt --json
```

Use the shell's own syntax for that variable: `$env:CLAUDE_PLUGIN_ROOT` in PowerShell, as above,
and `${CLAUDE_PLUGIN_ROOT}` in bash. PowerShell reads `${CLAUDE_PLUGIN_ROOT}` as a *shell*
variable, not an environment one, and silently substitutes an empty string.

If `CLAUDE_PLUGIN_ROOT` is unset — you are in an endnotex9-mcp checkout rather than the installed
plugin — substitute the checkout root; the script finds the package by walking up from its own
path, so both layouts work. Write the block body (the compact form from
`references/note-templates.md`, not the long markdown) to a file first, or pass `--note-file -`
and pipe it in.

The script **never writes to the library**. It reads the current Notes value and prints the
merged one, plus the exact `update_reference_fields` payload. The write itself is the MCP tool's,
under its own gate:

1. `update_reference_fields(updates=[…], dry_run=True)` first, always. Show the diff.
2. Check the diff's `old_value` really is contained in the `new_value` before proposing the
   non-dry-run call. If the script reported `mode: "replace"`, text is being dropped — the user
   has to agree to that specifically.
3. Tell the user to close EndNote and get their acknowledgement before the `dry_run=False` call.
   The server's lock check is a safety net, not a substitute for asking.
4. Report `records_written` and `backup_path` afterwards.

Keep the saved block compact and plain-text — this is EndNote's Notes field, not a document. The
long version belongs in the conversation or a markdown file next to the user's other notes.

## What this skill will never do

- **Never invent bibliographic data or numbers.** Everything reported comes from the record or
  the extracted text.
- **Never write any field other than `notes`**, and never without the dry-run gate above.
- **Never overwrite existing Notes text silently.** Merge through `notes_block.py`, or get
  explicit agreement to replace.
- **Never claim to have read a paper whose text was not extracted**, and never let a `triage`
  reading be mistaken for a full one.
