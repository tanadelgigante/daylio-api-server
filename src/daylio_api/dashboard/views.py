"""Dashboard SPA + Influx-only analytics API (Fase 6, R4/D4).

=============================== MAINTENANCE NOTES ===============================
Module   : src/daylio_api/dashboard/views.py
Scope    : Fase 6 (WORKPLAN §7 Tasks 6.1/6.3, §5.2/§5.6/§5.7) — the integrated
           dashboard: a light SPA (static/index.html + static/app.js + the
           vendored Chart.js) fed by five JSON endpoints that read **exclusively
           from InfluxDB**.

Endpoints (all ``@login_required``; D6 user filter, D4/R4 Influx-only):

  GET /dashboard                            -> the SPA shell (public: without a
                                              session it only shows the login form)
  GET /api/analytics/mood-over-time         -> mean mood series (day/week buckets)
  GET /api/analytics/mood-distribution      -> record count per mood level (1..5)
  GET /api/analytics/heatmap                -> weekday x hour matrix (mean mood)
  GET /api/analytics/scales-trend           -> per-scale value/label trend
  GET /api/analytics/activities-top         -> most frequent activities

Influx-only (R4/D4) : every view goes through ``InfluxStore.read_daily_points``
  (§5.4 point contract) and NOTHING else. This module deliberately does not
  import ``db.sqlite_store`` and does not touch ``day_entries``: the identity
  (session/API key -> username) is the only SQLite read, and it happens inside
  ``api/auth.py`` before the view body runs. ``tests/integration/
  test_dashboard_api.py`` pins this with a sqlite3 trace-callback spy AND a
  source-level import check.

Read path : ``InfluxStore.read_daily_points`` (db/influx_store.py) pivots the
  Flux rows into one record per (user, date) — the real client returns one
  FluxRecord per FIELD, the test fake returns whole points; both are handled
  there. All aggregations below are pure functions over that list, so they are
  unit-testable without Influx and identical against a real server.

Granularity: ``mood-over-time`` buckets by day, or by ISO week when the range is
  wider than ``WEEKLY_THRESHOLD_DAYS`` (92 days) — the sample dataset spans ~2
  years and a 628-point daily line is unreadable. ``?granularity=day|week``
  forces it; anything else is a 400.

Error contract: 400 invalid params, 401 no session, 403 cross-user (non-admin),
  404 unknown chart, 503 InfluxDB not configured/unreachable/query failure. The
  400/503 handlers below keep the uniform ``{"error", "type"}`` shape used by
  ``api/routes.py``; 401/403/404/500 are served by the app-level handlers (the
  500 one is owned by ``api/routes.py`` — a chart failure therefore answers the
  same JSON envelope as the rest of the API instead of an HTML error page, and
  this blueprint does not register a second, competing one). A 503 is the ONLY
  answer for an unavailable Influx: the dashboard never degrades to SQLite
  (R4/D4), the SPA renders the error banner instead.

Filter edge cases: ``?user=`` with an empty value is treated as ABSENT by the
  shared ``_effective_user`` (fallback = the viewer), and ``?scale=`` is
  validated — an unknown name is a 400, never a 200 with an all-null series
  (:func:`_known_scale_key`).

Reuse     : ``_effective_user`` / ``_valid_date`` are imported from
  ``api.routes`` (Fase 4/5) on purpose — the D6 isolation rules and the date
  validation must have a single implementation.
================================================================================
"""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import date as date_cls
from typing import Any

from flask import Blueprint, abort, current_app, jsonify, request

from daylio_api.api.auth import login_required
from daylio_api.api.routes import _effective_user, _valid_date
from daylio_api.config import AppConfig, scale_label
from daylio_api.db.influx_store import (
    InfluxStore,
    build_scale_key_map,
    influx_available,
    scale_key_for_display,
)
from daylio_api.logger import get_logger

log = get_logger("dashboard")

