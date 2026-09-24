"""API blueprint: configuration reads (Fase 1) + parametric records (Fase 4).

=============================== MAINTENANCE NOTES ===============================
Scope    : Fase 0 predicate (Task 0.2) + Fase 1 (Task 1.3) expose the
           configuration reads — ``GET /api/version``, ``GET /api/columns``,
           ``GET /api/scales``. Fase 4 (R2/D10) adds the parametric records API:
           ``GET /api/records`` (filters user/date/columns/page/limit),
           ``GET /api/records/<full_date>`` and ``GET /api/import/jobs``
           (audit). The two legacy endpoints ``GET /users`` and ``GET /moods``
           keep the old single-file ``server.py`` wire contract, now served
           from ``day_entries`` (SQLite is the source of truth, D4).
           Upload/import (POST /api/import), auth and analytics arrive in
           Phases 5-6 (WORKPLAN §7); routes are public in this phase.

Source   : Config reads are served from the SQLite configuration tables
           (columns_config / scales_config / mood_map) bootstrapped by
           ``config.seed_defaults`` + ``config.sync_schema`` at app startup.
           Records/legacy endpoints read ``day_entries``/``import_jobs``.
           Optional ``?user=<name>`` merges the YAML per-user scale overrides
           and (for columns) the ``columns.optional`` allow-list defined in
           ``users.example.yml``.

Errors   : uniform ``{"error": str, "type": <bad_request|not_found|
           internal_error>}`` handled in this blueprint (Task 4.3). Internal
           errors are logged at ERROR, no traceback is leaked to the client.

Helpers  : ``_store()`` resolves the SqliteStore from ``app.extensions``;
           ``_merge_user_scales`` / ``_scale_from_override`` implement the
           per-user merge without writing to SQLite (multi-user rows land in
           Phase 5); ``_resolve_user()``/``_valid_date()`` centralize the
           Fase 4 parameter validation.
==================================================================================
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from flask import Blueprint, abort, current_app, jsonify, request

from daylio_api import __version__
from daylio_api.api.serializers import serialize_record
from daylio_api.audit import list_jobs
from daylio_api.config import mood_override_values, parse_tag_group, scale_label
from daylio_api.db.sqlite_store import SqliteStore

bp = Blueprint("api", __name__, url_prefix="/api")

#: Root-level blueprint exposing the legacy single-file ``server.py`` wire
#: contract (``/users`` and ``/moods``, no ``/api`` prefix). Register it in
#: the app factory next to ``api_bp``.
legacy_bp = Blueprint("legacy", __name__)

#: Extra canonical keys produced by ``serialize_record`` on top of the
#: ``columns_config`` rows (mood_label/scala_salute/source/created_at/updated_at
#: are exposed by the serializer even though they are not configurable columns).
_CANONICAL_EXTRA_COLUMNS = (
    "mood_label",
    "scala_salute",
    "source",
    "created_at",
    "updated_at",
)


def _store() -> SqliteStore:
    """Return the bootstrapped SqliteStore (registered by the app factory)."""
    store = current_app.extensions.get("daylio_store")
    if store is None:
        abort(503, description="SQLite store not initialised")
    return store


@bp.get("/version")
def api_version() -> Any:
    """Return the running package version (used by tests/health checks)."""
    return jsonify({"name": "daylio-api-server", "version": __version__})


@bp.get("/columns")
def api_columns() -> Any:
    """Return the configured record columns coming from ``columns_config``.

    Example response (Fase 4 consumers can rely on this shape):
        GET /api/columns -> 200
        {
          "columns": [
            {"name": "full_date", "label": "Full date", "type": "date",
             "required": true, "derived": false, "source": "csv+daylio",
             "sort_order": 10, "enabled": true, "options": {}},
            {"name": "date", "label": "Date", "type": "date",
             "required": false, "derived": true, "source": "derived",
             "sort_order": 20, "enabled": true, "options": {}},
            {"name": "activities", "label": "Activities", "type": "list",
             "required": false, "derived": false, "source": "csv+daylio",
             "sort_order": 60, "enabled": true,
             "options": {"separator": " | "}},
            ...
          ]
        }

    Query params:
        user: optional username; when the user has a ``columns.optional``
            override in the users config it acts as an allow-list of exposed
            column names (see config/users.example.yml, "carol" example).
    """
    store = _store()
    columns = store.get_columns()
    user = request.args.get("user")
    if user:
        user_overrides = _user_overrides(user)
        columns_override = user_overrides.get("columns")
        if isinstance(columns_override, dict) and isinstance(
            columns_override.get("optional"), list
        ):
            allowed = set(str(name) for name in columns_override["optional"])
            columns = [column for column in columns if column["name"] in allowed]
    return jsonify({"columns": columns})


@bp.get("/scales")
def api_scales() -> Any:
    """Return the configured scales and the D1 mood map.

    Example response (Fase 4 consumers can rely on this shape):
        GET /api/scales -> 200
        {
          "scales": [
            {"name": "stress", "label": "Stress", "type": "text",
             "source": "column", "tag_group": null, "levels": 5,
             "min": null, "max": null,
             "values": [{"label": "Calma", "rank": 1}, ...],
             "sort_order": 10, "enabled": true},
            {"name": "scala_salute", "label": "Scala salute", "type": "text",
             "source": "tag-group:5", "tag_group": 5, "levels": 8,
             "min": null, "max": null,
             "values": [{"label": "ottimo", "rank": 1}, ...],
             "sort_order": 20, "enabled": true}
          ],
          "mood_map": [
            {"level": 1, "label": "alla grande"},
            {"level": 2, "label": "bene"},
            ...
          ]
        }

    Query params:
        user: optional username; YAML per-user ``scales`` overrides replace
            or extend the global scales (R4/§6). A per-user ``mood`` override
            (label -> index, D1) replaces ``mood_map``.
    """
    store = _store()
    scales = store.get_scales()
    mood_map = store.get_mood_map()
    user = request.args.get("user")
    if user:
        scales, mood_map = _merge_user_scales(scales, mood_map, _user_overrides(user))
    return jsonify({"scales": scales, "mood_map": mood_map})


# --------------------------------------------------------------------------- #
# Per-user YAML override helpers (Fase 1 read-only; real multi-user rows in Phase 5)
# --------------------------------------------------------------------------- #
def _user_overrides(user: str) -> dict[str, Any]:
    """Return the YAML override mapping for ``user`` (empty dict when absent)."""
    cfg = current_app.config.get("DAYLIO")
    users = cfg.users if cfg is not None else {}
    overrides = users.get(user)
    return overrides if isinstance(overrides, dict) else {}


def _scale_from_override(name: str, definition: Any, sort_order: int) -> dict[str, Any] | None:
    """Translate a YAML scale override into the canonical JSON shape."""
    if not isinstance(definition, dict):
        return None
    scale_type = str(definition.get("type", "text"))
    raw_values = definition.get("values")
    values: list[dict[str, Any]] = []
    if isinstance(raw_values, dict):
        values = [
            {"label": str(label), "rank": int(rank)}
            for label, rank in sorted(raw_values.items(), key=lambda kv: int(kv[1]))
        ]
    elif isinstance(raw_values, list):
        values = [
            {"label": str(value), "rank": rank}
            for rank, value in enumerate(raw_values, start=1)
        ]
    source = str(definition.get("source", "user-config"))
    is_number = scale_type == "number"
    return {
        "name": name,
        "label": scale_label(name),
        "type": scale_type,
        "source": source,
        "tag_group": parse_tag_group(source),
        "levels": 0 if is_number else len(values),
        "min": definition.get("min") if is_number else None,
        "max": definition.get("max") if is_number else None,
        "values": values,
        "sort_order": sort_order,
        "enabled": True,
    }


def _merge_user_scales(
    base_scales: list[dict[str, Any]],
    mood_map: list[dict[str, Any]],
    overrides: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Merge per-user YAML ``scales`` overrides into the global scales/mood.

    Existing scales keep their global sort_order; new user-only scales are
    appended after the highest sort_order. A per-user ``mood`` override
    (label -> index, D1) replaces ``mood_map`` entirely.
    """
    user_scales = overrides.get("scales")
    if not isinstance(user_scales, dict):
        return base_scales, mood_map

    merged = {scale["name"]: dict(scale) for scale in base_scales}
    next_order = max((scale["sort_order"] for scale in base_scales), default=0)
    user_mood: list[dict[str, Any]] | None = None

    for name, definition in user_scales.items():
        if name == "mood":
            # Normalize both documented forms (direct label->index mapping and
            # the generic {type, values} wrapper) through the shared helper
            # kept in sync with config validation (config.mood_override_values).
            values = mood_override_values(definition)
            if values:
                user_mood = [
                    {"level": int(rank), "label": str(label)}
                    for label, rank in sorted(values.items(), key=lambda kv: int(kv[1]))
                ]
            continue
        if name in merged:
            entry = _scale_from_override(name, definition, merged[name]["sort_order"])
        else:
            next_order += 10
            entry = _scale_from_override(name, definition, next_order)
        if entry is not None:
            merged[name] = entry

    scales = sorted(merged.values(), key=lambda item: (item["sort_order"], item["name"]))
    return scales, user_mood if user_mood is not None else mood_map

