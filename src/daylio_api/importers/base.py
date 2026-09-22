"""Shared import interfaces.

=============================== MAINTENANCE NOTES ===============================
Module   : src/daylio_api/importers/base.py
Scope    : Fase 0 placeholder (Task 0.2). The canonical ``ImportResult`` is
           already defined here because the audit dataclass and the importers
           (Phases 2-3) both rely on it; the actual parsing/normalisation lives
           in ``csv_importer``/``daylio_importer``/``normalizer`` (Phase 2).
================================================================================
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


@dataclass
class ImportResult:
    """Outcome of a single import run (CSV or .daylio upload/folder).

    Mirrors the ``import_jobs`` audit table (WORKPLAN §5.3). Counters reflect
    the "vince la più recente" (D3) deduplication applied by the normalizer.
    """

    user: str = ""
    source: str = "upload"  # upload | folder | daily
    filename: str = ""
    imported: int = 0       # new records inserted
    updated: int = 0        # existing records updated (upsert)
    skipped: int = 0        # rows skipped (invalid/duplicate-loser)
    errors: int = 0         # rows that failed parsing/validation
    detail: list[dict[str, Any]] = field(default_factory=list)  # per-row errors
    status: str = "pending"  # pending | running | completed | failed
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None

    def finish(self, status: str = "completed") -> None:
        """Mark the import as finished with the given status."""
        self.status = status
        self.finished_at = datetime.now(UTC)
