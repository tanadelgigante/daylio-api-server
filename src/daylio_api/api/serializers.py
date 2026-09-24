"""Serializers for API responses (Fase 4 — R2/D10).

=============================== MAINTENANCE NOTES ===============================
Module   : src/daylio_api/api/serializers.py
Scope    : Fase 4 maps SQLite ``day_entries`` rows to the canonical JSON record
           applying the user's column selection (WORKPLAN §5.6). It is the
           single source of the canonical per-record shape that the parametric
           endpoints (``/api/records``, ``/api/records/<full_date>``) return.

Canonical shape (WORKPLAN §5.2/§5.3/§5.6, D10):
    full_date, date, weekday, time, mood, mood_label, activities (JSON -> list),
    scales (object ``{<scale label>: {"value": int, "label": str,
    "levels_count": int?}}``, §5.3), scala_salute (extracted convenience copy),
    note_title, note, source, created_at, updated_at.

Derivation (D8.a): ``day_entries`` stores ONLY ``full_date``; ``date`` and
    ``weekday`` are derived here from ``full_date`` when the stored row does
    not carry them (it never does today — the upsert does not persist them).
    ``weekday`` uses the Italian names (lunedì..domenica).

Scales enrichment (Task 4.2): the caller may pass ``scale_meta`` mapping a
    scale name OR display label -> ``levels_count`` (from ``scales_config``);
    when present the ``levels_count`` key is added to each ``scales`` item.
    Extending the object with new keys is backward compatible for consumers
    that only read ``value``/``label``.

Column selection: ``columns`` is an optional allow-list of canonical keys
    (snake_case). Unknown names are silently ignored here — the routes
    validate them against ``columns_config`` before calling this function.
================================================================================
"""

from __future__ import annotations

import calendar
import json
from datetime import datetime
from typing import Any

#: Italian weekday names, indexed by ``datetime.weekday()``/``calendar.weekday``
#: (Monday == 0), matching the Italian Daylio UI (D8.a).
_WEEKDAY_IT: tuple[str, ...] = (
    "lunedì",
    "martedì",
    "mercoledì",
    "giovedì",
    "venerdì",
    "sabato",
    "domenica",
)


def _derive_date_weekday(full_date: str) -> tuple[str, str]:
    """Derive ``date`` and the Italian ``weekday`` from full_date (D8.a).

    Returns ``(full_date as-is, "")`` when the value is not a valid
    ``YYYY-MM-DD`` so that a corrupt row never breaks the response.
    """
    if not full_date:
        return "", ""
    try:
        parsed = datetime.strptime(full_date, "%Y-%m-%d")
    except ValueError:
        return full_date, ""
    return parsed.strftime("%Y-%m-%d"), _WEEKDAY_IT[calendar.weekday(
        parsed.year, parsed.month, parsed.day
    )]


def _parse_json_column(value: Any, default: Any) -> Any:
    """Parse a JSON-encoded column, tolerating already-parsed values."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return default
    return value if value is not None else default


def serialize_record(
    row: dict[str, Any],
    columns: list[str] | None = None,
    scale_meta: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Serialize a ``day_entries`` row to the canonical API record.

    Args:
        row: row as returned by the store (JSON columns are parsed here when
            they arrive as strings, tolerating either representation).
        columns: optional allow-list of canonical snake_case keys to return.
            ``None`` returns the full canonical record.
        scale_meta: optional ``{key: levels_count}`` where key is a scale name
            or its display label; the value is merged into each ``scales``
            item as ``levels_count`` (Task 4.2).

    Returns:
        Canonical dict (snake_case keys). With ``columns`` only the requested
        keys are present.
    """
    activities = _parse_json_column(row.get("activities"), default=[])
    if not isinstance(activities, list):
        activities = []

    scales_obj = _parse_json_column(row.get("scales"), default={})
    if not isinstance(scales_obj, dict):
        scales_obj = {}

    # Normalize each scale item to {value, label} and enrich with levels_count.
    scale_meta = scale_meta or {}
    scales_out: dict[str, Any] = {}
    for name, data in scales_obj.items():
        if isinstance(data, dict):
            item: dict[str, Any] = {
                "value": data.get("value"),
                "label": str(data.get("label") or ""),
            }
            for level_value in (data.get("levels_count"), scale_meta.get(name),
                                scale_meta.get(str(data.get("label") or ""))):
                if level_value is not None:
                    item["levels_count"] = int(level_value)
                    break
            scales_out[name] = item
        else:
            scales_out[name] = {"value": data, "label": str(data) if data is not None else ""}

    # Convenience top-level "scala_salute" mirror of the scale when present.
    scala_salute = scales_out.get("Scala salute") or scales_out.get("scala_salute")
    if scala_salute is None and "Scala salute" in scales_out:
        scala_salute = scales_out["Scala salute"]

    full_date = str(row.get("full_date") or "")
    date_str, weekday_str = _derive_date_weekday(full_date)
    # If a future phase persists date/weekday, honour the stored values.
    if row.get("date"):
        date_str = str(row["date"])
    if row.get("weekday"):
        weekday_str = str(row["weekday"])

    canonical: dict[str, Any] = {
        "full_date": full_date,
        "date": date_str,
        "weekday": weekday_str,
        "time": str(row.get("time") or ""),
        "mood": row.get("mood"),
        "mood_label": str(row.get("mood_label") or ""),
        "activities": activities,
        "scales": scales_out,
        "scala_salute": scala_salute,
        "note_title": str(row.get("note_title") or ""),
        "note": str(row.get("note") or ""),
        "source": str(row.get("source") or ""),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }

    if columns:
        return {col: canonical[col] for col in columns if col in canonical}
    return canonical
