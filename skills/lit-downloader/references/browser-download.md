# Browser retrieval and EndNote boundary

## Retrieval order

1. Resolve legal OA candidates with Unpaywall and OpenAlex.
2. Download an OA candidate only after rejecting local/private destinations and unsafe redirects.
3. Validate candidate bytes and identity; a bad OA candidate falls through without attachment.
4. Search Web of Science by DOI in the dedicated visible Edge profile.
5. Require the matching title, follow **Full text at publisher**, and apply host-specific PDF
   selectors before generic selectors.
6. Capture the publisher's actual `application/pdf` response at the CDP response stage. Do not
   replay the URL with another HTTP client; signed or session-bound URLs can return HTML/403.

The browser profile lives under the per-user application config directory and is reused across a
batch. It must never be committed or packaged.

## PDF validation

Require all of the following before staging:

- `%PDF-` magic bytes and the configured size ceiling;
- successful parsing with at least one page;
- the normalized EndNote DOI in extracted text;
- title similarity of at least 0.70 against metadata or a title-sized text window;
- SHA-256 recorded in the private batch manifest.

Content-Type, filename, URL suffix, and publisher title alone are insufficient.

## EndNote write boundary

Resolve selections from the live list view. For a single row, the Reference panel is a safe
fallback; for multiple rows, every title must match an explicit selected list row. Existing
attachments are skipped.

Use EndNote's Attach File dialog with relative-copy enabled. Change and restore rows to flush the
in-memory attachment field, then observe the new `internal-pdf://` link through the read-only
`refstable` reader. Rebuild metadata and PDF-text indexes once after the batch.

Do not write EndNote's MyISAM files. A future EndNote plugin may replace UI automation with a
small in-application `attach_pdf(record_id, validated_path)` bridge, but the download and
validation boundary must stay outside that bridge.

## Adapter maintenance

Publisher selectors live in `PUBLISHER_SELECTORS` in `scripts/download_selected.py`. Add the
narrowest stable semantic selector for a host before changing generic selectors. Never use screen
coordinates, challenge bypasses, or page-context fetch as a substitute for response capture.
