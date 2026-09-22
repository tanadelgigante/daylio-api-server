"""Record normalizer placeholder.

=============================== MAINTENANCE NOTES ===============================
Module   : src/daylio_api/importers/normalizer.py
Scope    : Fase 0 placeholder. Phase 2 implements the canonical record shape
           ``{full_date, date, weekday, time, mood_num 1-5, mood_label,
           activities, scales {name: {value, label}}, scala_salute,
           note_title, note}`` plus the daily "vince la più recente" rule
           (D1/D2/D3/D8).
================================================================================
"""

from __future__ import annotations

from typing import Any


def normalize(
    rows: list[dict[str, Any]],
    config: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Normalize raw parsed rows (implemented in Phase 2)."""
    raise NotImplementedError("normalize() is implemented in Phase 2 (WORKPLAN §7)")