#: Single blueprint: the SPA + its static assets + the /api/analytics endpoints.
#: No ``url_prefix`` so the analytics routes can live under /api/... while the
#: assets stay under /dashboard/static/ (the URLs are declared explicitly).
bp = Blueprint(
    "dashboard",
    __name__,
    static_folder="static",
    static_url_path="/dashboard/static",
)

#: Monday-first weekday axis of the heatmap (§5.7).
WEEKDAY_LABELS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

#: Ranges wider than this are aggregated per ISO week by ``mood-over-time``.
WEEKLY_THRESHOLD_DAYS = 92

#: Span assumed when the caller sends no range (Influx default window is ``-1y``).
DEFAULT_RANGE_DAYS = 365

#: Mood levels exposed by the distribution/heatmap endpoints (D1: 1=best..5=worst).
MOOD_LEVELS = (1, 2, 3, 4, 5)

#: Bounds of the ``activities-top`` ``?limit=`` parameter.
ACTIVITIES_LIMIT_DEFAULT = 10
ACTIVITIES_LIMIT_MAX = 50

#: 503 message shared by every "InfluxDB is not usable" path (R4/D4).
_INFLUX_UNAVAILABLE = (
    "InfluxDB is not available: the dashboard reads analytics from InfluxDB only (R4/D4)"
)

#: Matches the per-scale numeric field of a §5.4 point (``scale_<key>_value``).
_SCALE_FIELD_RE = re.compile(r"^scale_(?P<key>.+)_value$")


# --------------------------------------------------------------------------- #
# Request helpers (identity, range, availability)
# --------------------------------------------------------------------------- #
def _config() -> AppConfig | None:
    """Return the validated AppConfig registered by the app factory."""
    cfg = current_app.config.get("DAYLIO")
    return cfg if isinstance(cfg, AppConfig) else None


def _influx() -> InfluxStore | None:
    """Return the InfluxStore bootstrapped by the app factory (may be None)."""
    return current_app.extensions.get("daylio_influx")


def _require_influx() -> InfluxStore:
    """Return a reachable InfluxStore or abort 503 (never a SQLite fallback).

    ``influx_available`` is False when the store is missing/unconfigured (no
    network call) and when the ping fails (logged at ERROR), so an unreachable
    server produces the documented 503 instead of an empty chart.
    """
    store = _influx()
    if store is None or not influx_available(store):
        log.warning("analytics refused: InfluxDB unavailable (R4/D4)")
        abort(503, description=_INFLUX_UNAVAILABLE)
    return store


def _parse_range() -> tuple[str | None, str | None]:
    """Validate the inclusive ``start_date``/``end_date`` query params.

    Returns ``(start, end)`` with ``None`` for a missing bound. An invalid date
    format, or a range that ends before it starts, is a uniform 400.
    """
    start = request.args.get("start_date")
    end = request.args.get("end_date")
    if not _valid_date(start) or not _valid_date(end):
        abort(400, description="invalid date format (expected YYYY-MM-DD)")
    if start and end and start > end:
        abort(400, description="start_date must be <= end_date")
    return start or None, end or None


def _read_points(
    store: InfluxStore, user: str, start: str | None, end: str | None
) -> list[dict[str, Any]]:
    """Read the user's pivoted daily points; a query failure is a 503.

    The exception detail is logged (ERROR) but not leaked to the client, and it
    is NEVER replaced by a SQLite read (R4/D4).
    """
    try:
        return store.read_daily_points(user, start, end)
    except Exception as exc:  # noqa: BLE001 — never leak Influx internals
        log.error("Influx analytics read failed for user=%s: %s", user, exc)
        abort(503, description="InfluxDB query failed: analytics temporarily unavailable")


def _scope(user: str, start: str | None, end: str | None) -> dict[str, Any]:
    """Echo the resolved filter scope in every analytics response."""
    return {"user": user, "start_date": start, "end_date": end}


