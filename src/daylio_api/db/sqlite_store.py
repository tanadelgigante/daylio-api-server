"""SQLite store: connection holder + configuration-table reads (Fase 1).

=============================== MAINTENANCE NOTES ===============================
Module   : src/daylio_api/db/sqlite_store.py
Scope    : Fase 0 (Task 0.2/0.4) connection holder, extended in Fase 1 with the
           ``migrate()`` bootstrap call and the read accessors for the
           configuration tables (columns_config, scales_config, mood_map) used
           by the Fase 1 read endpoints. Versioned, idempotent migrations live
           in ``db/migrations.py``; the record tables (day_entries with
           UNIQUE(user, full_date), import_jobs, users) are applied in Phases
           3/5 (WORKPLAN §7).

Writer  : schema seeding/syncing lives in ``config.seed_defaults`` /
           ``config.sync_schema`` (Task 1.2/1.4) — this module stays a reader
           for configuration data to keep the SQL in one place.
==================================================================================
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from daylio_api.logger import get_logger

log = get_logger("db.sqlite")


def _json_loads(raw: str | None, *, default: Any) -> Any:
    """Parse a JSON column, falling back to ``default`` on malformed data."""
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        log.warning("Ignoring malformed JSON in configuration column")
        return default


class SqliteStore:
    """Thin SQLite connection holder + configuration-table readers.

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

    def migrate(self) -> int:
        """Apply versioned, idempotent migrations and return the schema version.

        Safe to call on every boot and from tests (no-op when current).
        """
        from daylio_api.db.migrations import migrate as apply_migrations

        return apply_migrations(self.connect())

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

    # ------------------------------------------------------------------ #
    # Configuration-table readers (Fase 1, Task 1.3)
    # ------------------------------------------------------------------ #
    def get_columns(
        self, *, user_id: int | None = None, include_disabled: bool = False
    ) -> list[dict[str, Any]]:
        """Return the enabled ``columns_config`` rows as JSON-ready dicts.

        Args:
            user_id: filter for a specific user (None = global defaults only).
            include_disabled: also return ``enabled = 0`` rows.

        Returns:
            Ordered (by sort_order, id) column descriptors: name, label, type,
            required, derived, source, sort_order, enabled, options.
        """
        conn = self.connect()
        sql = "SELECT * FROM columns_config"
        params: tuple[Any, ...] = ()
        if user_id is None:
            sql += " WHERE user_id IS NULL"
        else:
            sql += " WHERE user_id = ?"
            params = (user_id,)
        sql += " ORDER BY sort_order, id"
        rows = conn.execute(sql, params).fetchall()

        columns: list[dict[str, Any]] = []
        for row in rows:
            enabled = bool(row["enabled"])
            if not include_disabled and not enabled:
                continue
            columns.append(
                {
                    "name": row["column_name"],
                    "label": row["label"],
                    "type": row["column_type"],
                    "required": bool(row["required"]),
                    "derived": bool(row["derived"]),
                    "source": row["source"],
                    "sort_order": row["sort_order"],
                    "enabled": enabled,
                    "options": _json_loads(row["options_json"], default={}),
                }
            )
        return columns

    def get_scales(
        self, *, user_id: int | None = None, include_disabled: bool = False
    ) -> list[dict[str, Any]]:
        """Return the enabled ``scales_config`` rows as JSON-ready dicts.

        Args:
            user_id: filter for a specific user (None = global defaults only).
            include_disabled: also return ``enabled = 0`` rows.

        Returns:
            Ordered (by sort_order, id) scale descriptors: name, label, type,
            source, tag_group, levels, min, max, values (list of label/rank),
            sort_order, enabled.
        """
        conn = self.connect()
        sql = "SELECT * FROM scales_config"
        params: tuple[Any, ...] = ()
        if user_id is None:
            sql += " WHERE user_id IS NULL"
        else:
            sql += " WHERE user_id = ?"
            params = (user_id,)
        sql += " ORDER BY sort_order, id"
        rows = conn.execute(sql, params).fetchall()

        scales: list[dict[str, Any]] = []
        for row in rows:
            enabled = bool(row["enabled"])
            if not include_disabled and not enabled:
                continue
            scales.append(
                {
                    "name": row["scale_name"],
                    "label": row["label"],
                    "type": row["scale_type"],
                    "source": row["source"],
                    "tag_group": row["tag_group"],
                    "levels": row["levels_count"],
                    "min": row["min_value"],
                    "max": row["max_value"],
                    "values": _json_loads(row["values_json"], default=[]),
                    "sort_order": row["sort_order"],
                    "enabled": enabled,
                }
            )
        return scales

    def get_mood_map(self) -> list[dict[str, Any]]:
        """Return the ``mood_map`` rows ordered by level (1 = best, D1).

        Returns:
            ``[{"level": 1, "label": "alla grande"}, ...]`` for levels 1..5.
        """
        conn = self.connect()
        rows = conn.execute("SELECT mood_level, label FROM mood_map ORDER BY mood_level").fetchall()
        return [{"level": row["mood_level"], "label": row["label"]} for row in rows]
