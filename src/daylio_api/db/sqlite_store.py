"""SQLite store: connection holder + configuration/record/user accessors.

=============================== MAINTENANCE NOTES ===============================
Module   : src/daylio_api/db/sqlite_store.py
Scope    : Fase 0 (Task 0.2/0.4) connection holder, extended in Fase 1 with the
           ``migrate()`` bootstrap call and the read accessors for the
           configuration tables (columns_config, scales_config, mood_map), and
           in Fase 3 (R3/R5, WORKPLAN §7) with the daily-record store:
           ``upsert_entry``/``get_entry``/``list_entries``/``count_entries``
           against ``day_entries`` (UNIQUE(user, full_date), D3) plus the
           minimal ``users`` helpers (structure only; real auth is Phase 5).

Record contract : ``upsert_entry(conn, user, entry)`` accepts a
           ``NormalizedEntry`` (importers.base) or the canonical dict returned
           by its ``as_dict()`` (WORKPLAN §5.3/§5.5 step 6). It persists:
           - activities as a JSON array string;
           - scales as the §5.3 JSON object
             ``{"Stress": {"value": 3, "label": "Moderato"}, ...}`` built from
             the normalizer's scales list + ``scala_salute``.

Transaction  :  store functions are statement-oriented and do NOT commit; the
           coordinator (``import_flow.import_data``) owns the transaction and
           commits once at the end. The ``connect()`` method also applies the
           versioned migrations so a store is always schema-ready on open.
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
    """Thin SQLite connection holder + configuration/record accessors.

    Args:
        db_path: filesystem path of the SQLite database. Defaults to
            ``data/daylio.db`` (WORKPLAN §6); ``DB_PATH``/``DATA_DIR`` env
            overrides are applied by ``config.load`` on the storage section.
    """

    def __init__(self, db_path: str | Path = "data/daylio.db") -> None:
        self.db_path = Path(db_path)
        self._conn: sqlite3.Connection | None = None

    def connect(self) -> sqlite3.Connection:
        """Open (or reuse) a SQLite connection, creating parent dirs.

        Also applies the versioned, idempotent migrations so a freshly opened
        store is always schema-ready (Fase 3: users/day_entries/import_jobs).
        """
        if self._conn is not None:
            return self._conn
        if self.db_path.parent != Path("."):
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        log.debug("SQLite connected: %s", self.db_path)
        from daylio_api.db.migrations import migrate as apply_migrations

        apply_migrations(self._conn)
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


# --------------------------------------------------------------------------- #
# Daily records (Fase 3 — R5/D3): one row per (user, full_date)
# --------------------------------------------------------------------------- #
#: Columns updated by the ON CONFLICT upsert (every mutable field, D3).
_UPSERT_COLUMNS = (
    "time",
    "mood",
    "mood_label",
    "activities",
    "scales",
    "note_title",
    "note",
    "source",
)

_UPSERT_SQL = f"""
INSERT INTO day_entries (user, full_date, {", ".join(_UPSERT_COLUMNS)})
VALUES (?, ?, {", ".join("?" for _ in _UPSERT_COLUMNS)})
ON CONFLICT(user, full_date) DO UPDATE SET
    {", ".join(f"{col} = excluded.{col}" for col in _UPSERT_COLUMNS)},
    updated_at = datetime('now')