# --------------------------------------------------------------------------- #
# Point accessors (pure functions over the §5.4 point contract)
# --------------------------------------------------------------------------- #
def _point_mood(point: dict[str, Any]) -> float | None:
    """Return the mood of a point (1..5) or None when the day has no mood.

    The §5.4 point stores the value twice (``mood`` + ``mood_num``); ``0`` means
    "unresolved label" and is treated as MISSING so no aggregate ever shows a
    fake "perfect mood" zero.
    """
    fields = point.get("fields") or {}
    for key in ("mood", "mood_num"):
        raw = fields.get(key)
        if raw is None:
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return None


def _point_activities(point: dict[str, Any]) -> list[str]:
    """Return the activity list of a point (JSON string field -> list)."""
    return _json_list((point.get("fields") or {}).get("activities"))


def _point_scales(point: dict[str, Any]) -> Any:
    """Return the raw ``scales`` payload of a point (dict or list, parsed)."""
    return _json_any((point.get("fields") or {}).get("scales"))


def _json_any(raw: Any) -> Any:
    """Parse a JSON string field, returning the value as-is when already typed."""
    if isinstance(raw, (dict, list)):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _json_list(raw: Any) -> list[str]:
    """Parse a JSON array field into a list of non-empty strings."""
    parsed = _json_any(raw)
    if not isinstance(parsed, list):
        return []
    return [str(item).strip() for item in parsed if str(item).strip()]


def _point_scale_value(point: dict[str, Any], key: str) -> int | None:
    """Return ``scale_<key>_value`` for a point (None when absent/zero)."""
    raw = (point.get("fields") or {}).get(f"scale_{key}_value")
    try:
        value = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return value or None


def _point_scale_label(
    point: dict[str, Any], key: str, key_map: dict[str, str]
) -> str | None:
    """Return the human label of ``key`` for a point (None when absent).

    ``scala_salute`` is stored as a point TAG (§5.4), every other scale keeps
    its label inside the ``scales`` JSON field, in either the canonical list
    form or the §5.3 object form.
    """
    if key == "scala_salute":
        label = str((point.get("tags") or {}).get("scala_salute_label") or "").strip()
        return label or None
    scales = _point_scales(point)
    if isinstance(scales, dict):
        for display, entry in scales.items():
            resolved = key_map.get(str(display).strip().casefold())
            if resolved == key and isinstance(entry, dict):
                return str(entry.get("label") or "").strip() or None
    elif isinstance(scales, list):
        for entry in scales:
            if isinstance(entry, dict) and str(entry.get("scale_key") or "") == key:
                return str(entry.get("label") or "").strip() or None
    return None


def _scale_key_map(cfg: AppConfig | None) -> dict[str, str]:
    """Return the display(casefolded) -> config key map (influx_store helper)."""
    return build_scale_key_map(cfg)


# --------------------------------------------------------------------------- #
# Bucketing helpers
# --------------------------------------------------------------------------- #
def _bucket_key(day: str, granularity: str) -> str:
    """Bucket label for a ``YYYY-MM-DD`` day: the day itself or its ISO week."""
    if granularity == "week":
        parsed = date_cls.fromisoformat(day)
        iso = parsed.isocalendar()
        return f"{iso[0]}-W{iso[1]:02d}"
    return day


def _span_days(start: str | None, end: str | None) -> int:
    """Length in days of the requested window (bounded by today when open)."""
    if start and end:
        return (date_cls.fromisoformat(end) - date_cls.fromisoformat(start)).days + 1
    today = date_cls.today()
    if start:
        return max((today - date_cls.fromisoformat(start)).days + 1, 1)
    if end:
        return max((today - date_cls.fromisoformat(end)).days + 1, 1)
    return DEFAULT_RANGE_DAYS


def _resolve_granularity(start: str | None, end: str | None) -> str:
    """Return ``"day"`` or ``"week"`` (``?granularity=`` wins, else auto).

    Auto: a window longer than :data:`WEEKLY_THRESHOLD_DAYS` is aggregated per
    ISO week so the chart stays readable on multi-year datasets.
    """
    requested = (request.args.get("granularity") or "").strip().lower()
    if requested in {"day", "week"}:
        return requested
    if requested:
        abort(400, description="granularity must be 'day' or 'week'")
    return "week" if _span_days(start, end) > WEEKLY_THRESHOLD_DAYS else "day"


