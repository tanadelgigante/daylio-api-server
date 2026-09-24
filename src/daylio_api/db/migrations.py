"""Versioned, idempotent SQLite migrations (Fase 1 + Fase 3).

=============================== MAINTENANCE NOTES ===============================
Module   : src/daylio_api/db/migrations.py
Scope    : Fase 1 (R1) — configuration tables (``columns_config``,
           ``scales_config``, ``mood_map``) and Fase 3 (R3/R5) — the daily
           record store (``users``, ``day_entries`` with UNIQUE(user, full_date)
           and the ``import_jobs`` audit table, WORKPLAN §5.3). Versioning uses
           ``PRAGMA user_version``: every step is idempotent (``CREATE TABLE
           IF NOT EXISTS`` / ``CREATE INDEX IF NOT EXISTS``) and bumps the
           version inside the same transaction, so a partially applied
           migration rolls back as a whole.

Contract : ``apply(conn, to_version=None) -> int`` (stable since Fase 0) and the
           ``migrate(conn)`` alias — both are safe to call on every boot and
           from tests. ``conn`` may be any sqlite3 connection (file or
           in-memory); DDL statements are executed one by one (NOT via
           ``executescript``, which would implicitly COMMIT and break the
           transactional version bump).

Extending: phases 4-7 append new ``(name, fn)`` tuples to ``_MIGRATIONS``.
           Never edit an already-released step: ``PRAGMA user_version`` would
           not re-run it on existing databases. Use a new step + ``ALTER TABLE``
           with an idempotent guard instead.

Note      : ``user_id`` columns are plain INTEGERs (no FK) until the ``users``
           table exists; NULL ``user_id`` = global/default row, uniqueness is
           enforced through partial unique indexes (SQLite treats NULLs as
           distinct in plain UNIQUE constraints).

Legacy    : v2 also ships a NON-BLOCKING "legacy lift" for the old single-file
           ``server.py`` database (WORKPLAN §7 Task 3.1): if a ``mood_entries``
           table created by the legacy service is found, its rows are folded
           into ``day_entries`` applying the same D3 rule ("most recent wins"
           per ``(user, full_date)``). The lift runs inside a try/except and
           never fails the version bump; it is a no-op on fresh databases.
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


def _migration_002_day_store(conn: sqlite3.Connection) -> None:
    """v2: users, day_entries and import_jobs (Fase 3 — R3/R5, WORKPLAN §5.3).

    Creates the exact §5.3 record schema: ``users`` (with UNIQUE username),
    ``day_entries`` keyed by ``UNIQUE(user, full_date)`` (upsert D3) and the
    ``import_jobs`` audit table. The old single-table ``mood_entries`` layout
    (legacy server.py) is lifted into ``day_entries`` when present, in a
    non-blocking way (see :func:`_maybe_migrate_legacy`).

    ``mood`` allows 0 as well as 1..5: the Fase 2 normalizer emits 0 for an
    unresolved label with a warning, so a row is never rejected for a
    misconfigured mood (D1) — 1..5 remain the valid Daylio range.
    """
    statements = (
        # ------------------------------------------------------------------ #
        # users — minimal structure for Phase 3/5 (auth/password is Phase 5).
        # is_admin/import_dir are stored here so Phase 5 needs no schema change.
        # ------------------------------------------------------------------ #
        """
        CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT    NOT NULL UNIQUE,
            password_hash TEXT    NOT NULL DEFAULT '',
            is_admin      INTEGER NOT NULL DEFAULT 0 CHECK (is_admin IN (0, 1)),
            import_dir    TEXT,
            created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
            updated_at    TEXT    NOT NULL DEFAULT (datetime('now'))
        )
        """,
        # ------------------------------------------------------------------ #
        # day_entries — ONE row per (user, full_date) (R5/D3).
        #   activities: JSON array of strings (D8.d)
        #   scales    : JSON object per §5.3, e.g.
        #               {"Stress": {"value": 3, "label": "Moderato"},
        #                "Scala salute": {"value": …, "label": …}}
        #   UNIQUE(user, full_date) drives the ON CONFLICT upsert.
        # ------------------------------------------------------------------ #
        """
        CREATE TABLE IF NOT EXISTS day_entries (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user        TEXT    NOT NULL REFERENCES users(username),
            full_date   TEXT    NOT NULL,
            time        TEXT    NOT NULL DEFAULT '',
            mood        INTEGER NOT NULL DEFAULT 0 CHECK (mood BETWEEN 0 AND 5),
            mood_label  TEXT    NOT NULL DEFAULT '',
            activities  TEXT    NOT NULL DEFAULT '[]',
            scales      TEXT    NOT NULL DEFAULT '{}',
            note_title  TEXT    NOT NULL DEFAULT '',
            note        TEXT    NOT NULL DEFAULT '',
            source      TEXT    NOT NULL DEFAULT 'csv',
            created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
            updated_at  TEXT    NOT NULL DEFAULT (datetime('now')),
            UNIQUE (user, full_date)
        )
        """,
        # ------------------------------------------------------------------ #
        # import_jobs — audit of every import run (WORKPLAN §5.3).
        #   detail: JSON array of per-row errors/warnings.
        #   source : "upload" | "folder" | "daily"
        #   status : "running" | "completed" | "failed"
        # ------------------------------------------------------------------ #
        """
        CREATE TABLE IF NOT EXISTS import_jobs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user        TEXT    NOT NULL,
            source      TEXT    NOT NULL DEFAULT 'upload',
            filename    TEXT    NOT NULL DEFAULT '',
            imported    INTEGER NOT NULL DEFAULT 0,
            updated     INTEGER NOT NULL DEFAULT 0,
            skipped     INTEGER NOT NULL DEFAULT 0,
            errors      INTEGER NOT NULL DEFAULT 0,
            detail      TEXT    NOT NULL DEFAULT '[]',
            started_at  TEXT    NOT NULL,
            finished_at TEXT,
            status      TEXT    NOT NULL DEFAULT 'pending'
        )
        """,
    )
    for statement in statements:
        conn.execute(statement)

    # Non-blocking lift of the legacy server.py schema (safe no-op normally).
    try:
        _maybe_migrate_legacy(conn)
    except Exception as exc:  # pragma: no cover - defensive, must never block
        log.warning("Legacy mood_entries migration skipped (%s); nothing lost", exc)


def _maybe_migrate_legacy(conn: sqlite3.Connection) -> None:
    """Fold a legacy ``mood_entries`` table into ``day_entries`` (D3).

    The pre-Fase-0 ``server.py`` created ``mood_entries(user, full_date, time,
    mood, activities, note_title, note)`` with ``UNIQUE(user, full_date,
    time)`` — i.e. one row per *timestamp*, violating R5. When such a table is
    detected the rows are folded into ``day_entries`` keeping the most recent
    ``time`` per ``(user, full_date)`` (same "vince la più recente" rule, D3).
    Users are created on the fly so the FK keeps working with
    ``PRAGMA foreign_keys = ON``. The source table is left untouched: the
    operator decides how/when to drop it.

    The current bundled ``data.db`` has no tables, so this is a no-op there.
    """
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'mood_entries'"
    ).fetchone()
    if exists is None:
        return
    # Keep the migration idempotent: the day_entries UNIQUE(user, full_date)
    # makes a re-run an upsert-free no-op, and users use INSERT OR IGNORE.
    rows = conn.execute(
        "SELECT DISTINCT user FROM mood_entries ORDER BY user"
    ).fetchall()
    for (username,) in rows:
        conn.execute(
            "INSERT OR IGNORE INTO users (username) VALUES (?)", (username,)
        )
    # D3: keep the most recent ``time`` per (user, full_date); ties -> higher
    # rowid (the later legacy import order).
    # Row layout: rowid, user, full_date, time, mood, activities, note_title, note.
    winners: dict[tuple[str, str], tuple] = {}
    for row in conn.execute(
        "SELECT rowid, user, full_date, time, mood, activities, note_title, note "
        "FROM mood_entries"
    ):
        key = (str(row[1]), str(row[2]))
        winner = winners.get(key)
        if winner is None or (str(row[3] or ""), row[0]) >= (str(winner[3] or ""), winner[0]):
            winners[key] = row
    for (username, full_date), row in winners.items():
        conn.execute(
            "INSERT OR IGNORE INTO day_entries "
            "(user, full_date, time, mood, mood_label, activities, scales, "
            " note_title, note, source) "
            "VALUES (?, ?, ?, ?, '', ?, '{}', ?, ?, 'legacy')",
            (
                username,
                full_date,
                str(row[3] or ""),
                int(row[4] or 0),
                str(row[5] or "") or "[]",
                str(row[6] or ""),
                str(row[7] or ""),
            ),
        )
    log.info("Legacy mood_entries folded into day_entries (%d days)", len(winners))


#: Ordered migration steps: (name, callable). Append only — never reorder or
#: mutate released entries (PRAGMA user_version would skip them forever).
_MIGRATIONS: Sequence[tuple[str, Callable[[sqlite3.Connection], None]]] = (
    ("001_config_tables", _migration_001_config_tables),
    ("002_day_store", _migration_002_day_store),
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
