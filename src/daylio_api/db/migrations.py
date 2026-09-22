"""Versioned, idempotent SQLite migrations placeholder.

=============================== MAINTENANCE NOTES ===============================
Module   : src/daylio_api/db/migrations.py
Scope    : Fase 0 placeholder. The real schema (``users``, ``columns_config``,
           ``scales_config``, ``mood_map``, ``day_entries`` with
           ``UNIQUE(user, full_date)``, ``import_jobs``, ``schema_version``) is
           defined in WORKPLAN §5.3 and implemented in Phase 3. The module
           already exposes the stable ``apply`` contract used by
           ``SqliteStore`` so later phases only need to append migrations.
================================================================================
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence

from daylio_api.logger import get_logger

log = get_logger("db.migrations")

#: Ordered list of migration steps. In Phase 3 each entry becomes a callable
#: ``(conn) -> None`` executing idempotent CREATE/ALTER statements.
_MIGRATIONS: Sequence[tuple[str, str]] = []


def apply(conn: sqlite3.Connection, *, to_version: int | None = None) -> int:
    """Apply pending migrations to the connection (idempotent, no-op for now).

    Args:
        conn: SQLite connection.
        to_version: optional target schema version; defaults to all migrations.

    Returns:
        The schema version reached after applying.
    """
    current = _schema_version(conn)
    target = to_version if to_version is not None else len(_MIGRATIONS)
    if current < target:
        log.info("SQLite migrations: %d -> %d (pending executable in Phase 3)", current, target)
        # Phase 3: loop over _MIGRATIONS[current:target] inside a transaction.
    else:
        log.debug("SQLite schema already at version %d", current)
    return current


def _schema_version(conn: sqlite3.Connection) -> int:
    """Read the schema version from PRAGMA user_version (default 0)."""
    row = conn.execute("PRAGMA user_version").fetchone()
    return int(row[0]) if row else 0
