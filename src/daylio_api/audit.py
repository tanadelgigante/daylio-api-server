"""Audit import jobs.

=============================== MAINTENANCE NOTES ===============================
Module   : src/daylio_api/audit.py
Scope    : Fase 0 placeholder (Task 0.2). Defines the canonical ``ImportJob``
           dataclass mapping the ``import_jobs`` table (WORKPLAN §5.3). The
           actual persistence (write rows at the end of every import, Phase 3)
           lives in ``db/sqlite_store.py``.
================================================================================
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


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