# --------------------------------------------------------------------------- #
# Fase 4 shared helpers (validation + column list resolution)
# --------------------------------------------------------------------------- #
def _valid_date(value: str | None) -> bool:
    """Return True when ``value`` is missing or a strict ``YYYY-MM-DD`` date."""
    if not value:
        return True
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except (TypeError, ValueError):
        return False
    return True


def _resolve_user() -> str:
    """Return the explicit ``?user=`` query param or a sensible default.

    Default precedence: the single user declared in the YAML per-user section
    (when exactly one exists), otherwise the Phase-3 placeholder ``"default"``.
    """
    user = request.args.get("user")
    if user:
        return user
    cfg = current_app.config.get("DAYLIO")
    users = cfg.users if cfg is not None else {}
    if len(users) == 1:
        return str(next(iter(users)))
    return "default"


def _parse_columns_list(value: str | None) -> list[str] | None:
    """Split the comma-separated ``columns`` query param (None when absent)."""
    if not value:
        return None
    cols = [column.strip() for column in value.split(",")]
    return [column for column in cols if column]


def _get_valid_columns_set() -> set[str]:
    """Return every selectable column: columns_config + canonical extras."""
    store = _store()
    names = {column["name"] for column in store.get_columns()}
    names.update(_CANONICAL_EXTRA_COLUMNS)
    return names


