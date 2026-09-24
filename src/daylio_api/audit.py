"""Audit import jobs.

=============================== MAINTENANCE NOTES ===============================
Module   : src/daylio_api/audit.py
Scope    : Fase 0 placeholder (Task 0.2). Defines the canonical ``ImportJob``
           dataclass mapping the ``import_jobs`` table (WORKPLAN §5.3). The
           persistence lives here too since Fase 3 (R3/§5.3):
           ``record_job`` / ``list_jobs`` write into ``import_jobs``. The
           coordinator (``import_flow.import_data``) is the only caller and
           always records a row — including for failed runs (status
           ``"failed"``), so every import leaves an audit trail.
================================================================================
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from daylio_api.logger import get_logger

log = get_logger("audit")


@dataclass
class ImportJob:
    """One row of the ``import_jobs`` audit table."""

    id: int | None = None
    user: str = ""
    source: str = "upload"      # upload | folder | daily
    filename: str = ""
    imported: int = 0
    updated: int = 0
    skipped: int = 0
    errors: int = 0
    detail: list[dict[str, Any]] = field(default_factory=list)
    status: str = "completed"   # running | completed | failed
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None

    def to_row(self) -> dict[str, Any]:
        """Dictionary representation ready for an INSERT/UPDATE statement."""
        return {
            "user": self.user,
            "source": self.source,
            "filename": self.filename,
            "imported": self.imported,
            "updated": self.updated,
            "skipped": self.skipped,
            "errors": self.errors,
            "detail": self.detail,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


def record_job(
    conn: sqlite3.Connection,
    *,
    user: str,
    source: str = "upload",
    filename: str = "",
    imported: int = 0,
    updated: int = 0,
    skipped: int = 0,
    errors: int = 0,
    detail: list[dict[str, Any]] | None = None,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
    status: str = "completed",
) -> int:
    """Insert one row into ``import_jobs`` and return its id.

    ``detail`` is stored as a JSON array (WORKPLAN §5.3); ``started_at``
    defaults to now(UTC). The caller owns the transaction — this function does
    NOT commit.
    """
    started = started_at or datetime.now(UTC)
    finished = finished_at or datetime.now(UTC)
    cursor = conn.execute(
        "INSERT INTO import_jobs "
        "(user, source, filename, imported, updated, skipped, errors, detail, "
        " started_at, finished_at, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            user,
            source,
            filename,
            int(imported),
            int(updated),
            int(skipped),
            int(errors),
            json.dumps(detail or [], ensure_ascii=False),
            started.isoformat(),
            finished.isoformat(),
            status,
        ),
    )
    log.debug(
        "audit job recorded id=%s user=%s status=%s imported=%d updated=%d",
        cursor.lastrowid,
        user,
        status,
        imported,
        updated,
    )
    return int(cursor.lastrowid)


def list_jobs(
    conn: sqlite3.Connection,
    user: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Return audit rows, ordered by finished_at DESC (most recent first).

    Args:
        conn: SQLite connection.
        user: optional filter by owner.
        limit: optional row cap.

    Returns:
        List of dicts with parsed ``detail`` and ISO ``started_at``/
        ``finished_at`` strings; ``ImportJob`` conversion is left to Phase 5
        read endpoints.
    """
    sql = "SELECT * FROM import_jobs"
    params: list[Any] = []
    if user:
        sql += " WHERE user = ?"
        params.append(user)
    sql += " ORDER BY finished_at DESC, id DESC"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
    rows = conn.execute(sql, params).fetchall()
    jobs: list[dict[str, Any]] = []
    for row in rows:
        job = dict(row)
        try:
            job["detail"] = json.loads(job.get("detail") or "[]")
        except ValueError:
            job["detail"] = []
        jobs.append(job)
    return jobs
