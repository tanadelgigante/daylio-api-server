"""Serializers placeholder.

=============================== MAINTENANCE NOTES ===============================
Module   : src/daylio_api/api/serializers.py
Scope    : Fase 0 placeholder. Phase 4 maps SQLite ``day_entries`` rows to JSON
           applying the user's column selection (WORKPLAN §5.6).
================================================================================
"""

from __future__ import annotations

from typing import Any


def serialize_record(row: dict[str, Any], columns: list[str] | None = None) -> dict[str, Any]:
    """Serialize a row honouring the requested column selection (Phase 4)."""
    raise NotImplementedError("serialize_record() is implemented in Phase 4 (WORKPLAN §7)")