def _scale_levels_meta() -> dict[str, int]:
    """Map scale name AND display label -> levels_count (Task 4.2).

    ``scales_config`` rows carry both a stable name (``stress``) and a display
    label (``Stress``); the stored per-record scales object is keyed by the
    display label, so both forms are mapped for the serializer enrichment.
    """
    meta: dict[str, int] = {}
    for scale in _store().get_scales():
        levels = int(scale.get("levels") or 0)
        meta[str(scale.get("name") or "")] = levels
        meta[str(scale.get("label") or "")] = levels
    return meta


# --------------------------------------------------------------------------- #
# Legacy endpoints kept for backward compatibility with the old server.py:
# they now read from ``day_entries``/``users`` (SQLite = source of truth, D4).
# They live on ``legacy_bp`` (root prefix, NO ``/api``) exactly like the old
# single-file server.
# --------------------------------------------------------------------------- #
@legacy_bp.get("/users")
def legacy_get_users() -> Any:
    """Legacy ``GET /users``: list users with their record counts.

    Backward-compatible body (old server grouped ``mood_entries``; we group
    ``day_entries``): ``[{"user": <username>, "records": <n>}, ...]``.
    """
    store = _store()
    conn = store.connect()
    rows = conn.execute(
        "SELECT user, COUNT(*) AS records FROM day_entries GROUP BY user ORDER BY user"
    ).fetchall()
    return jsonify([{"user": row["user"], "records": row["records"]} for row in rows])