def _mean(values: list[float]) -> float | None:
    """Arithmetic mean rounded to 2 decimals, or None for an empty list."""
    if not values:
        return None
    return round(sum(values) / len(values), 2)


def _level_counts(values: list[float]) -> dict[str, int]:
    """Count the mood records of one bucket per level, as string keys (JSON)."""
    counter = Counter(int(value) for value in values)
    return {str(level): int(counter.get(level, 0)) for level in MOOD_LEVELS}


# --------------------------------------------------------------------------- #
# SPA
# --------------------------------------------------------------------------- #
@bp.get("/dashboard")
@bp.get("/dashboard/")
def dashboard_index() -> Any:
    """Serve the SPA shell (static/index.html).

    Public on purpose: the page is a login form until ``/api/auth/me`` answers
    200, and every analytics endpoint is protected on its own. Both the
    ``/dashboard`` and the ``/dashboard/`` spellings answer 200 (no redirect).
    """
    return bp.send_static_file("index.html")


# --------------------------------------------------------------------------- #
# Analytics endpoints (Influx-only)
# --------------------------------------------------------------------------- #
@bp.get("/api/analytics/mood-over-time")
@login_required
def analytics_mood_over_time() -> Any:
    """Mean mood series for the range, bucketed per day or ISO week.

    Query params: ``user`` (D6), ``start_date``/``end_date`` (inclusive),
    ``granularity=day|week`` (auto by range width).

    Response::

        {"user", "start_date", "end_date", "granularity", "days", "buckets",
         "dates": ["2026-01-05", ...],
         "values": [1.0, null, ...],       # mean mood per bucket (null = no record)
         "levels": [{"1": 1, ..., "5": 0}, ...],   # record count per mood level
         "average": 2.4 | null}

    ``days`` counts the daily records READ from Influx (a day whose mood could
    not be resolved is still counted), while ``buckets`` counts the plotted
    points in ``dates``: with ``granularity=week`` the two differ by design (6
    days -> 1 bucket), in ``day`` mode ``buckets`` is ``days`` minus the days
    with no resolvable mood.
    """
    user = _effective_user()
    start, end = _parse_range()
    granularity = _resolve_granularity(start, end)
    points = _read_points(_require_influx(), user, start, end)

    buckets: dict[str, list[float]] = {}
    for point in points:
        mood = _point_mood(point)
        if mood is None:
            continue
        buckets.setdefault(_bucket_key(point["date"], granularity), []).append(mood)

    dates = sorted(buckets)
    all_moods = [value for values in buckets.values() for value in values]
    payload = {
        **_scope(user, start, end),
        "granularity": granularity,
        "days": len(points),
        "buckets": len(dates),
        "dates": dates,
        "values": [_mean(buckets[date]) for date in dates],
        "levels": [_level_counts(buckets[date]) for date in dates],
        "average": _mean(all_moods),
    }
    log.debug("mood-over-time user=%s buckets=%d granularity=%s", user, len(dates), granularity)
    return jsonify(payload), 200


@bp.get("/api/analytics/mood-distribution")
@login_required
def analytics_mood_distribution() -> Any:
    """Record count per mood level (1..5) for the range.

    Query params: ``user`` (D6), ``start_date``/``end_date`` (inclusive).

    Response::

        {"user", "start_date", "end_date", "days", "total", "average",
         "levels": [1, 2, 3, 4, 5],
         "labels": ["alla grande", ..., "malissimo"],
         "counts": [12, 0, 3, 1, 0]}
    """
    user = _effective_user()
    start, end = _parse_range()
    points = _read_points(_require_influx(), user, start, end)

    rated = [(point, mood) for point in points if (mood := _point_mood(point)) is not None]
    moods = [mood for _, mood in rated]
    counter = Counter(int(mood) for mood in moods)
    labels = _mood_labels(rated)
    payload = {
        **_scope(user, start, end),
        "days": len(points),
        "total": len(moods),
        "average": _mean(moods),
        "levels": list(MOOD_LEVELS),
        "labels": labels,
        "counts": [int(counter.get(level, 0)) for level in MOOD_LEVELS],
    }
    return jsonify(payload), 200


