# Structures

Four scopes, four different documents. A review of a research direction asks "what is known"; a
review of a lab asks "what does this group do, and how did it get here"; a review of one
researcher asks "what is this person's contribution"; a related-work section asks "where does the
user's own work sit". Using the direction outline for all four is the commonest way this skill
produces something that reads like a report and answers nobody's question.

Pick the spine after the evidence table exists, from what it shows.

## Choosing the spine

| The evidence table shows | Spine | Why |
|---|---|---|
| 3–6 recurring problem areas, spread evenly across years | **Thematic** | The default, and the only one that makes synthesis natural |
| A clear before/after — a method, a material, a result that split the field | **Chronological** | Only when the timeline carries meaning; "papers by year" is not a spine |
| The same question attacked by experiment, simulation and theory | **Methodological** | Good when the disagreements are methodological rather than substantive |
| Distinct material systems / datasets / populations, each with its own literature | **By system** | Common in materials and clinical corpora |
| Several open questions, each with partial answers | **By question** | The strongest form for a review that ends in future work |

One spine. Sub-sections may use a second axis — a thematic review whose sections run
chronologically inside is fine, and usually good. Two top-level axes are two reviews interleaved.

## Research direction or topic group

```markdown
# [Direction] — a review of [N] records in [scope]

**Scope:** [group / topic / query] · **Corpus:** [N] records, [year]–[year] ·
**Read:** [n] full text, [n] abstract, [n] metadata only · **Generated:** [date]

## 1. Scope and what this covers
[What the slice is and how it was resolved — the group name, the author variants, the queries.
Then the sentence that must not be omitted: this reviews the records in this library slice, not
the field. One paragraph.]

## 2. The shape of the corpus
[The census from SKILL.md §2: size, year distribution, venue concentration, type mix, PDF
coverage. Two or three sentences plus a small table. Descriptive only — no claims about the
field.]

## 3. [Theme A]
[Topic sentence naming the theme. Then synthesis: what several records agree on, where they
diverge, what the strongest evidence is and at what depth it was read. Every claim marked
`[#rec]`. Close on what this theme leaves unresolved.]

## 4. [Theme B]
## 5. [Theme C]

## 6. Methods and evidence base
[What the corpus is made of methodologically: sample sizes, instruments, simulation vs
experiment, whether results are reproduced across records. This is where a corpus's real
weaknesses show.]

## 7. Disagreements
[Where records conflict, one item each: the two positions, the records, and what differs between
them (material, conditions, measurement). Do not resolve what the evidence cannot.]

## 8. What is missing from this collection
[Gaps as collection facts. Each with what would fill it: a search, an import, a database check.
Never phrased as a gap in the field.]

## 9. Coverage ledger
[The table from evidence-table.md.]

## References
[From get_bibliography.]
```

## A lab or research group

The question is what the group *does*, not what is known about a topic. Keep the sections above
for scope, census and ledger; replace the middle with:

```markdown
## Research threads
[2–5 threads, each: what the group works on, over which years, with which records. A thread is a
line of work, not a keyword.]

## Trajectory
[How the threads started, split, merged or stopped. Anchor every turn to records and years.
Where the corpus thins out after a year, say so — it is usually the user's import history, not
the lab's output.]

## Methods and platform
[The instruments, datasets, simulation codes or animal models that recur. A group is often
identifiable by its platform more than its topic.]

## Signature results
[The 3–6 records that carry the group's reputation in this corpus, with their headline numbers
at full-text depth. Say what each is cited for.]

## Collaboration and venues
[Recurring co-authors and institutions visible in the corpus; the journals and conferences the
work lands in. Corpus-level facts only — this is not an evaluation of people.]

## Where they are going
[Only what the recent records support, marked as inference.]
```

Two cautions. First, an author-resolved corpus is a **substring match on the authors field**: any
claim about "the group's output" is a claim about the records that matched, and homonyms and
missing name forms both bite (SKILL.md §1). Second, keep it about the work. A review of a lab is
not an assessment of the people in it, and productivity counts from one user's library are not
evidence about anybody's career.

## One researcher

```markdown
## The arc
[Chronological, one paragraph per phase, each phase named for what changed.]

## Signature contributions
[3–5 records with what each contributed, at full-text depth where possible.]

## Methods they reach for
## How the work evolved
[Question, method or scale — say which, and back it with records.]

## What to read first
[3 records, and one sentence each on why. This is usually the most-used section: someone is about
to meet this person, review their paper, or join their group.]
```

Same caution, harder: with one name the homonym risk is at its highest, and one wrong record
distorts the whole arc. Get the resolved list confirmed.

## Related-work section for the user's own paper

Shorter, positioning-led, and it ends at the user's contribution.

```markdown
[Paragraph 1: the established approach and its records.]
[Paragraph 2: the limitation everything in the corpus shares, or the axis on which they differ.]
[Paragraph 3: the closest work, treated fairly — what it does well before what it does not do.]
[Paragraph 4: the gap this collection shows, and the user's contribution stated against it.]
```

Ask what the user's contribution is before drafting; without it there is no axis to arrange the
records on. Treating the nearest competing work fairly is not politeness — a reviewer of the
user's paper is often its author.

## Length

| Tier | Body words (EN) / 正文字数 (ZH) | Records cited | Fits |
|---|---|---|---|
| Briefing | 600–900 / 1 000–1 500 | 10–20 | "Tell me what this group is about" |
| Standard | 1 800–3 000 / 3 000–5 000 | 25–50 | The default review |
| Full | 4 000+ / 7 000+ | 50+ | A thesis chapter; expect `deep` depth to be needed |

Length follows the corpus. Forty records do not support a 6 000-word review, and padding shows up
as paragraphs that summarise one record each.

## Paragraph craft

The unit of a review is a paragraph that holds several records together:

```markdown
[Topic sentence: the claim the paragraph makes — not "several studies have examined X".]
[Evidence: 2–4 records, each with a marker, arranged by what they show rather than by year.]
[Relation: agreeing, extending, contradicting, or measuring the same thing differently.]
[Assessment: how strong this is — how many records, at what depth, with what limitations.]
```

Comparative moves worth having in reach: *converge* (several records, one conclusion) · *extend*
(later record generalises an earlier one) · *contradict* (same question, different answers) ·
*reframe* (a record redefines the question) · *stand alone* (one record, unreplicated — say so).

In Chinese, the same moves: 一致 / 拓展 / 分歧 / 重构 / 孤证. Keep technical terms in both
languages on first use in a bilingual corpus, and do not translate a paper's title in the prose
if the bibliography carries the original.

## Before delivering

- Does every paragraph in the body carry at least one `[#rec]` marker, or a stated reason not to?
- Does every number carry a record **and** a page, from a `fulltext` record?
- Is there at least one disagreement in the review? A corpus of 30+ records with none usually
  means they were read too shallowly to notice.
- Are gaps phrased as collection gaps?
- Does the header state the depth mix, and does the ledger match it?
- Would a reader who opened three of the cited records find them saying what the review says?

## Anti-patterns

- **The annotated list.** One paragraph per paper, in some order. It is what a review turns into
  when the evidence table was never built.
- **The invisible corpus.** A review whose scope, size and depth mix are not stated, so a reader
  cannot tell it covers 40 records or 400.
- **Borrowed knowledge.** A claim the model knows from the field but no corpus record supports.
  This is the failure mode with the highest cost, and `cite_audit.py` catches only the version
  that carries a fake record number — the version with no marker at all is caught by the
  block check and by you.
- **Smoothed conflict.** Two contradicting records averaged into one hedged sentence.
- **Gap inflation.** "No work addresses X" from a 60-record slice of one person's library.