"""


def _entry_dict(entry: Any) -> dict[str, Any]:
    """Normalize a ``NormalizedEntry`` (or canonical dict) to a plain dict."""
    if hasattr(entry, "as_dict"):
        return entry.as_dict()
    return dict(entry)


def _scales_object(entry_dict: dict[str, Any]) -> dict[str, Any]:
    """Build the §5.3 ``scales`` JSON object from the canonical record.

    The normalizer exposes scales as a list of ``{scale_name, label, rank,
    levels_count, scale_key}`` plus a separate ``scala_salute`` field; the store
    persists the object form ``{display_name: {"value": rank, "label": label}}``
    required by WORKPLAN §5.3.
    """
    scales: dict[str, Any] = {}
    for scale in entry_dict.get("scales") or []:
        if not isinstance(scale, dict):
            continue
        name = str(scale.get("scale_name") or "")
        if name:
            scales[name] = {
                "value": int(scale.get("rank") or 0),
                "label": str(scale.get("label") or ""),
            }
    salute = entry_dict.get("scala_salute")
    if isinstance(salute, dict) and salute.get("label"):
        scales.setdefault(
            "Scala salute",
            {"value": int(salute.get("rank") or 0), "label": str(salute["label"])},
        )
    return scales


def _row_to_record(row: sqlite3.Row) -> dict[str, Any]:
    """Convert a ``day_entries`` row to a JSON-ready dict (JSON columns parsed)."""
    record = dict(row)
    record["activities"] = _json_loads(record.get("activities"), default=[])
    record["scales"] = _json_loads(record.get("scales"), default={})
    return record


def upsert_entry(conn: sqlite3.Connection, user: str, entry: Any) -> str:
    """Insert or fully update one daily record (D3 upsert, R5).

    Uses ``INSERT ... ON CONFLICT(user, full_date) DO UPDATE`` and returns
    ``"imported"`` when a new row was created or ``"updated"`` when an existing
    day was replaced. All mutable fields are overwritten on conflict.

    Args:
        conn: SQLite connection (caller owns the transaction/commit).
        user: the record owner (placeholder ``"default"`` until Phase 5).
        entry: a ``NormalizedEntry`` or the canonical dict from ``as_dict()``.

    Returns:
        ``"imported"`` if a row was inserted, ``"updated"`` otherwise.
    """
    data = _entry_dict(entry)
    full_date = str(data.get("full_date") or "").strip()
    if not full_date:
        raise ValueError("upsert_entry requires a non-empty full_date")
    data.setdefault("source", "csv")

    existing = conn.execute(
        "SELECT 1 FROM day_entries WHERE user = ? AND full_date = ?",
        (user, full_date),
    ).fetchone()

    params = (
        user,
        full_date,
        str(data.get("time") or ""),
        int(data.get("mood") or 0),
        str(data.get("mood_label") or ""),
        json.dumps(data.get("activities") or [], ensure_ascii=False),
        json.dumps(_scales_object(data), ensure_ascii=False),
        str(data.get("note_title") or ""),
        str(data.get("note") or ""),
        str(data.get("source") or "csv"),
    )
    conn.execute(_UPSERT_SQL, params)
    return "updated" if existing is not None else "imported"


def get_entry(conn: sqlite3.Connection, user: str, full_date: str) -> dict[str, Any] | None:
    """Return the daily record for ``(user, full_date)`` as a parsed dict."""
    row = conn.execute(
        "SELECT * FROM day_entries WHERE user = ? AND full_date = ?",
        (user, full_date),
    ).fetchone()
    return _row_to_record(row) if row is not None else None


def list_entries(
    conn: sqlite3.Connection,
    user: str,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Return the user's daily records ordered by ``full_date DESC``.

    Args:
        conn: SQLite connection.
        user: record owner.
        start_date: optional inclusive lower bound ``YYYY-MM-DD``.
        end_date: optional inclusive upper bound ``YYYY-MM-DD``.
        limit: optional row cap (applied after ordering).
    """
    sql = "SELECT * FROM day_entries WHERE user = ?"
    params: list[Any] = [user]
    if start_date:
        sql += " AND full_date >= ?"
        params.append(start_date)
    if end_date:
        sql += " AND full_date <= ?"
        params.append(end_date)
    sql += " ORDER BY full_date DESC"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
    rows = conn.execute(sql, params).fetchall()
    return [_row_to_record(row) for row in rows]


def count_entries(conn: sqlite3.Connection, user: str) -> int:
    """Return the number of daily records for ``user`` (== number of days)."""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM day_entries WHERE user = ?", (user,)
    ).fetchone()
    return int(row["n"]) if row is not None else 0


# --------------------------------------------------------------------------- #
# Minimal users helpers (structure only — real auth lands in Phase 5)
# --------------------------------------------------------------------------- #
def get_or_create_user(
    conn: sqlite3.Connection,
    username: str,
    *,
    is_admin: bool = False,
    import_dir: str | None = None,
) -> int:
    """Return the ``users.id`` for ``username``, creating the row if needed.

    Phase 3 keeps this structure-only: there is no password hashing yet. The
    created user is inert until Phase 5 adds auth.
    """
    row = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
    if row is not None:
        return int(row["id"])
    cursor = conn.execute(
        "INSERT INTO users (username, is_admin, import_dir) VALUES (?, ?, ?)",
        (username, int(bool(is_admin)), import_dir),
    )
    return int(cursor.lastrowid)


def set_import_dir(conn: sqlite3.Connection, username: str, import_dir: str) -> None:
    """Update ``users.import_dir`` for ``username`` (Phase 5 folder import)."""
    conn.execute(
        "UPDATE users SET import_dir = ?, updated_at = datetime('now') "
        "WHERE username = ?",
        (import_dir, username),
    )


def list_users(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return all ``users`` rows (Phase 5 read endpoints / resync iteration)."""
    rows = conn.execute(
        "SELECT id, username, is_admin, import_dir, created_at, updated_at "
        "FROM users ORDER BY username"
    ).fetchall()
    return [dict(row) for row in rows]