@legacy_bp.get("/moods")
def legacy_get_moods() -> Any:
    """Legacy ``GET /moods``: filtered mood list with the OLD dict keys.

    Keys are kept exactly as the old server: ``date, time, mood, activities,
    note_title, note``. If ``user`` is missing the old server would fail on a
    ``None`` query parameter — here we return a clear 400 instead.

    Query params: user (required), start_date, end_date (YYYY-MM-DD, inclusive).
    """
    user = request.args.get("user")
    if not user:
        return jsonify({"error": "user parameter is required", "type": "bad_request"}), 400
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    if not _valid_date(start_date) or not _valid_date(end_date):
        return (
            jsonify(
                {"error": "invalid date format (expected YYYY-MM-DD)", "type": "bad_request"}
            ),
            400,
        )

    store = _store()
    conn = store.connect()
    sql = (
        "SELECT full_date, time, mood, activities, note_title, note "
        "FROM day_entries WHERE user = ?"
    )
    params: list[Any] = [user]
    if start_date:
        sql += " AND full_date >= ?"
        params.append(start_date)
    if end_date:
        sql += " AND full_date <= ?"
        params.append(end_date)
    sql += " ORDER BY full_date, time"
    rows = conn.execute(sql, params).fetchall()

    moods: list[dict[str, Any]] = []
    for row in rows:
        activities = row["activities"]
        if isinstance(activities, str):
            import json

            try:
                activities = json.loads(activities)
            except ValueError:
                activities = []
        moods.append(
            {
                "date": row["full_date"],
                "time": row["time"] or "",
                "mood": row["mood"],
                "activities": activities,
                "note_title": row["note_title"] or "",
                "note": row["note"] or "",
            }
        )
    return jsonify(moods)


# --------------------------------------------------------------------------- #
# Fase 4: uniform error handling (Task 4.3)
# --------------------------------------------------------------------------- #
@bp.errorhandler(400)
def _error_bad_request(exc: Any) -> Any:
    """Translate HTTP 400 into the uniform ``{"error", "type"}`` shape."""
    return (
        jsonify(
            {
                "error": str(getattr(exc, "description", None) or exc or "bad request"),
                "type": "bad_request",
            }
        ),
        400,
    )


@bp.errorhandler(404)
def _error_not_found(exc: Any) -> Any:
    """Translate HTTP 404 into the uniform ``{"error", "type": "not_found"}``."""
    return (
        jsonify(
            {"error": str(getattr(exc, "description", None) or "not found"), "type": "not_found"}
        ),
        404,
    )


@bp.app_errorhandler(500)
def _error_internal(exc: Any) -> Any:
    """Log the internal error at ERROR and hide the traceback from the client."""
    current_app.logger.error("internal error: %s", exc)
    return jsonify({"error": "internal server error", "type": "internal_error"}), 500


