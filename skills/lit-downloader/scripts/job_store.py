"""Durable, append-audited job state for lit-downloader batches."""

from __future__ import annotations

import csv
import json
import os
import re
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1

TERMINAL_STATUSES = {
    "indexed",
    "skipped_existing",
    "permanent_failed",
    "identity_mismatch",
}
RETRYABLE_STATUSES = {"queued", "retryable_failed", "rate_limited", "needs_user_auth"}
IN_PROGRESS_STATUSES = {"resolving", "downloading", "validated", "attaching", "attached"}
ALL_STATUSES = TERMINAL_STATUSES | RETRYABLE_STATUSES | IN_PROGRESS_STATUSES

_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sanitize_message(value: object) -> str:
    """Keep reports useful without persisting signed URLs or query tokens."""
    return _URL_RE.sub("<url>", str(value or "")).strip()[:2000]


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _batch_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S-%f")


class JobStore:
    """Persist a batch after every state transition and append an event ledger."""

    def __init__(self, folder: Path, manifest: dict):
        self.folder = folder
        self.manifest_path = folder / "manifest.json"
        self.events_path = folder / "events.jsonl"
        self.report_path = folder / "batch-report.csv"
        self.manifest = manifest

    @classmethod
    def create(
        cls,
        root: Path,
        library: str,
        records: Iterable[dict],
        batch_id: str | None = None,
    ) -> JobStore:
        batch_id = batch_id or _batch_id()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", batch_id):
            raise ValueError("batch id must contain only letters, digits, dot, underscore, or dash")
        folder = root / batch_id
        if folder.exists():
            raise FileExistsError(f"Batch already exists: {batch_id}")

        jobs = []
        seen: set[int] = set()
        for record in records:
            rec_number = int(record["rec_number"])
            if rec_number in seen:
                raise ValueError(f"Duplicate EndNote record in batch: {rec_number}")
            seen.add(rec_number)
            jobs.append(
                {
                    "rec_number": rec_number,
                    "title": str(record.get("title", "")),
                    "doi": str(record.get("doi", "")),
                    "status": "queued",
                    "stage": "queued",
                    "attempts": 0,
                    "source": "",
                    "error": "",
                    "pdf_files": [],
                    "size_bytes": 0,
                    "sha256": "",
                    "pages": 0,
                    "updated_at": utc_now(),
                }
            )

        now = utc_now()
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "batch_id": batch_id,
            "library": str(library),
            "created_at": now,
            "updated_at": now,
            "jobs": jobs,
        }
        store = cls(folder, manifest)
        store._save()
        store._append_event({"event": "batch_created", "job_count": len(jobs)})
        store.write_report()
        return store

    @classmethod
    def load(cls, root: Path, batch_id: str) -> JobStore:
        folder = root / batch_id
        path = folder / "manifest.json"
        if not path.is_file():
            raise FileNotFoundError(f"Batch not found: {batch_id}")
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if manifest.get("schema_version") != SCHEMA_VERSION:
            raise RuntimeError("Unsupported lit-downloader batch schema")
        return cls(folder, manifest)

    @property
    def batch_id(self) -> str:
        return str(self.manifest["batch_id"])

    @property
    def jobs(self) -> list[dict]:
        return self.manifest["jobs"]

    def job(self, rec_number: int) -> dict:
        for job in self.jobs:
            if int(job["rec_number"]) == int(rec_number):
                return job
        raise KeyError(f"Record {rec_number} is not in batch {self.batch_id}")

    def resumable_jobs(self) -> list[dict]:
        return [
            job for job in self.jobs if job["status"] in RETRYABLE_STATUSES | IN_PROGRESS_STATUSES
        ]

    def begin_attempt(self, rec_number: int) -> dict:
        job = self.job(rec_number)
        return self.transition(
            rec_number,
            "resolving",
            stage="resolver",
            attempts=int(job.get("attempts", 0)) + 1,
            error="",
        )

    def transition(self, rec_number: int, status: str, **fields: object) -> dict:
        if status not in ALL_STATUSES:
            raise ValueError(f"Unknown job status: {status}")
        job = self.job(rec_number)
        previous = job["status"]
        job["status"] = status
        for key, value in fields.items():
            if key == "error":
                value = sanitize_message(value)
            job[key] = value
        job["updated_at"] = utc_now()
        self.manifest["updated_at"] = job["updated_at"]
        self._save()
        self._append_event(
            {
                "event": "status_changed",
                "rec_number": int(rec_number),
                "from": previous,
                "to": status,
                "stage": job.get("stage", ""),
                "error": job.get("error", ""),
            }
        )
        self.write_report()
        return job

    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for job in self.jobs:
            status = str(job["status"])
            counts[status] = counts.get(status, 0) + 1
        return counts

    def _save(self) -> None:
        _atomic_json(self.manifest_path, self.manifest)

    def _append_event(self, event: dict) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        payload = {"at": utc_now(), "batch_id": self.batch_id, **event}
        with self.events_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def write_report(self) -> None:
        fields = [
            "rec_number",
            "title",
            "doi",
            "status",
            "stage",
            "attempts",
            "source",
            "pages",
            "size_bytes",
            "sha256",
            "pdf_files",
            "error",
            "updated_at",
        ]
        temporary = self.report_path.with_name(f".{self.report_path.name}.{os.getpid()}.tmp")
        with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            for raw in self.jobs:
                row = dict(raw)
                row["pdf_files"] = " | ".join(raw.get("pdf_files", []))
                writer.writerow(row)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.report_path)
