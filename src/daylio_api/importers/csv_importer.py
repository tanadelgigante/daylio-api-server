"""CSV importer placeholder.

=============================== MAINTENANCE NOTES ===============================
Module   : src/daylio_api/importers/csv_importer.py
Scope    : Fase 0 placeholder. Phase 2 implements the stdlib-based parser with
           UTF-8 BOM handling (``utf-8-sig``), optional columns but mandatory
           ``full_date`` (D8.b), scales column ("Name: Level k/N") and the
           "Scala salute" detection inside ``activities`` (D2).
================================================================================
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from daylio_api.importers.base import ImportResult


def parse_csv(path: str | Path, config: dict[str, Any] | None = None) -> ImportResult:
    """Parse a Daylio CSV export (implemented in Phase 2)."""
    raise NotImplementedError("parse_csv() is implemented in Phase 2 (WORKPLAN §7)")
