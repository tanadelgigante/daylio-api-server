"""Shared import interfaces and canonical normalized record.

=============================== MAINTENANCE NOTES ===============================
Module   : src/daylio_api/importers/base.py
Scope    : Fase 0 (Task 0.2) placeholder + Fase 2 (Tasks 2.1-2.3). The
           canonical ``ImportResult`` is the audit contract shared with Fase 3
           (``import_jobs`` table, WORKPLAN §5.3); ``NormalizedEntry`` is the
           daily record produced by ``normalizer.normalize`` and consumed by
           the SQLite/Influx stores in Fase 3.

ImportResult extension (Fase 2):
  - ``records``   raw cleaned rows (one per valid CSV row / .daylio entry);
  - ``total_rows`` all input rows/entries (including the invalid ones);
  - ``warnings``  non-fatal issues counter (unknown CSV column, ignored
                  .daylio assets D11, unknown scales/mood at normalize time);
  - ``warn()``/``add_error()`` helpers append an entry to ``detail``.

NormalizedEntry contract (Task 2.3):
  - ``full_date``    ISO ``YYYY-MM-DD`` (unique day key, D3);
  - ``mood_level``   int 1..5 (1=best .. 5=worst, D1); 0 = unresolved label;
  - ``mood_label``   configured mood label (or the raw unknown label);
  - ``activities``   clean list of activity/tag labels (Scala salute removed);
  - ``scales``       list of ``{scale_name, label, rank, levels_count,
                    scale_key}`` for the generic "column" scales (e.g. Stress);
  - ``scala_salute`` ``{label, rank}`` or None (8 levels, D2), extracted from
                    activities (CSV) or tag-group 5 (.daylio);
  - ``source``       "csv" | "daylio" (audit provenance);
  - ``date``/``weekday`` derived from ``full_date`` (D8.a); ``time`` kept.
===============================================================================
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

#: Recognized source formats (D7 magic-byte detection).
FORMAT_CSV = "csv"
FORMAT_DAYLIO = "daylio"
#: ZIP local-file magic bytes marking a .daylio archive (PK\x03\x04).
_ZIP_MAGIC = b"PK\x03\x04"
#: UTF-8 byte-order-mark (EF BB BF).
_BOM = b"\xef\xbb\xbf"


@dataclass
class ImportResult:
    """Outcome of a single import run (CSV or .daylio upload/folder).

    Mirrors the ``import_jobs`` audit table (WORKPLAN §5.3). ``imported`` /
    ``skipped`` are filled by the normalizer (deduplicated daily records);
    ``errors`` counts rows rejected by the parser; ``warnings`` counts
    non-fatal issues tracked in the traffic-light (R12).
    """

    user: str = ""
    source: str = "upload"  # upload | folder | daily
    filename: str = ""
    records: list[dict[str, Any]] = field(default_factory=list)  # raw cleaned rows
    imported: int = 0       # unique daily records after normalization
    updated: int = 0        # existing records updated (upsert, Phase 3)
    skipped: int = 0        # rows skipped (invalid/duplicate-loser)
    errors: int = 0         # rows that failed parsing/validation
    warnings: int = 0       # non-fatal issues (unknown columns, ignored assets)
    total_rows: int = 0     # every input row/entry, valid or not
    detail: list[dict[str, Any]] = field(default_factory=list)  # per-row errors
    status: str = "pending"  # pending | running | completed | failed
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None

    def finish(self, status: str = "completed") -> None:
        """Mark the import as finished with the given status."""
        self.status = status
        self.finished_at = datetime.now(UTC)

    def warn(self, message: str, **context: Any) -> None:
        """Record a non-fatal warning in the traffic-light counters (R12)."""
        self.warnings += 1
        self.detail.append({"level": "warning", "message": message, **context})

    def add_error(self, message: str, **context: Any) -> None:
        """Record a row-level error in ``detail`` (audit JSON payload)."""
        self.errors += 1
        self.detail.append({"level": "error", "message": message, **context})


@dataclass
class NormalizedEntry:
    """Canonical daily record produced by the Fase 2 normalizer.

    Exactly one entry per ``full_date`` (D3). ``raw`` keeps the source record
    for audit/troubleshooting. ``date`` and ``weekday`` are derivations from
    ``full_date`` (D8.a); ``time`` is the winning row/entry time.
    """

    full_date: str
    mood_level: int  # 1..5 (D1), 0 = unresolved mapping -> warning
    mood_label: str
    activities: list[str]
    scales: list[dict[str, Any]]  # {scale_name,label,rank,levels_count,scale_key}
    scala_salute: dict[str, Any] | None  # {label, rank} (1=ottimo..8=terribile)
    note_title: str
    note: str
    source: str  # "csv" | "daylio"
    raw: dict[str, Any] = field(default_factory=dict)
    date: str = ""  # derived: same as full_date (D8.a)
    weekday: str = ""  # derived from full_date (D8.a)
    time: str = ""  # winner time, for audit

    def as_dict(self) -> dict[str, Any]:
        """Return the serializable canonical record (Fase 3 stores/API)."""
        return {
            "full_date": self.full_date,
            "date": self.date,
            "weekday": self.weekday,
            "time": self.time,
            "mood": self.mood_level,
            "mood_label": self.mood_label,
            "activities": self.activities,
            "scales": self.scales,
            "scala_salute": self.scala_salute,
            "note_title": self.note_title,
            "note": self.note,
            "source": self.source,
        }


def detect_format(path: str | Path) -> str:
    """Detect the import format from the file magic bytes (D7).

    A .daylio archive is a ZIP whose local header starts with ``PK\\x03\\x04``.
    Everything else is treated as CSV (a UTF-8 BOM or plain text). Returns
    ``FORMAT_CSV``, ``FORMAT_DAYLIO`` or ``"unknown"``.
    """
    try:
        with Path(path).open("rb") as fh:
            head = fh.read(4)
    except OSError:
        return "unknown"
    if head == _ZIP_MAGIC:
        return FORMAT_DAYLIO
    if head.startswith(_BOM) or head[:2].isascii():
        return FORMAT_CSV
    return "unknown"
