---
name: lit-classifier
description: Build a reviewable topic classification over an EndNote X9 library with endnotex9-mcp -- profile the corpus, propose a facet taxonomy, write bounded declarative rules, evaluate them, audit a holdout sample, and produce the read-only Custom 1 writeback preview. Use when the user invokes /lit-classifier, or asks to classify, categorize, tag by topic, or build a taxonomy over their EndNote library.
---

# lit-classifier

Drive the `endnotex9` MCP server's classification workflow. Every tool named below is an MCP
tool on that server; this skill adds no scripts of its own.

The workflow is deliberately gated: the server refuses to skip steps, and two of the gates are
the *user's* decision, not yours. Your job is to do the work between the gates well and to stop
at each one.

## The one thing to say up front

**There is no commit path for the classification result.** `prepare_classification_writeback`
produces a fingerprinted, read-only diff of what `Custom 1` would become and stores it in the
local index — nothing is ever written into EndNote by this workflow. Tell the user that before
they invest in a taxonomy, not after. If they want labels physically in the library at the end,
the honest options are:

- keep the preview as the deliverable and re-run it whenever the library changes, or
- pick the labels they care about and write them into a *different* column with
  `mark_references` (Custom 2–7 — `custom_1` is reserved for this workflow and refused by that
  tool), one `mark_references` call per label, with its usual dry-run-then-confirm and backup, or
- copy the preview diff out and apply it by hand in EndNote.

## Gate map

| Step | Tool | Who decides |
|---|---|---|
| 1 | `profile_library`, `sample_references` | you |
| 2 | `create_classification_session` | you |
| 3 | `save_classification_taxonomy` → `approve_classification_taxonomy` | **user approves** |
| 4 | `save_classification_rules` → `freeze_classification_rules` | you, after showing the rules |
| 5 | `evaluate_classification_rules`, `list_unresolved_classifications` | you |
| 6 | `create_classification_audit` → `submit_classification_audit` → `get_classification_report` | **user labels the sample** |
| 7 | `prepare_classification_writeback` | you, after the report |

Never call `approve_classification_taxonomy` on a taxonomy the user has not seen printed out in
the conversation. Approval is immutable, and it is the point of the whole design.

## 1–2. Profile, sample, open a session

`profile_library` for the corpus's own vocabulary, `sample_references` for concrete records to
read. Propose facets from what is actually in *this* library — not from a generic field taxonomy
you already know. Then `create_classification_session`; it pins a library fingerprint, so a later
`evaluate_classification_rules` refuses to run if the index moved underneath it.

If the library changes mid-session (a `lit-cleaner` commit, new imports), the session is dead. Say
so and start a new one rather than working around the fingerprint.

## 3. Taxonomy, then the user's approval

`save_classification_taxonomy` takes:

```json
{"facets": [
  {"id": "topic", "mode": "single", "slot": "custom_1", "approved": true,
   "labels": ["elastocaloric", "magnetocaloric", "shape-memory", "other"]},
  {"id": "method", "mode": "multiple", "labels": ["experiment", "simulation"]}
]}
```

Constraints the server enforces, worth designing around rather than discovering:

- Exactly **one** facet may carry `"approved": true`, and it must be `mode: "single"` with
  `slot: "custom_1"`. Extra facets may be saved unapproved for analysis; they cannot be evaluated.
- An approved facet needs declared labels; facet ids and label ids must be unique and non-empty.
- Include a catch-all label (`other`, `unclassified`) in the approved facet. Step 7 fails closed
  on *any* record without an assignment, so a taxonomy with no catch-all cannot reach a preview.

Saving returns a hash. Print the taxonomy and the hash, ask the user to approve, and only then
call `approve_classification_taxonomy` with that exact hash.

## 4. Rules

`save_classification_rules` takes `{"rules": [...]}`. Each rule is:

```json
{"id": "r-elasto", "facet": "topic", "label": "elastocaloric", "priority": 10,
 "when": [{"op": "contains_any", "field": ["title", "abstract", "keywords"],
           "values": ["elastocaloric", "弹热"]}]}
```

- Predicates: `contains_any`, `contains_all`, `prefix`, `token`, `year_range`. Nothing else —
  arbitrary Python, SQL and regex are rejected by design, so do not try to express a rule you
  cannot say with these five.
- Fields: `title`, `title_short`, `abstract`, `keywords`, `authors`, `editors`,
  `container_title`, `collection_title`, `year`, `publisher`, `language`, `ref_type`.
  `year_range` accepts only `year`, with integer `min`/`max`.
- `when` is a non-empty list; entries may nest (bounded depth and total operation count).
- `priority` breaks ties between rules that both match; higher wins. Give the catch-all rule the
  lowest priority.
- For a bilingual library, put the Chinese and English surface forms in the same `values` list.
  The rule engine matches text, not concepts.

Show the rules to the user in a readable form before freezing. Freezing binds the exact hash so
the evaluation is reproducible; a changed rule means a new save and a new freeze.

## 5. Evaluate and read the long tail

`evaluate_classification_rules`, then `list_unresolved_classifications` for records that matched
nothing or matched conflicting rules. Report both counts honestly — coverage first, conflicts
second. Do not quietly assign the unresolved ones yourself; either widen a rule's `values` and
re-freeze, or accept that they fall to the catch-all label, and say which you did.

Iterating here is normal: save rules → freeze → evaluate → look at unresolved → repeat. The
taxonomy approval survives; only the ruleset version changes.

## 6. Audit — the user's second gate

`create_classification_audit` draws a reproducible stratified sample (`holdout=True` keeps it
clear of records used by earlier audits). Present those records to the user with enough context
to label them — title, journal, year, and the assigned label — and ask for their labels. Submit
with `submit_classification_audit`; labels are immutable once submitted, so do not guess on the
user's behalf, and do not submit the rules' own output as if it were a human label.

`get_classification_report` gives coverage, conflicts, a confusion matrix and per-label
precision/recall **on the audit set only**. Say that when reporting it. It is not library-wide
accuracy, and presenting it as such is the single easiest way to mislead the user here.

If precision or recall on a label is poor, go back to step 4. That is the loop working, not a
failure.

## 7. Writeback preview

`prepare_classification_writeback(session_id, idempotency_key)` — the key is any stable string
you choose for this attempt; reusing it returns the same stored plan instead of a second one.

It fails closed, on purpose. The common failures and what they mean:

- *"N records are unresolved or abstained"* — the preview is atomic; every indexed record needs a
  decision. Widen the rules or lean on the catch-all label, then re-evaluate.
- *"assignments contain conflicts, abstentions, or unsupported facets"* — resolve the conflicts
  from step 5 first.
- *"session has no assignments"* — evaluation has not run against the current frozen ruleset.
- A source-fingerprint mismatch — the library changed since the session was created. Start over.

Report `changes`, `unchanged`, and a handful of example diffs (`rec_number`, `old_value`,
`new_value`). Then restate that nothing was written, and offer the three options from the top of
this file.

## What this skill will never do

- **Never write `custom_1`.** No commit path exists; do not build one, and do not route around it
  with `update_reference_fields` (that tool refuses `custom_1` too).
- **Never approve a taxonomy or submit audit labels on the user's behalf.** Those two gates are
  the reason this workflow is trustworthy at all.
- **Never report audit-set metrics as library-wide accuracy.**