def _mood_labels(rated: list[tuple[dict[str, Any], float]]) -> list[str]:
    """Mood labels for levels 1..5: observed Influx tags, then config defaults.

    The label is a point TAG (§5.4), so it comes from Influx; when a level has
    no observation the configured label (YAML ``mood.values``, D1) is used so the
    chart legend is never empty.
    """
    observed: dict[int, str] = {}
    for point, mood in rated:
        label = str((point.get("tags") or {}).get("mood_label") or "").strip()
        if not label:
            continue
        observed.setdefault(int(mood), label)
    cfg = _config()
    configured: dict[int, str] = {}
    mood_cfg = getattr(cfg, "mood", None)
    values = mood_cfg.get("values") if isinstance(mood_cfg, dict) else None
    if isinstance(values, dict):
        for label, index in values.items():
            try:
                configured[int(index)] = str(label)
            except (TypeError, ValueError):
                continue
    return [observed.get(level) or configured.get(level) or "" for level in MOOD_LEVELS]


@bp.get("/api/analytics/heatmap")
@login_required
def analytics_heatmap() -> Any:
    """Weekday x hour matrix of the mean mood for the range.

    Query params: ``user`` (D6), ``start_date``/``end_date`` (inclusive),
    ``metric=mean_mood|count`` (default ``mean_mood``).

    Response::

        {"user", "start_date", "end_date", "metric", "days", "total",
         "weekdays": ["Mon", ..., "Sun"],
         "buckets": ["00", "01", ..., "23"],
         "data": [[1.0, null, ...], ...],   # 7 rows (Mon..Sun) x 24 columns
         "counts": [[1, 0, ...], ...]}      # records behind every cell

    MAINTENANCE: the hour column comes from the point timestamp, which §5.4
    fixes at the START of the day (``T00:00:00Z``); with the current daily
    measurement every record therefore lands in the ``00`` bucket. The matrix is
    already intraday-ready should the measurement ever carry a real time.
    """
    user = _effective_user()
    start, end = _parse_range()
    metric = (request.args.get("metric") or "mean_mood").strip().lower()
    if metric not in {"mean_mood", "count"}:
        abort(400, description="metric must be 'mean_mood' or 'count'")
    points = _read_points(_require_influx(), user, start, end)

    cell_values: list[list[list[float]]] = [[[] for _ in range(24)] for _ in range(7)]
    for point in points:
        mood = _point_mood(point)
        if mood is None:
            continue
        day = date_cls.fromisoformat(point["date"])
        cell_values[day.weekday()][_point_hour(point)].append(mood)

    data: list[list[float | None]] = []
    counts: list[list[int]] = []
    for row in cell_values:
        row_data: list[float | None] = []
        row_counts: list[int] = []
        for cell in row:
            average = _mean(cell)
            if metric == "count":
                row_data.append(float(len(cell)))
            else:
                row_data.append(average)
            row_counts.append(len(cell))
        data.append(row_data)
        counts.append(row_counts)

    payload = {
        **_scope(user, start, end),
        "metric": metric,
        "days": len(points),
        "total": sum(sum(row) for row in counts),
        "weekdays": list(WEEKDAY_LABELS),
        "buckets": [f"{hour:02d}" for hour in range(24)],
        "data": data,
        "counts": counts,
    }
    return jsonify(payload), 200


def _point_hour(point: dict[str, Any]) -> int:
    """Hour of the point timestamp (0..23, default 0 when absent/unparsable)."""
    time_value = str(point.get("time") or "")
    try:
        return max(0, min(23, int(time_value[11:13])))
    except ValueError:
        return 0


