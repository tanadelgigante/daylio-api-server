"""Application configuration loading, validation and SQLite persistence.

=============================== MAINTENANCE NOTES ===============================
Module      : src/daylio_api/config.py
Scope       : Fase 0 (Task 0.4) + Fase 1 (Tasks 1.2/1.4) — read the YAML
              configuration schema described in docs/WORKPLAN.md §6, merge
              environment variables and per-user overrides, validate the
              resulting object with fail-fast errors (R15), and persist the
              columns/scales/mood schema into SQLite through
              ``seed_defaults`` / ``sync_schema`` (columns_config,
              scales_config, mood_map). The public API (``load``,
              ``AppConfig``, ``ConfigError``) is stable since Fase 0.

Behaviour   :
  - Base file : ``config/config.example.yml`` by default; in production set the
    ``CONFIG_FILE`` env var to a real deployment file with the same schema.
  - Per-user  : optional ``users`` section inside the base file and/or a
    dedicated users file (``USERS_CONFIG_FILE``, schema = ``users.example.yml``).
    User overrides are merged under ``AppConfig.users[<username>]``.
  - Env merge : the following variables override YAML values:
    ``INFLUX_URL``, ``INFLUX_TOKEN``, ``INFLUX_ORG`` (default ``tanadelgigante``),
    ``INFLUX_BUCKET`` (default ``daylio``), ``INFLUX_INSTANCE`` (CI: builds
    ``http://<instance>`` when ``INFLUX_URL`` is missing, D18),
    ``ADMIN_USERNAME``, ``ADMIN_PASSWORD``, ``LOG_LEVEL``, ``DATA_DIR``,
    ``DB_PATH``.
  - "env:VAR"  : YAML values of the form ``env:VAR_NAME`` are resolved from the
    process environment. If the variable is missing the literal ``env:VAR_NAME``
    is kept so that loading the example file never fails (fail-fast applies to
    *schema* errors, not to optional secrets).
  - SQLite     : ``seed_defaults(conn)`` inserts the exact D1/D2 defaults on
    empty tables; ``sync_schema(conn, cfg)`` reconciles YAML -> SQLite. The
    ``app.schema_source`` key selects the authority:
      * ``yaml`` (default) — YAML wins: rows are upserted by name on every boot;
      * ``db``             — SQLite is authoritative: only *missing* names are
        inserted, administrative edits of existing rows are never overwritten.
    ``seed_defaults``/``sync_schema`` expect ``db.migrations.migrate`` to have
    created the tables first (the app factory does this automatically).

Validation : see ``_validate``. Phase-0 checks (unchanged) plus Fase 1 extras:
  - mood: mapping label -> int with exactly 5 labels whose values are 1..5 (D1),
    non-empty string labels;
  - scales: each text scale has a non-empty ``values`` list (or label->rank
    mapping) with unique ranks and non-empty labels, each number scale has
    ``min``/``max`` integers, ``source`` must be present;
  - columns: ``required`` is a non-empty list that includes ``full_date`` (D8.b),
    ``optional``/``derive`` are lists of non-empty names, no required/optional
    overlap, every derived column is declared in required or optional;
  - scheduler: ``interval_seconds > 0`` and ``start_hour`` in 0..23;
  - auth override sanity and ``app.schema_source`` in {yaml, db}.
================================================================================
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from daylio_api.logger import get_logger

log = get_logger("config")

#: Level names accepted by the logger. WARN/FATAL are aliases used by the
#: WORKPLAN; Python's logging API spells them WARNING/CRITICAL.
LOG_LEVELS = {"DEBUG", "INFO", "WARN", "WARNING", "ERROR", "FATAL", "CRITICAL"}

#: Fallback used when no config file is provided and CONFIG_FILE is unset.
#: Resolved relative to the package so it works from any working directory
#: (local repo, installed package, Docker image with config/ baked in).
_DEFAULT_CONFIG_FILE = str(Path(__file__).resolve().parents[2] / "config" / "config.example.yml")

#: Defaults confirmed with the user on 2026-09-22 (D18).
_DEFAULT_INFLUX_ORG = "tanadelgigante"
_DEFAULT_INFLUX_BUCKET = "daylio"


class ConfigError(ValueError):
    """Raised when the configuration is structurally or semantically invalid."""


@dataclass
class AppConfig:
    """Validated application configuration.

    The private ``_raw`` attribute keeps the original merged dictionary so that
    later phases (importers, stores, scheduler) can read any section without
    adding accessors to this dataclass.
    """

    app: dict[str, Any] = field(default_factory=dict)
    storage: dict[str, Any] = field(default_factory=dict)
    mood: dict[str, Any] = field(default_factory=dict)
    scales: dict[str, Any] = field(default_factory=dict)
    columns: dict[str, Any] = field(default_factory=dict)
    scheduler: dict[str, Any] = field(default_factory=dict)
    auth: dict[str, Any] = field(default_factory=dict)
    logging: dict[str, Any] = field(default_factory=dict)
    users: dict[str, Any] = field(default_factory=dict)
    _raw: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict[str, Any]:
        """Return a shallow copy of the merged configuration dictionary."""
        return dict(self._raw)

    @property
    def effective_log_level(self) -> str:
        """Resolve the log level: env ``LOG_LEVEL`` wins, then YAML, else INFO."""
        env_level = os.environ.get("LOG_LEVEL")
        yaml_level = self.app.get("log_level") or self.logging.get("level")
        return str(env_level or yaml_level or "INFO")


# --------------------------------------------------------------------------- #
# Public loading API
# --------------------------------------------------------------------------- #
def load(
    config_file: str | os.PathLike[str] | None = None,
    users_file: str | os.PathLike[str] | None = None,
) -> AppConfig:
    """Load, merge (env + per-user) and validate the application configuration.

    Args:
        config_file: Path to the base YAML file. Defaults to
            ``config/config.example.yml``; the ``CONFIG_FILE`` env var overrides
            the default when ``config_file`` is None.
        users_file: Path to a file containing per-user overrides (schema of
            ``config/users.example.yml``). Defaults to the ``USERS_CONFIG_FILE``
            env var if set; otherwise only the ``users:`` section of the base
            file is used.

    Raises:
        ConfigError: if the YAML is missing/unreadable/invalid (fail-fast R15).
    """
    file_to_load = Path(config_file or os.environ.get("CONFIG_FILE") or _DEFAULT_CONFIG_FILE)
    raw = _read_yaml(file_to_load)
    _resolve_env_refs(raw)

    # Per-user overrides: env-provided users file, then the ``users`` section
    # embedded in the base configuration. Explicitly passed file wins.
    merged_users: dict[str, Any] = {}
    embedded_users = raw.pop("users", None)
    if isinstance(embedded_users, dict):
        merged_users.update(embedded_users)

    if users_file is not None:
        users_path = Path(users_file)
    elif os.environ.get("USERS_CONFIG_FILE"):
        users_path = Path(os.environ["USERS_CONFIG_FILE"])
    else:
        users_path = None
    if users_path is not None:
        users_raw = _read_yaml(users_path)
        if "users" in users_raw and isinstance(users_raw["users"], dict):
            merged_users.update(users_raw["users"])
        else:
            merged_users.update(users_raw)  # bare username -> override mapping

    _apply_env_overrides(raw)

    cfg = AppConfig(
        app=raw.get("app") or {},
        storage=raw.get("storage") or {},
        mood=raw.get("mood") or {},
        scales=raw.get("scales") or {},
        columns=raw.get("columns") or {},
        scheduler=raw.get("scheduler") or {},
        auth=raw.get("auth") or {},
        logging=raw.get("logging") or {},
        users=merged_users,
        _raw=raw,
    )
    _validate(cfg)
    return cfg


def load_users_file(users_file: str | os.PathLike[str]) -> dict[str, Any]:
    """Load a standalone per-user overrides file without validating the app.

    Convenience helper for admin scripts; merges the plain ``{username: {...}}``
    format used by ``config/users.example.yml``.
    """
    data = _read_yaml(Path(users_file))
    return data.get("users", data) if isinstance(data, dict) else {}


# --------------------------------------------------------------------------- #
# Fase 1 (R1): SQLite persistence for columns / scales / mood configuration
# --------------------------------------------------------------------------- #
#: Exact mood map seeded on first boot (Task 1.4, D1: 1 = best … 5 = worst).
#: Labels verified against the real exports in sample-data/ (2026-09-22).
DEFAULT_MOOD_MAP: tuple[tuple[int, str], ...] = (
    (1, "alla grande"),
    (2, "bene"),
    (3, "così così"),
    (4, "male"),
    (5, "malissimo"),
)

#: Display labels for known scale keys (CSV `scales` column and .daylio backups
#: use the display form, e.g. ``Stress: Moderato 3/5``).
_SCALE_LABELS: dict[str, str] = {"stress": "Stress", "scala_salute": "Scala salute"}

#: Semantic type for every known record column (D8). Fase 4 serializers use it.
_COLUMN_TYPES: dict[str, str] = {
    "full_date": "date",
    "date": "date",
    "weekday": "text",
    "time": "time",
    "mood": "text",
    "activities": "list",
    "scales": "scale",
    "note_title": "text",
    "note": "text",
}

#: UI/API label for every known record column.
_COLUMN_LABELS: dict[str, str] = {
    "full_date": "Full date",
    "date": "Date",
    "weekday": "Weekday",
    "time": "Time",
    "mood": "Mood",
    "activities": "Activities",
    "scales": "Scales",
    "note_title": "Note title",
    "note": "Note",
}

#: Free-form per-column options persisted as JSON (D8.d: activities separator).
_COLUMN_OPTIONS: dict[str, dict[str, Any]] = {"activities": {"separator": " | "}}

#: SQL fragments used by seed_defaults/sync_schema (parameterised; row dicts
#: must expose exactly the named keys).
_INSERT_COLUMN_SQL = (
    "INSERT INTO columns_config "
    "(user_id, column_name, column_type, required, source, derived, label, "
    " sort_order, enabled, options_json) "
    "VALUES (NULL, :column_name, :column_type, :required, :source, :derived, "
    " :label, :sort_order, :enabled, :options_json)"
)
_UPDATE_COLUMN_SQL = (
    "UPDATE columns_config SET column_type = :column_type, required = :required, "
    "source = :source, derived = :derived, label = :label, "
    "sort_order = :sort_order, enabled = :enabled, options_json = :options_json, "
    "updated_at = datetime('now') "
    "WHERE column_name = :column_name AND user_id IS NULL"
)
_INSERT_SCALE_SQL = (
    "INSERT INTO scales_config "
    "(user_id, scale_name, label, scale_type, source, tag_group, min_value, "
    " max_value, levels_count, values_json, sort_order, enabled) "
    "VALUES (NULL, :scale_name, :label, :scale_type, :source, :tag_group, "
    " :min_value, :max_value, :levels_count, :values_json, :sort_order, :enabled)"
)
_UPDATE_SCALE_SQL = (
    "UPDATE scales_config SET label = :label, scale_type = :scale_type, "
    "source = :source, tag_group = :tag_group, min_value = :min_value, "
    "max_value = :max_value, levels_count = :levels_count, "
    "values_json = :values_json, sort_order = :sort_order, enabled = :enabled, "
    "updated_at = datetime('now') "
    "WHERE scale_name = :scale_name AND user_id IS NULL"
)


def parse_tag_group(source: str) -> int | None:
    """Extract ``N`` from a ``tag-group:N`` source string (D2), else None."""
    if isinstance(source, str) and source.startswith("tag-group:"):
        try:
            return int(source.split(":", 1)[1].strip())
        except ValueError:
            return None
    return None


def scale_label(name: str) -> str:
    """Display label for a scale key (``scala_salute`` -> ``Scala salute``)."""
    if name in _SCALE_LABELS:
        return _SCALE_LABELS[name]
    return str(name).replace("_", " ")


def _mk_column(
    name: str, sort_order: int, *, required: bool = False, derived: bool = False
) -> dict[str, Any]:
    """Build a columns_config row for a named column (defaults decision D8)."""
    return {
        "column_name": name,
        "column_type": _COLUMN_TYPES.get(name, "text"),
        "required": int(required),
        "source": "derived" if derived else "csv+daylio",
        "derived": int(derived),
        "label": _COLUMN_LABELS.get(name, name),
        "sort_order": sort_order,
        "enabled": 1,
        "options_json": json.dumps(_COLUMN_OPTIONS.get(name, {}), sort_keys=True),
    }


def _mk_scale(
    name: str,
    sort_order: int,
    *,
    source: str,
    values: Any,
    tag_group: int | None = None,
    min_value: int | None = None,
    max_value: int | None = None,
) -> dict[str, Any]:
    """Build a scales_config row from a name + ordered values (D2, D8.c)."""
    if isinstance(values, dict):
        ranked = [
            {"label": str(label), "rank": int(rank)}
            for label, rank in sorted(values.items(), key=lambda kv: int(kv[1]))
        ]
    else:
        ranked = [{"label": str(v), "rank": rank} for rank, v in enumerate(values, start=1)]
    is_number = min_value is not None or max_value is not None
    return {
        "scale_name": name,
        "label": scale_label(name),
        "scale_type": "number" if is_number else "text",
        "source": source,
        "tag_group": tag_group if tag_group is not None else parse_tag_group(source),
        "min_value": min_value,
        "max_value": max_value,
        "levels_count": 0 if is_number else len(ranked),
        "values_json": json.dumps(ranked, ensure_ascii=False),
        "sort_order": sort_order,
        "enabled": 1,
    }


#: Exact default columns (Task 1.4): full_date mandatory (CSV and .daylio),
#: date/weekday derived (D8.a), the remaining columns optional (D8.b).
DEFAULT_COLUMNS: tuple[dict[str, Any], ...] = (
    _mk_column("full_date", 10, required=True),
    _mk_column("date", 20, derived=True),
    _mk_column("weekday", 30, derived=True),
    _mk_column("time", 40),
    _mk_column("mood", 50),
    _mk_column("activities", 60),
    _mk_column("scales", 70),
    _mk_column("note_title", 80),
    _mk_column("note", 90),
)

#: Exact default scales (Task 1.4): Stress (column `scales`, 5 levels) and
#: Scala salute (tag-group:5 / activities, 8 levels, D2).
DEFAULT_SCALES: tuple[dict[str, Any], ...] = (
    _mk_scale(
        "stress",
        10,
        source="column",
        values=["Calma", "Basso", "Moderato", "Alto", "Sopraffatto"],
    ),
    _mk_scale(
        "scala_salute",
        20,
        source="tag-group:5",
        tag_group=5,
        values=[
            "ottimo",
            "buono",
            "discreto",
            "sufficiente",
            "mediocre",
            "insufficiente",
            "pessimo",
            "terribile",
        ],
    ),
)


def seed_defaults(conn: sqlite3.Connection) -> dict[str, int]:
    """Seed the exact Phase-1 defaults into *empty* config tables (Task 1.4).

    Each table is seeded only when empty (idempotent); rows are never updated
    here, so existing data is preserved. Commits the transaction.

    Precondition: ``db.migrations.migrate(conn)`` already created the tables.

    Returns:
        ``{"mood": n, "scales": n, "columns": n}`` with the number of rows
        inserted per table (0 when the table already had data).
    """
    seeded = {"mood": 0, "scales": 0, "columns": 0}
    if _is_table_empty(conn, "mood_map"):
        conn.executemany(
            "INSERT INTO mood_map (mood_level, label) VALUES (?, ?)",
            DEFAULT_MOOD_MAP,
        )
        seeded["mood"] = len(DEFAULT_MOOD_MAP)
    if _is_table_empty(conn, "scales_config"):
        conn.executemany(
            _INSERT_SCALE_SQL,
            [dict(row) for row in DEFAULT_SCALES],
        )
        seeded["scales"] = len(DEFAULT_SCALES)
    if _is_table_empty(conn, "columns_config"):
        conn.executemany(
            _INSERT_COLUMN_SQL,
            [dict(row) for row in DEFAULT_COLUMNS],
        )
        seeded["columns"] = len(DEFAULT_COLUMNS)
    conn.commit()
    log.info("Seeded config defaults into SQLite: %s", seeded)
    return seeded


def sync_schema(conn: sqlite3.Connection, cfg: AppConfig) -> dict[str, Any]:
    """Reconcile the validated YAML config with the SQLite config tables.

    Authority is selected by ``app.schema_source`` (Task 1.2):
      * ``yaml`` (default): every boot upserts mood_map/scales_config/
        columns_config rows by name — YAML always wins;
      * ``db``            : SQLite is authoritative — only *missing* names are
        inserted, administrative edits of existing rows are preserved.

    Rows are never deleted here (a rename/removal requires an explicit admin
    action, to be exposed by the Phase-5 admin API). Commits the transaction.

    Precondition: ``db.migrations.migrate(conn)`` already created the tables.

    Returns:
        Statistics dict: ``{"mode", "seeded", "mood", "scales", "columns"}``
        with per-table inserted/updated/preserved counters.
    """
    seeded = seed_defaults(conn)
    mode = str((cfg.app or {}).get("schema_source", "yaml")).lower()
    insert_only = mode == "db"

    stats: dict[str, Any] = {
        "mode": mode,
        "seeded": seeded,
        "mood": {"inserted": 0, "preserved": 0},
        "scales": {"inserted": 0, "updated": 0, "preserved": 0},
        "columns": {"inserted": 0, "updated": 0, "preserved": 0},
    }

    for level, label in _mood_rows_from_cfg(cfg):
        existing = conn.execute(
            "SELECT label FROM mood_map WHERE mood_level = ?", (level,)
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO mood_map (mood_level, label) VALUES (?, ?)", (level, label)
            )
            stats["mood"]["inserted"] += 1
        elif existing["label"] == label:
            continue  # already in sync
        elif insert_only:
            stats["mood"]["preserved"] += 1
        else:
            conn.execute(
                "UPDATE mood_map SET label = ?, updated_at = datetime('now') "
                "WHERE mood_level = ?",
                (label, level),
            )
            stats["mood"]["inserted"] += 1

    for row in _scale_rows_from_cfg(cfg):
        existing = conn.execute(
            "SELECT id FROM scales_config WHERE scale_name = :scale_name AND user_id IS NULL",
            {"scale_name": row["scale_name"]},
        ).fetchone()
        if existing is None:
            conn.execute(_INSERT_SCALE_SQL, dict(row))
            stats["scales"]["inserted"] += 1
        elif insert_only:
            stats["scales"]["preserved"] += 1
        else:
            conn.execute(_UPDATE_SCALE_SQL, dict(row))
            stats["scales"]["updated"] += 1

    for row in _column_rows_from_cfg(cfg):
        existing = conn.execute(
            "SELECT id FROM columns_config WHERE column_name = :column_name AND user_id IS NULL",
            {"column_name": row["column_name"]},
        ).fetchone()
        if existing is None:
            conn.execute(_INSERT_COLUMN_SQL, dict(row))
            stats["columns"]["inserted"] += 1
        elif insert_only:
            stats["columns"]["preserved"] += 1
        else:
            conn.execute(_UPDATE_COLUMN_SQL, dict(row))
            stats["columns"]["updated"] += 1

    conn.commit()
    log.info("SQLite config sync (mode=%s): %s", mode, stats)
    return stats


def _is_table_empty(conn: sqlite3.Connection, table: str) -> bool:
    """Return True when ``table`` has zero rows (internal allow-listed names)."""
    row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    return int(row[0]) == 0


def _mood_rows_from_cfg(cfg: AppConfig) -> list[tuple[int, str]]:
    """Derive mood_map rows ``(level, label)`` from ``cfg.mood.values`` (D1)."""
    values = (cfg.mood or {}).get("values") or {}
    return [
        (int(rank), str(label))
        for label, rank in sorted(values.items(), key=lambda kv: int(kv[1]))
    ]


def _scale_rows_from_cfg(cfg: AppConfig) -> list[dict[str, Any]]:
    """Derive scales_config rows from ``cfg.scales`` (YAML key order kept)."""
    rows: list[dict[str, Any]] = []
    for index, (name, definition) in enumerate((cfg.scales or {}).items(), start=1):
        if not isinstance(definition, dict):
            continue
        scale_type = definition.get("type", "text")
        if scale_type == "number":
            rows.append(
                _mk_scale(
                    name,
                    index * 10,
                    source=str(definition.get("source", "")),
                    values=[],
                    min_value=definition.get("min"),
                    max_value=definition.get("max"),
                )
            )
        else:
            rows.append(
                _mk_scale(
                    name,
                    index * 10,
                    source=str(definition.get("source", "")),
                    values=definition.get("values", []),
                )
            )
    return rows


def _column_rows_from_cfg(cfg: AppConfig) -> list[dict[str, Any]]:
    """Derive columns_config rows from ``cfg.columns`` (required then optional)."""
    required = list((cfg.columns or {}).get("required") or [])
    optional = list((cfg.columns or {}).get("optional") or [])
    derive = set((cfg.columns or {}).get("derive") or [])
    names = list(required) + [name for name in optional if name not in required]
    return [
        _mk_column(name, index * 10, required=name in required, derived=name in derive)
        for index, name in enumerate(names, start=1)
    ]


# --------------------------------------------------------------------------- #
# Internal helpers
# --------------------------------------------------------------------------- #
def _read_yaml(path: Path) -> dict[str, Any]:
    """Read and parse a YAML file as a dictionary (empty dict if absent).

    MAINTENANCE NOTES:
        - Only the YAML subset needed for config is supported. PyYAML's
          ``safe_load`` is used on purpose: application config is not trusted
          input and arbitrary object construction would be a security risk.
    """
    try:
        with Path(path).open(encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except FileNotFoundError as exc:
        raise ConfigError(f"Configuration file not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {path}: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(
            f"Configuration root must be a mapping (got {type(data).__name__}) in {path}"
        )
    return data


def _resolve_env_refs(mapping: dict[str, Any]) -> None:
    """Replace ``env:VAR_NAME`` string values with the env value, in place.

    Missing environment variables are kept as the original ``env:VAR_NAME``
    placeholder so that loading the *example* configuration is always possible.
    """

    def _walk(node: Any) -> Any:
        if isinstance(node, dict):
            return {k: _walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [_walk(v) for v in node]
        if isinstance(node, str) and node.startswith("env:"):
            var_name = node[len("env:") :]
            return os.environ.get(var_name, node)
        return node

    mapping.update(_walk(mapping))


def _apply_env_overrides(cfg: dict[str, Any]) -> None:
    """Apply real environment overrides on top of the YAML values, in place."""
    storage = cfg.setdefault("storage", {})
    if os.environ.get("INFLUX_URL"):
        storage["influx_url"] = os.environ["INFLUX_URL"]
    if os.environ.get("INFLUX_TOKEN"):
        storage["influx_token"] = os.environ["INFLUX_TOKEN"]
    storage.setdefault("influx_org", _DEFAULT_INFLUX_ORG)
    storage.setdefault("influx_bucket", _DEFAULT_INFLUX_BUCKET)
    if os.environ.get("INFLUX_ORG"):
        storage["influx_org"] = os.environ["INFLUX_ORG"]
    if os.environ.get("INFLUX_BUCKET"):
        storage["influx_bucket"] = os.environ["INFLUX_BUCKET"]

    # CI/test shortcut: INFLUX_INSTANCE = "<ip>:<port>" builds the full URL
    # (D18). Only used when the URL is not explicitly provided.
    instance = os.environ.get("INFLUX_INSTANCE")
    current_url = storage.get("influx_url") or "env:INFLUX_URL"
    if instance and (not os.environ.get("INFLUX_URL") or current_url == "env:INFLUX_URL"):
        prefix = "" if instance.startswith(("http://", "https://")) else "http://"
        storage["influx_url"] = f"{prefix}{instance}"

    if os.environ.get("DB_PATH"):
        storage["sqlite_path"] = os.environ["DB_PATH"]
    if os.environ.get("DATA_DIR"):
        cfg.setdefault("data", {})["dir"] = os.environ["DATA_DIR"]

    auth = cfg.setdefault("auth", {})
    if os.environ.get("ADMIN_USERNAME"):
        auth["admin_username"] = os.environ["ADMIN_USERNAME"]
    if os.environ.get("ADMIN_PASSWORD"):
        auth["admin_password"] = os.environ["ADMIN_PASSWORD"]

    if os.environ.get("LOG_LEVEL"):
        cfg.setdefault("app", {})["log_level"] = os.environ["LOG_LEVEL"]
        cfg.setdefault("logging", {})["level"] = os.environ["LOG_LEVEL"]


def _validate(cfg: AppConfig) -> None:
    """Fail-fast validation (R15). All errors are ConfigError with clear text."""
    # --- logging level --------------------------------------------------- #
    log_level = cfg.effective_log_level.upper()
    if log_level not in LOG_LEVELS:
        raise ConfigError(
            f"Invalid LOG_LEVEL {log_level!r}; expected one of {', '.join(sorted(LOG_LEVELS))}"
        )

    # --- mood scale (D1) ------------------------------------------------ #
    mood_values = cfg.mood.get("values") if isinstance(cfg.mood, dict) else None
    if not isinstance(mood_values, dict) or len(mood_values) != 5:
        raise ConfigError(
            "mood.values must map exactly 5 labels to indices (1=best..5=worst, D1)"
        )
    try:
        indices = sorted({int(v) for v in mood_values.values()})
    except (TypeError, ValueError) as exc:
        raise ConfigError("mood.values must contain only integer indices") from exc
    if indices != [1, 2, 3, 4, 5]:
        raise ConfigError(f"mood.values indices must be 1..5, got {indices}")
    for label in mood_values:
        if not isinstance(label, str) or not label.strip():
            raise ConfigError("mood.values labels must be non-empty strings")

    # --- custom scales --------------------------------------------------- #
    if not isinstance(cfg.scales, dict):
        raise ConfigError("scales must be a mapping of scale-name -> definition")
    for name, definition in cfg.scales.items():
        if not isinstance(definition, dict):
            raise ConfigError(f"scale {name!r} must be a mapping")
        scale_type = definition.get("type")
        if scale_type not in {"text", "number"}:
            raise ConfigError(
                f"scale {name!r}: type must be 'text' or 'number', got {scale_type!r}"
            )
        if "source" not in definition or not definition["source"]:
            raise ConfigError(f"scale {name!r}: missing or empty 'source'")
        if scale_type == "text":
            values = definition.get("values")
            if isinstance(values, dict):
                _validate_ranked_values(name, values)
            elif not isinstance(values, list) or not values:
                raise ConfigError(f"text scale {name!r} must have a non-empty 'values' list")
            else:
                folded: list[str] = []
                for value in values:
                    if not isinstance(value, str) or not value.strip():
                        raise ConfigError(
                            f"text scale {name!r}: values must be non-empty strings"
                        )
                    folded.append(value.strip().casefold())
                if len(set(folded)) != len(folded):
                    raise ConfigError(
                        f"text scale {name!r}: values must be unique (duplicate level found)"
                    )
        elif scale_type == "number":
            for key in ("min", "max"):
                if not isinstance(definition.get(key), int):
                    raise ConfigError(f"number scale {name!r} must define integer {key!r}")

    # --- columns (D8.b) -------------------------------------------------- #
    required = cfg.columns.get("required")
    if not isinstance(required, list) or not required:
        raise ConfigError("columns.required must be a non-empty list")
    if "full_date" not in required:
        raise ConfigError("columns.required must include 'full_date'")
    for column in required:
        if not isinstance(column, str) or not column:
            raise ConfigError("columns.required entries must be non-empty names")

    optional = cfg.columns.get("optional")
    if optional is not None:
        if not isinstance(optional, list) or not all(
            isinstance(column, str) and column for column in optional
        ):
            raise ConfigError("columns.optional must be a list of non-empty column names")
        overlap = set(optional) & set(required)
        if overlap:
            raise ConfigError(
                f"columns.optional must not repeat required columns: {sorted(overlap)}"
            )

    derive = cfg.columns.get("derive")
    if derive is not None:
        if not isinstance(derive, list) or not all(
            isinstance(column, str) and column for column in derive
        ):
            raise ConfigError("columns.derive must be a list of non-empty column names")
        known = set(required) | set(optional or [])
        unknown = [column for column in derive if column not in known]
        if unknown:
            raise ConfigError(
                "columns.derive entries must be declared as required or optional: "
                f"{unknown}"
            )

    # --- schema source (Fase 1 authority) -------------------------------- #
    schema_source = (cfg.app or {}).get("schema_source", "yaml")
    if schema_source not in {"yaml", "db"}:
        raise ConfigError("app.schema_source must be 'yaml' or 'db'")

    # --- scheduler ------------------------------------------------------- #
    scheduler = cfg.scheduler or {}
    interval = scheduler.get("interval_seconds")
    if not isinstance(interval, int) or interval <= 0:
        raise ConfigError("scheduler.interval_seconds must be a positive integer")
    start_hour = scheduler.get("start_hour")
    if not isinstance(start_hour, int) or not (0 <= start_hour <= 23):
        raise ConfigError("scheduler.start_hour must be an integer in 0..23")

    # --- per-user scheduler/scales sanity --------------------------------- #
    for username, overrides in cfg.users.items():
        if not isinstance(overrides, dict):
            raise ConfigError(f"user {username!r} overrides must be a mapping")
        user_scheduler = overrides.get("scheduler")
        if isinstance(user_scheduler, dict):
            interval = user_scheduler.get("interval_seconds")
            if "interval_seconds" in user_scheduler and (
                not isinstance(interval, int) or interval <= 0
            ):
                raise ConfigError(
                    f"user {username!r}: scheduler.interval_seconds must be a positive integer"
                )
            hour = user_scheduler.get("start_hour")
            if "start_hour" in user_scheduler and not (
                isinstance(hour, int) and 0 <= hour <= 23
            ):
                raise ConfigError(
                    f"user {username!r}: scheduler.start_hour must be an integer in 0..23"
                )
        user_scales = overrides.get("scales")
        if isinstance(user_scales, dict):
            _validate_user_scales(username, user_scales)


def mood_override_values(definition: Any) -> dict[str, Any] | None:
    """Extract the label -> rank mapping of a per-user mood override.

    Accepts both documented forms (maintained in sync with the validator):
      - direct mapping : ``{"fantastico": 1, ..., "malissimo": 5}`` (users.example.yml);
      - wrapper        : ``{"type": "text", "values": {"fantastico": 1, ...}}``
                         (generic scale shape, accepted "for symmetry").

    Returns the label -> rank dict, or None when ``definition`` is not a
    mapping that carries a mood scale. Semantic validation (exactly 5 labels,
    indices 1..5, unique ranks) stays in ``_validate_user_scales`` / the
    same checks applied at merge time.
    """
    if not isinstance(definition, dict) or not definition:
        return None
    if "values" in definition:
        values = definition["values"]
        return values if isinstance(values, dict) else None
    return definition


def _validate_user_scales(username: str, scales: dict[str, Any]) -> None:
    """Lightweight validation of per-user scale overrides (R4).

    MAINTENANCE NOTES:
        - A user ``mood`` override follows the users.example.yml format: a
          direct label -> index mapping (``mood: {"fantastico": 1, ...}``).
          The generic ``{"type": "...", "values": {...}}`` wrapper is also
          accepted for symmetry, so admin configuration stays homogeneous.
        - Use :func:`mood_override_values` whenever the mood mapping is read
          at runtime (routes merge) so the accepted shapes stay consistent.
    """
    for name, definition in scales.items():
        if not isinstance(definition, dict):
            raise ConfigError(f"user {username!r}: scale {name!r} must be a mapping")
        if "type" in definition and definition["type"] not in {"text", "number"}:
            raise ConfigError(f"user {username!r}: scale {name!r} type must be 'text' or 'number'")
        values = definition.get("values")
        if name == "mood":
            # Normalize both documented forms before the D1 checks.
            values = mood_override_values(definition)
            # D1: mood overrides must still map exactly 5 labels onto 1..5.
            if not isinstance(values, dict) or len(values) != 5:
                raise ConfigError(
                    f"user {username!r}: mood scale must map exactly 5 labels to indices "
                    "(1=best..5=worst, D1)"
                )
            try:
                user_indices = sorted({int(v) for v in values.values()})
            except (TypeError, ValueError) as exc:
                raise ConfigError(
                    f"user {username!r}: mood scale indices must be integers"
                ) from exc
            if user_indices != [1, 2, 3, 4, 5]:
                raise ConfigError(
                    f"user {username!r}: mood scale indices must be 1..5, got {user_indices}"
                )
            for label in values:
                if not isinstance(label, str) or not label.strip():
                    raise ConfigError(
                        f"user {username!r}: mood scale labels must be non-empty strings"
                    )
        elif definition.get("type") == "text":
            if not isinstance(values, list) or not values:
                raise ConfigError(
                    f"user {username!r}: text scale {name!r} must have non-empty 'values'"
                )
        elif definition.get("type") == "number":
            for key in ("min", "max"):
                if not isinstance(definition.get(key), int):
                    raise ConfigError(
                        f"user {username!r}: number scale {name!r} must define integer {key!r}"
                    )
        if isinstance(values, dict):
            _validate_ranked_values(f"user {username!r} scale {name!r}", values)


def _validate_ranked_values(scale_ref: str, mapping: dict[str, Any]) -> None:
    """Validate a label -> rank mapping: non-empty string labels, int unique ranks."""
    if not mapping:
        raise ConfigError(f"{scale_ref}: 'values' mapping is empty")
    ranks: list[int] = []
    for label, rank in mapping.items():
        if not isinstance(label, str) or not label.strip():
            raise ConfigError(f"{scale_ref}: values labels must be non-empty strings")
        if not isinstance(rank, int):
            raise ConfigError(f"{scale_ref}: rank for {label!r} must be an integer")
        ranks.append(rank)
    if len(set(ranks)) != len(ranks):
        raise ConfigError(f"{scale_ref}: ranks must be unique")
