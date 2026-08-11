# Durable batch jobs

## Files and privacy

Each batch is stored under the per-user application config directory:

```text
lit-downloader/jobs/<batch-id>/
├── manifest.json
├── events.jsonl
└── batch-report.csv
```

`manifest.json` is atomically replaced after every state change. `events.jsonl` is append-only.
The CSV is a human-readable projection. Files may contain titles, DOIs, EndNote record numbers,
hashes, and local attachment names. They must not contain cookies, credentials, or source URLs;
errors redact URLs.

## State model

```text
queued → resolving → downloading → validated → attaching → attached → indexed
                  ↘ retryable_failed / rate_limited / needs_user_auth
preflight         ↘ skipped_existing / permanent_failed
validation        ↘ identity_mismatch
```

An interrupted in-progress state is resumable. `attached` is also resumable so index refresh can
finish without downloading or attaching again. Terminal identity mismatches require inspection,
not automatic retry.

## Batch behavior

- Queue exactly the selected EndNote records, including records later skipped for an existing PDF.
- Persist all selected work before starting network access.
- Process sequentially with a default 10-second gap and a default invocation limit of 20.
- Reuse one browser context across the run.
- Stop immediately for user authentication; stop after the configured number of consecutive
  rate/access failures.
- Refresh indexes once after all newly attached records.
- Restore the original EndNote selection before returning.

To resume, reopen the same library and select all records still requiring download. Records already
at `attached` need not be selected because only index refresh remains. The helper rejects a batch
from a different library.