@bp.get("/api/analytics/scales-trend")
@login_required
def analytics_scales_trend() -> Any:
    """Per-scale value/label trend for the range (Stress, Scala salute, custom).

    Query params: ``user`` (D6), ``start_date``/``end_date`` (inclusive),
    ``scale=<name or config key>`` (optional filter, case-insensitive).

    Response with ``?scale=`` (single scale)::

        {"user", "start_date", "end_date", "scale_name": "stress",
         "scale_label": "Stress", "days": 6, "days_with_value": 2,
         "dates": ["2026-01-05", ...],       # every day that has a record
         "values": [3, null, ...],           # scale_<key>_value (null = absent)
         "labels": ["Moderato", null, ...]}

    Response without it (all scales found in the data)::

        {"user", "start_date", "end_date", "scales": [<object above>, ...]}

    Days without the scale are kept with a ``null`` value (flagged by
    ``days_with_value``) so the chart shows the gaps instead of a fake 0.

    A ``?scale=`` that is neither a configured scale nor one carried by the
    caller's own points is a uniform ``400 bad_request`` (naming the available
    scales) — see :func:`_known_scale_key`. A configured scale with no rating
    in the range is still a 200 with an all-``null`` series: that is real data,
    not a bad request.
    """
    user = _effective_user()
    start, end = _parse_range()
    points = _read_points(_require_influx(), user, start, end)
    key_map = _scale_key_map(_config())
    requested = (request.args.get("scale") or "").strip()

    if requested:
        keys = [_known_scale_key(requested, key_map, points)]
    else:
        keys = sorted({key for point in points for key in _point_scale_keys(point, key_map)})

    series = [
        _scale_series(points, key, key_map)
        for key in keys
        if key is not None
    ]
    if requested:
        return jsonify({**_scope(user, start, end), **series[0]}), 200
    return jsonify({**_scope(user, start, end), "scales": series}), 200


def _known_scale_key(
    name: str, key_map: dict[str, str], points: list[dict[str, Any]]
) -> str:
    """Resolve and VALIDATE a ``?scale=`` value, or abort with a 400.

    Accepted names, checked in this order (a legitimate per-user custom scale
    must keep working, an invented name must not silently become an empty
    chart):

      1. a configured scale — by display label or by config key (case-insensitive);
      2. a scale actually present in the caller's own Influx points (the data may
         carry a scale that is no longer in the config, §5.4 tolerance), either
         by its key or by the display label stored in the point payload. Custom
         labels are mapped with :func:`scale_key_for_display`, i.e. exactly the
         key the WRITE path used, so read and write can never disagree;
      3. otherwise a uniform ``400 bad_request`` listing what IS available.

    An unrecognised value used to be echoed back as a series of ``null``, i.e.
    a 200 that looked like "this user never rated the scale".
    """
    resolved = _resolve_scale_key(name, key_map)
    if resolved is not None:
        return resolved
    available = _available_scale_keys(key_map, points)
    if name.casefold() in available:
        # Custom scale found in the data: use the key the writer used.
        return available[name.casefold()]
    abort(
        400,
        description=(
            f"unknown scale '{name}' "
            f"(available: {', '.join(sorted(set(available.values()))) or 'none'})"
        ),
    )


def _resolve_scale_key(name: str, key_map: dict[str, str]) -> str | None:
    """Resolve a ``?scale=`` value (display name or config key) to a config key.

    Returns ``None`` when the name matches no configured scale; the caller then
    decides whether the caller's own data justifies it (see
    :func:`_known_scale_key`).
    """
    lowered = name.strip().casefold()
    if lowered in key_map:
        return key_map[lowered]
    for key in key_map.values():
        if key.casefold() == lowered:
            return key
    return None


