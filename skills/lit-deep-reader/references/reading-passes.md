# Getting the text out

Read this when a paper does not come out of `read_pdf_pages` cleanly. Everything here is about
the extraction, not about the reading.

## Finding a section's page range

`read_pdf_pages` takes a page range, not a section name, so the orientation pass has to supply
the range. Two ways, in order of cost:

1. **From the orientation pass.** Pages 1–2 usually carry the section headings of the whole
   paper if it is short, and always carry the abstract's forward references ("Section 3
   describes…"). Write down what you saw.
2. **`search_fulltext` for the heading itself.** Headings survive extraction better than body
   text does, and the reply names the page:

   ```text
   search_fulltext(queries=["Experimental", "Materials and methods", "Results and discussion"])
   ```

   This searches the whole library, so add a distinctive phrase from the paper's title to keep
   the hits on the right record, or just take the hits whose `rec_number` matches yours.

Do not page through the paper looking for a section. A 14-page paper is roughly four capped
reads; scanning it linearly costs the same as reading it twice.

## Truncation

The reply sets `truncated: true` and a `note` when it hits the character cap. The cap counts
characters across the pages returned, so a truncated reply means the *tail* of the requested
range is missing, not a uniform sample of it.

Narrow the range and call again. Do not re-read from page 1: the pages you already have are
already in the conversation.

Dense two-column papers run about 4 000–6 000 characters per page, so the default 20 000-character
cap is roughly four pages. Ask for four or five pages at a time and the truncation flag will
mostly stay off.

## Two-column layouts

PyMuPDF extracts in reading order for most publisher PDFs, but not all. The symptom is text that
alternates between two unrelated sentences, or a paragraph that breaks mid-clause and resumes
several lines later.

There is no reflow option in this server. What works instead:

- Read the affected page anyway — the *content* is all there, only the order is wrong, and a
  sentence's two halves are usually within a few lines of each other.
- Use `search_fulltext` for the specific phrase rather than reading around it. Excerpts are
  centred on the match, which is often enough to recover the sentence.
- Say so in the note if a passage stayed genuinely unreadable. Do not reconstruct it.

## Scans and image-only PDFs

A PDF with no text layer extracts to almost nothing: `index_pdfs` reports the file as processed,
and `read_pdf_pages` returns a handful of characters per page, or pages with empty `text`.

This server does not OCR. When you see it:

- Say the PDF is a scan with no text layer, and that nothing in it can be read as text.
- Offer the alternatives plainly: the user OCRs the file themselves (Acrobat, `ocrmypdf`) and
  re-runs `index_pdfs(references=[…], force=True)`, or `lit-downloader` fetches a publisher copy
  with a real text layer.
- Do not fall back to reading the abstract while calling it a close reading.

The same reply shape — processed but nearly empty — also comes from a PDF that is a cover sheet
or a "you do not have access" page. Check the first page's text before blaming OCR.

## Figures, tables and equations

Extraction gets the caption and, for vector tables, usually the cell text. It does not get:

- values that live inside a plotted figure,
- text rendered as part of a raster image,
- equations, reliably — symbols come out as the wrong glyphs, subscripts detach, and Greek
  letters vary by font embedding.

Consequences for the note:

- A number you can only see in a figure is a number you did not read. Write "the value is in
  Fig. 3 and does not extract as text", not the number you inferred from the axis.
- Table cell text that *does* extract is fair to quote, but check the header row extracted too —
  a column of numbers whose header was lost is a column of unlabelled numbers.
- Quote equations only when they extract legibly. Otherwise describe them in words and cite the
  page.

## Bilingual libraries

`search_fulltext` matches text, not concepts, and the queries list is OR-ed. Pass every surface
form in one call rather than several calls:

```text
search_fulltext(queries=["elastocaloric", "弹热", "elastocaloric effect", "弹热效应"])
```

Two things that catch people out in mixed CJK/Latin libraries:

- A Chinese paper's English abstract uses terminology the body does not. Search both.
- Latin technical terms embedded in Chinese text extract fine, so the English query often finds
  the Chinese paper; the reverse is rarely true.

## Cost control

`max_pages_per_ref` (default 3) bounds how much of one record `search_fulltext` returns; `limit`
(default 15) bounds how many records come back. When you are searching inside one known paper,
neither default is what you want — the useful shape is a low `limit` with a higher
`max_pages_per_ref`, then expand the interesting pages with `read_pdf_pages`.

At `triage` depth the whole budget is pages 1–2 plus the conclusion. Resist the fourth call.
