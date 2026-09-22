""".daylio importer placeholder.

=============================== MAINTENANCE NOTES ===============================
Module   : src/daylio_api/importers/daylio_importer.py
Scope    : Fase 0 placeholder. Phase 2 implements the ZIP (magic bytes ``PK``)
           -> ``backup.daylio`` (Base64) -> JSON decoder; maps customMoods (D1),
           tags/tag_groups (scala salute = id_tag_group=5, D2), scales
           (text_scale/number_scale) and dayEntries with month 0-based (D7).
================================================================================
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from daylio_api.importers.base import ImportResult


def parse_daylio(path: str | Path, config: dict[str, Any] | None = None) -> ImportResult:
    """Parse a .daylio backup archive (implemented in Phase 2)."""
    raise NotImplementedError("parse_daylio() is implemented in Phase 2 (WORKPLAN §7)")