def _available_scale_keys(key_map: dict[str, str], points: list[dict[str, Any]]) -> dict[str, str]:
    """Casefolded name -> key for every scale a ``?scale=`` may legitimately use.

    Union of the configured display labels + config keys and everything the
    caller's own points carry (numeric ``scale_<key>_value`` fields, the
    ``scales`` payload keys/``scale_name`` entries and the D2 ``scala_salute``
    tag), so a per-user custom scale is still reachable.
    """
    available: dict[str, str] = {}
    for display, key in key_map.items():
        available.setdefault(display.casefold(), key)
        available.setdefault(key.casefold(), key)
    for point in points:
        for key in _point_scale_keys(point, key_map):
            available.setdefault(key.casefold(), key)
        for name, key in _point_scale_aliases(point, key_map).items():
            available.setdefault(name.casefold(), key)
    return available


def _point_scale_aliases(point: dict[str, Any], key_map: dict[str, str]) -> dict[str, str]:
    """Map every display name a point carries to the key used to store it."""
    aliases: dict[str, str] = {}
    scales = _point_scales(point)
    if isinstance(scales, dict):
        for display in scales:
            key = scale_key_for_display(display, key_map)
            if key:
                aliases[str(display).strip()] = key
    elif isinstance(scales, list):
        for entry in scales:
            if not isinstance(entry, dict):
                continue
            key = str(entry.get("scale_key") or "").strip()
            if not key:
                key = scale_key_for_display(entry.get("scale_name") or "", key_map) or ""
            if not key:
                continue
            for field in ("scale_name", "label"):
                name = str(entry.get(field) or "").strip()
                if name:
                    aliases[name] = key
    return aliases



def _point_scale_display_names(point: dict[str, Any]) -> set[str]:
    """Casefolded display labels carried by a point's ``scales`` payload."""
    names: set[str] = set()
    scales = _point_scales(point)
    if isinstance(scales, dict):
        for display in scales:
            text = str(display).strip()
            if text:
                names.add(text.casefold())
    elif isinstance(scales, list):
        for entry in scales:
            if isinstance(entry, dict):
                for field in ("scale_name", "label"):
                    text = str(entry.get(field) or "").strip()
                    if text:
                        names.add(text.casefold())
    return names


def _point_scale_keys(point: dict[str, Any], key_map: dict[str, str]) -> set[str]:
    """Config keys of every scale present on a point (numeric fields + payload)."""
    keys: set[str] = set()
    for name in (point.get("fields") or {}):
        match = _SCALE_FIELD_RE.match(str(name))
        if match:
            keys.add(match.group("key"))
    scales = _point_scales(point)
    if isinstance(scales, dict):
        for display in scales:
            resolved = key_map.get(str(display).strip().casefold())
            if resolved:
                keys.add(resolved)
    elif isinstance(scales, list):
        for entry in scales:
            if isinstance(entry, dict) and str(entry.get("scale_key") or ""):
                keys.add(str(entry["scale_key"]))
    return keys


def _scale_series(
    points: list[dict[str, Any]], key: str, key_map: dict[str, str]
) -> dict[str, Any]:
    """Build the ``{scale_name, dates, values, labels}`` series for one scale."""
    dates: list[str] = []
    values: list[int | None] = []
    labels: list[str | None] = []
    for point in sorted(points, key=lambda item: item["date"]):
        dates.append(point["date"])
        values.append(_point_scale_value(point, key))
        labels.append(_point_scale_label(point, key, key_map))
    reported = sum(1 for value in values if value is not None)
    return {
        "scale_name": key,
        "scale_label": scale_label(key),
        "days": len(dates),
        "days_with_value": reported,
        "dates": dates,
        "values": values,
        "labels": labels,
    }


