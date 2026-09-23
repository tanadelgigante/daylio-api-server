"""Versioned, idempotent SQLite migrations (Fase 1 — Task 1.1).

=============================== MAINTENANCE NOTES ===============================
Module   : src/daylio_api/db/migrations.py
Scope    : Fase 1 (R1) — real schema for the configurable columns/scales system:
           ``columns_config``, ``scales_config`` and ``mood_map`` (WORKPLAN §5.3,
           decisions D1/D2/D8). Versioning uses ``PRAGMA user_version``: every
           step is idempotent (``CREATE TABLE IF NOT EXISTS`` /
           ``CREATE INDEX IF NOT EXISTS``) and bumps the version inside the same
           transaction, so a partially applied migration rolls back as a whole.

Contract : ``apply(conn, to_version=None) -> int`` (stable since Fase 0) and the
           ``migrate(conn)`` alias required by Task 1.1 — both are safe to call
           on every boot and from tests. ``conn`` may be any sqlite3 connection
           (file or in-memory); DDL statements are executed one by one (NOT via
           ``executescript``, which would implicitly COMMIT and break the
           transactional version bump).

Extending: phases 2-5 append new ``(name, fn)`` tuples to ``_MIGRATIONS``.
           Never edit an already-released step: ``PRAGMA user_version`` would
           not re-run it on existing databases. Use a new step + ``ALTER TABLE``
           with an idempotent guard instead.

Note      : ``user_id`` columns are plain INTEGERs (no FK) until the ``users``
           table exists (Phase 5); the FK is added there with a guarded
           migration. NULL ``user_id`` = global/default row, uniqueness is
           enforced through partial unique indexes (SQLite treats NULLs as
           distinct in plain UNIQUE constraints).
==================================================================================
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence

from daylio_api.logger import get_logger

log = get_logger("db.migrations")


def _migration_001_config_tables(conn: sqlite3.Connection) -> None:
    """v1: configuration tables for columns, scales and the mood map (R1).

    Idempotent by construction: safe to re-run on any database. See the module
    docstring for the ``user_id IS NULL`` uniqueness strategy.
    """
    statements = (
        # ---------------------------------------------------------------- #
        # columns_config — configurable columns of a Daylio record (D8).
        #   column_type : date | time | text | list | scale (semantic hint)
        #   required    : 1 only for full_date (D8.b)
        #   source      : "csv+daylio" | "derived" (D8.a) | future values
        #   options_json: free-form extras, e.g. activities separator " | "
        # ---------------------------------------------------------------- #
        """
        CREATE TABLE IF NOT EXISTS columns_config (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER,                    -- NULL = global default (FK added in Phase 5)
            column_name TEXT    NOT NULL,
            column_type TEXT    NOT NULL DEFAULT 'text',
            required    INTEGER NOT NULL DEFAULT 0 CHECK (required IN (0, 1)),
            source      TEXT    NOT NULL DEFAULT 'csv+daylio',
            derived     INTEGER NOT NULL DEFAULT 0 CHECK (derived IN (0, 1)),
            label       TEXT    NOT NULL DEFAULT '',
            sort_order  INTEGER NOT NULL DEFAULT 0,
            enabled     INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
            options_json TEXT   NOT NULL DEFAULT '{}',
            created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
            updated_at  TEXT    NOT NULL DEFAULT (datetime('now'))
        )
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_columns_config_global_name
            ON columns_config (column_name) WHERE user_id IS NULL
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_columns_config_user_name
            ON columns_config (user_id, column_name) WHERE user_id IS NOT NULL
        """,
        # ---------------------------------------------------------------- #
        # scales_config — ordered scales (text levels or numeric ranges).
        #   source      : "column" (CSV `scales` col) | "tag-group:<N>" (D2)
        #   tag_group   : parsed from source when it is a tag-group reference
        #   values_json : [{"label": "Calma", "rank": 1}, ...] (ranks unique)
        #   levels_count: number of levels (0 for number scales; min/max apply)
        # ---------------------------------------------------------------- #
        """
        CREATE TABLE IF NOT EXISTS scales_config (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id      INTEGER,                   -- NULL = global default (FK added in Phase 5)
            scale_name   TEXT    NOT NULL,          -- stable key, e.g. "stress"
            label        TEXT    NOT NULL DEFAULT '',-- display form, e.g. "Stress"
            scale_type   TEXT    NOT NULL DEFAULT 'text' CHECK (scale_type IN ('text', 'number')),
            source       TEXT    NOT NULL DEFAULT 'column',
            tag_group    INTEGER,                   -- NULL unless source = tag-group:<N>
            min_value    INTEGER,                   -- number scales only
            max_value    INTEGER,                   -- number scales only
            levels_count INTEGER NOT NULL DEFAULT 0,
            values_json  TEXT    NOT NULL DEFAULT '[]',
            sort_order   INTEGER NOT NULL DEFAULT 0,
            enabled      INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
            created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
            updated_at   TEXT    NOT NULL DEFAULT (datetime('now'))
        )
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_scales_config_global_name
            ON scales_config (scale_name) WHERE user_id IS NULL
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_scales_config_user_name
            ON scales_config (user_id, scale_name) WHERE user_id IS NOT NULL
        """,
        # ---------------------------------------------------------------- #
        # mood_map — D1 convention: mood_level 1 = best ... 5 = worst.
        # Global only in Fase 1; per-user mood labels stay in the YAML
        # `users.<name>.scales.mood` overrides until multi-user lands (Phase 5).
        # ---------------------------------------------------------------- #
        """
        CREATE TABLE IF NOT EXISTS mood_map (
            mood_level INTEGER PRIMARY KEY CHECK (mood_level BETWEEN 1 AND 5),
            label      TEXT    NOT NULL UNIQUE,
            updated_at TEXT    NOT NULL DEFAULT (datetime('now'))
        )
        """,
    )
    for statement in statements:
        conn.execute(statement)


#: Ordered migration steps: (name, callable). Append only — never reorder or
#: mutate released entries (PRAGMA user_version would skip them forever).
_MIGRATIONS: Sequence[tuple[str, Callable[[sqlite3.Connection], None]]] = (
    ("001_config_tables", _migration_001_config_tables),
)


def apply(conn: sqlite3.Connection, *, to_version: int | None = None) -> int:
    """Apply pending migrations to the connection (idempotent, transactional).

    Args:
        conn: SQLite connection.
        to_version: optional target schema version; defaults to all migrations.

    Returns:
        The schema version reached after applying.

    Raises:
        ValueError: if ``to_version`` is greater than the available migrations.
    """
    current = _schema_version(conn)
    target = len(_MIGRATIONS) if to_version is None else to_version
    if target > len(_MIGRATIONS):
        raise ValueError(
            f"Cannot migrate to schema version {target}: "
            f"only {len(_MIGRATIONS)} migration(s) exist"
        )
    if current >= target:
        log.debug("SQLite schema already at version %d", current)
        return current

    log.info("SQLite migrations: %d -> %d", current, target)
    for index in range(current, target):
        name, step = _MIGRATIONS[index]
        try:
            # Transactional: either the DDL *and* the version bump both land,
            # or everything rolls back and the migration is retried next boot.
            with conn:
                step(conn)
                conn.execute(f"PRAGMA user_version = {index + 1}")
        except Exception:
            log.error("Migration %s failed; schema stays at version %d", name, index)
            raise
        log.info("Applied migration %s (schema version %d)", name, index + 1)
    return target


def migrate(conn: sqlite3.Connection, *, to_version: int | None = None) -> int:
    """Bring the connection schema up to date (Task 1.1 public entry point).

    Thin, stable alias of :func:`apply` for app boot and tests:
    ``migrate(conn)`` is the name mandated by WORKPLAN §7 / Fase 1.
    """
    return apply(conn, to_version=to_version)


def _schema_version(conn: sqlite3.Connection) -> int:
    """Read the schema version from PRAGMA user_version (default 0)."""
    row = conn.execute("PRAGMA user_version").fetchone()
    return int(row[0]) if row else 0
