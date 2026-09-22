"""Application configuration loading and validation.

=============================== MAINTENANCE NOTES ===============================
Module      : src/daylio_api/config.py
Scope       : Fase 0 (Task 0.4) — read the YAML configuration schema described
              in docs/WORKPLAN.md §6, merge environment variables and per-user
              overrides, and validate the resulting object with fail-fast errors.
              In Fase 1 this module is extended with SQLite-backed persistence
              (columns_config / scales_config / mood_map), but the public API
              (``load``, ``AppConfig``, ``ConfigError``) is kept stable.

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

Validation : see ``_validate``. Base checks required by Task 0.4:
  - mood: mapping label -> int with exactly 5 labels whose values are 1..5 (D1);
  - scales: each text scale has a non-empty ``values`` list, each number scale
    has ``min``/``max`` integers, ``source`` must be present;
  - columns: ``required`` is a non-empty list that includes ``full_date`` (D8.b);
  - scheduler: ``interval_seconds > 0`` and ``start_hour`` in 0..23.
================================================================================
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

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
            if not isinstance(values, list) or not values:
                raise ConfigError(f"text scale {name!r} must have a non-empty 'values' list")
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


def _validate_user_scales(username: str, scales: dict[str, Any]) -> None:
    """Lightweight validation of per-user scale overrides (R4)."""
    for name, definition in scales.items():
        if not isinstance(definition, dict):
            raise ConfigError(f"user {username!r}: scale {name!r} must be a mapping")
        if "type" in definition and definition["type"] not in {"text", "number"}:
            raise ConfigError(f"user {username!r}: scale {name!r} type must be 'text' or 'number'")
        if definition.get("type") == "text":
            values = definition.get("values")
            if not isinstance(values, list) or not values:
                raise ConfigError(
                    f"user {username!r}: text scale {name!r} must have non-empty 'values'"
                )
        if "values" in definition and isinstance(definition["values"], dict):
            # user mood-like mapping (label -> index)
            if not definition["values"]:
                raise ConfigError(f"user {username!r}: scale {name!r} 'values' mapping is empty")