@bp.get("/api/analytics/activities-top")
@login_required
def analytics_activities_top() -> Any:
    """Most frequent activities in the range (top-N, tie -> alphabetical).

    Query params: ``user`` (D6), ``start_date``/``end_date`` (inclusive),
    ``limit`` (default 10, 1..50).

    Response::

        {"user", "start_date", "end_date", "limit", "days", "distinct",
         "excluded_scale": "scala_salute",
         "activities": [{"activity": "run", "count": 3}, ...]}

    The D2 "Scala salute" levels are EXCLUDED from the ranking: they are an
    ordered health scale carried inside the activities column, not activities.
    The normalizer already strips them, but a record normalized with the scale
    unconfigured keeps them, so the endpoint filters them again.
    """
    user = _effective_user()
    start, end = _parse_range()
    try:
        limit = int(request.args.get("limit") or ACTIVITIES_LIMIT_DEFAULT)
    except ValueError:
        abort(400, description="limit must be an integer")
    if limit < 1 or limit > ACTIVITIES_LIMIT_MAX:
        abort(400, description=f"limit must be between 1 and {ACTIVITIES_LIMIT_MAX}")

    points = _read_points(_require_influx(), user, start, end)
    excluded = _scala_salute_labels(points)
    counter: Counter[str] = Counter()
    for point in points:
        for activity in _point_activities(point):
            if activity.casefold() in excluded:
                continue
            counter[activity] += 1

    top = sorted(counter.items(), key=lambda item: (-item[1], item[0]))[:limit]
    payload = {
        **_scope(user, start, end),
        "limit": limit,
        "days": len(points),
        "distinct": len(counter),
        "excluded_scale": "scala_salute",
        "activities": [{"activity": name, "count": count} for name, count in top],
    }
    return jsonify(payload), 200


def _scala_salute_labels(points: list[dict[str, Any]]) -> set[str]:
    """Casefolded D2 scale-health labels to drop from the activity ranking."""
    labels: set[str] = set()
    cfg = _config()
    scales = getattr(cfg, "scales", None)
    definition = scales.get("scala_salute") if isinstance(scales, dict) else None
    values = definition.get("values") if isinstance(definition, dict) else None
    if isinstance(values, (list, tuple)):
        labels.update(str(value).strip().casefold() for value in values if str(value).strip())
    for point in points:
        label = str((point.get("tags") or {}).get("scala_salute_label") or "").strip()
        if label:
            labels.add(label.casefold())
    return labels


# --------------------------------------------------------------------------- #
# Uniform error handling (the 401/403/404 shapes come from app.py)
# --------------------------------------------------------------------------- #
@bp.errorhandler(400)
def _error_bad_request(exc: Any) -> Any:
    """400 -> ``{"error", "type": "bad_request"}`` (same shape as api/routes)."""
    return (
        jsonify(
            {
                "error": str(getattr(exc, "description", None) or "bad request"),
                "type": "bad_request",
            }
        ),
        400,
    )


@bp.errorhandler(404)
def _error_not_found(exc: Any) -> Any:
    """404 -> uniform JSON (unknown chart, unknown dashboard asset)."""
    return (
        jsonify(
            {
                "error": str(getattr(exc, "description", None) or "not found"),
                "type": "not_found",
            }
        ),
        404,
    )


@bp.errorhandler(503)
def _error_unavailable(exc: Any) -> Any:
    """503 -> uniform JSON (InfluxDB missing/unreachable; never a SQLite read)."""
    return (
        jsonify(
            {
                "error": str(getattr(exc, "description", None) or _INFLUX_UNAVAILABLE),
                "type": "service_unavailable",
            }
        ),
        503,
    )


@bp.get("/api/analytics/")
@bp.get("/api/analytics")
@login_required
def analytics_index() -> Any:
    """List the available charts (self-describing nav for the SPA)."""
    charts = [
        {"chart": "mood-over-time", "title": "Mood over time"},
        {"chart": "mood-distribution", "title": "Mood distribution"},
        {"chart": "heatmap", "title": "Mood heatmap (weekday x hour)"},
        {"chart": "scales-trend", "title": "Scales trend"},
        {"chart": "activities-top", "title": "Top activities"},
    ]
    return jsonify({"charts": charts, "source": "influxdb"}), 200
