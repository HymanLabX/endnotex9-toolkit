# Output templates

Two different artefacts, and they are not the same text shortened. The **reading note** is what
goes in the conversation or a markdown file. The **Notes block** is what goes into EndNote, where
the field is plain text in a small box and anything long is unreadable.

Write the reading note first. Derive the block from it.

## Reading note — `standard`

Everything in `[…]` is a slot. Drop a section only when there is genuinely nothing to say, and
say that rather than deleting the heading silently.

```markdown
# [Short title] — [First author] et al., [Journal] [Year]

**Read:** [depth] · [n] of [N] PDF pages · rec [rec_number] · [DOI]

## Problem and claim
[One paragraph: the gap the authors name, and what they say they achieved. Their framing, not
yours — your assessment goes in Limitations.]

## Method
- **Materials / data:** [what, how much, from where]
- **Instruments / settings:** [model numbers, rates, temperatures, resolutions]
- **Procedure:** [the steps that would have to be repeated]
- **Analysis:** [statistics, model, code, fitting]
- **n =** [sample counts, and how many repeats]

## Key results
| Quantity | Value | Conditions | Where |
|---|---|---|---|
| [ΔT_ad] | [12.5 K] | [ε = 4 %, 298 K] | [PDF p. 5, Table 2] |

[Sentence for anything the table cannot hold. Numbers only if you saw them as text.]

## Comparisons and baselines
[What it was measured against, and how the margin compares to the spread. If there is no
baseline, say so in one line — it is a finding about the paper.]

## Limitations
- **Stated:** [what the authors admit, PDF p. N]
- **Visible in the method:** [single condition, no error bars, simulation only, n = 3, …]

## What it is good for
[One or two lines: how the user would cite or use this in their own work.]

## Open questions
- [What you could not settle from the text, and why — not located, in a figure, not extracted.]
```

## Reading note — `triage`

```markdown
# [Short title] — [First author] et al., [Journal] [Year]

**Read:** triage · pages 1–2 and the conclusion only · rec [rec_number]

- **What it is:** [one sentence]
- **Headline claim:** [one sentence, plus the number if it is on page 1]
- **Worth a full read because:** [or: not worth it, because —]
```

The `**Read:**` line is not decoration. It is the difference between a triage and a reading, and
it must survive into whatever the user pastes elsewhere.

## Reading note — `deep`

The `standard` note, plus:

```markdown
## Claim checks
| Claim | Asserted | Supported at | Verdict |
|---|---|---|---|
| [10 % higher than X] | [abstract] | [PDF p. 6, Fig. 4b] | supported |
| [stable over 10⁴ cycles] | [abstract] | [PDF p. 7 shows 10³] | partly supported |
| [first demonstration of Y] | [intro] | [—] | not located |

## Reproducibility
[Data availability, code availability, funding, declared conflicts — with page numbers, and
"not stated" where that is the answer.]

## Neighbourhood
[From `find_related`: 3–5 records the user already owns, one clause each on how this sits
against them. Flag anything that looks like a duplicate.]
```

`not located` means the extracted text does not carry the support — see SKILL.md §4. Never let it
read as an accusation of misconduct.

## EndNote Notes block

This is the body passed to `notes_block.py`. Plain text, no markdown syntax, under about 1 200
characters. EndNote shows it in a small field; a page of markdown there is worse than nothing.

```text
Read 2026-08-10 (standard, lit-deep-reader).
Claim: [one line].
Method: [one line — materials, n, instrument].
Numbers: [ΔT_ad 12.5 K at 4% strain, PDF p.5; fatigue 10^3 cycles, p.7].
Baseline: [what it beat, by how much — or "none reported"].
Limits: [the two that matter].
Use: [why this record is worth citing].
Open: [what the text did not settle].
```

Rules for the block:

- One line per heading, `Heading: value`. It has to stay readable in a field with no wrapping
  control.
- Keep the page references (`p.5`). They are what makes the note checkable a year later.
- Keep it in the language the user is working in. A Chinese-language library gets a Chinese block.
- Do not include the markers themselves — `notes_block.py` adds `[lit-deep-reader]` and
  `[/lit-deep-reader]`, and refuses a body that already contains either.

A second reading of the same paper replaces the block in place, so a date on the first line is
what tells the user which reading they are looking at.