# --------------------------------------------------------------------------- #
# Fase 4: parametric records API (R2/D10)
# --------------------------------------------------------------------------- #
@bp.get("/records")
def api_records() -> Any:
    """Return daily records with filters, column selection and pagination.

    Query params:
        user: record owner (default: single YAML user or ``"default"``).
        start_date / end_date: inclusive ``YYYY-MM-DD`` bounds.
        columns: comma-separated allow-list of column names (from
            ``columns_config`` plus the canonical extras e.g. ``mood_label``,
            ``scala_salute``, ``source``).
        page: 1-based page number (default 1, must be >= 1).
        limit: page size (default 100, 1..1000).

    Response: ``{"total": n, "page": p, "limit": l, "records": [...]}`` with
    each record serialized by ``serialize_record`` (canonical shape §5.6).
    Invalid parameters produce a uniform 400 body; SQLite is the source of
    truth (D4).
    """
    args = request.args
    user = _resolve_user()
    start_date = args.get("start_date")
    end_date = args.get("end_date")

    if not _valid_date(start_date) or not _valid_date(end_date):
        return (
            jsonify(
                {"error": "invalid date format (expected YYYY-MM-DD)", "type": "bad_request"}
            ),
            400,
        )
    if start_date and end_date and start_date > end_date:
        return (
            jsonify({"error": "start_date must be <= end_date", "type": "bad_request"}),
            400,
        )

    try:
        page = int(args.get("page") or 1)
    except ValueError:
        return jsonify({"error": "page must be an integer", "type": "bad_request"}), 400
    try:
        limit = int(args.get("limit") or 100)
    except ValueError:
        return jsonify({"error": "limit must be an integer", "type": "bad_request"}), 400
    if page <= 0:
        return jsonify({"error": "page must be > 0", "type": "bad_request"}), 400
    if limit < 1:
        return jsonify({"error": "limit must be >= 1", "type": "bad_request"}), 400
    if limit > 1000:
        return jsonify({"error": "limit must be <= 1000", "type": "bad_request"}), 400

    requested_columns = _parse_columns_list(args.get("columns"))
    if requested_columns is not None:
        valid_set = _get_valid_columns_set()
        unknown = [column for column in requested_columns if column not in valid_set]
        if unknown:
            return (
                jsonify(
                    {
                        "error": f"unknown columns: {', '.join(unknown)}",
                        "type": "bad_request",
                    }
                ),
                400,
            )

    store = _store()
    conn = store.connect()

    sql_count = "SELECT COUNT(*) AS n FROM day_entries WHERE user = ?"
    count_params: list[Any] = [user]
    if start_date:
        sql_count += " AND full_date >= ?"
        count_params.append(start_date)
    if end_date:
        sql_count += " AND full_date <= ?"
        count_params.append(end_date)
    total = int(conn.execute(sql_count, count_params).fetchone()["n"])

    offset = (page - 1) * limit
    sql = "SELECT * FROM day_entries WHERE user = ?"
    page_params: list[Any] = [user]
    if start_date:
        sql += " AND full_date >= ?"
        page_params.append(start_date)
    if end_date:
        sql += " AND full_date <= ?"
        page_params.append(end_date)
    sql += " ORDER BY full_date DESC, id DESC LIMIT ? OFFSET ?"
    page_params.extend([limit, offset])
    rows = conn.execute(sql, page_params).fetchall()

    scale_meta = _scale_levels_meta()
    records = [
        serialize_record(dict(row), columns=requested_columns, scale_meta=scale_meta)
        for row in rows
    ]
    return jsonify({"total": total, "page": page, "limit": limit, "records": records})


@bp.get("/records/<full_date>")
def api_record_by_date(full_date: str) -> Any:
    """Return the single canonical record for ``(user, full_date)``.

    Query param user defaults like ``/api/records``. Returns a uniform 404
    (``{"error": "not_found", "type": "not_found"}``) when the date has no
    record for the user.
    """
    user = _resolve_user()
    if not _valid_date(full_date):
        return (
            jsonify({"error": "invalid date format (expected YYYY-MM-DD)", "type": "bad_request"}),
            400,
        )
    store = _store()
    conn = store.connect()
    row = conn.execute(
        "SELECT * FROM day_entries WHERE user = ? AND full_date = ?",
        (user, full_date),
    ).fetchone()
    if row is None:
        abort(404, description="not_found")
    return jsonify(serialize_record(dict(row), scale_meta=_scale_levels_meta()))


@bp.get("/import/jobs")
def api_import_jobs() -> Any:
    """Return the ``import_jobs`` audit trail (most recent first).

    Query params:
        user: optional owner filter.
        limit: optional row cap (default 100, 1..1000).

    Response: ``{"jobs": [...]}`` where each job mirrors the §5.3 audit row
    (detail parsed to a JSON array, timestamps as ISO strings).
    """
    user = request.args.get("user") or None
    try:
        limit = int(request.args.get("limit") or 100)
    except ValueError:
        return jsonify({"error": "limit must be an integer", "type": "bad_request"}), 400
    if limit < 1 or limit > 1000:
        return (
            jsonify({"error": "limit must be between 1 and 1000", "type": "bad_request"}),
            400,
        )
    store = _store()
    conn = store.connect()
    return jsonify({"jobs": list_jobs(conn, user=user, limit=limit)})
