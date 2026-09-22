"""SQLite store placeholder.

=============================== MAINTENANCE NOTES ===============================
Module   : src/daylio_api/db/sqlite_store.py
Scope    : Fase 0 placeholder (Task 0.2/0.4). Provides a minimal connectivity
           object with a configurable database path. Versioned, idempotent
           migrations live in ``db/migrations.py``; real schema (users,
           columns_config, scales_config, mood_map, day_entries, import_jobs,
           schema_version) will be applied in Phase 3.
================================================================================
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from daylio_api.logger import get_logger

log = get_logger("db.sqlite")


class SqliteStore:
    """Thin SQLite connection holder.

    Args:
        db_path: filesystem path of the SQLite database (default ``data.db``).
    """

    def __init__(self, db_path: str | Path = "data.db") -> None:
        self.db_path = Path(db_path)
        self._conn: sqlite3.Connection | None = None

    def connect(self) -> sqlite3.Connection:
        """Open (or reuse) a SQLite connection, creating parent dirs."""
        if self._conn is not None:
            return self._conn
        if self.db_path.parent != Path("."):
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        log.debug("SQLite connected: %s", self.db_path)
        return self._conn

    def close(self) -> None:
        """Close the underlying connection if open."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> SqliteStore:
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
