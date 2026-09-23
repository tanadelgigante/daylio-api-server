"""API blueprint: version + read-only configuration endpoints (Fase 1 / R1).

=============================== MAINTENANCE NOTES ===============================
Scope    : Fase 0 predicate (Task 0.2) + Fase 1 (Task 1.3). Fase 1 exposes
           ONLY configuration reads — ``GET /api/version``,
           ``GET /api/columns`` and ``GET /api/scales``. The parametric
           records API, import endpoints, analytics and auth arrive in Phases
           4-5 (WORKPLAN §7); do not implement them here yet.

Source   : Responses are served from the SQLite configuration tables
           (columns_config / scales_config / mood_map) bootstrapped by
           ``config.seed_defaults`` + ``config.sync_schema`` at app startup.
           The JSON shapes documented in each endpoint docstring are the
           contract Phase 4 consumers can rely on. Optional ``?user=<name>``
           merges the YAML per-user scale overrides and (for columns) the
           ``columns.optional`` allow-list defined in ``users.example.yml``.

Helpers  : ``_store()`` resolves the SqliteStore from ``app.extensions``;
           ``_merge_user_scales`` / ``_scale_from_override`` implement the
           per-user merge without writing to SQLite (multi-user rows land in
           Phase 5).
==================================================================================
"""

from __future__ import annotations

from typing import Any

from flask import Blueprint, abort, current_app, jsonify, request

from daylio_api import __version__
from daylio_api.config import mood_override_values, parse_tag_group, scale_label
from daylio_api.db.sqlite_store import SqliteStore

bp = Blueprint("api", __name__, url_prefix="/api")


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
