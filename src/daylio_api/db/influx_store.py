"""InfluxDB 2.x store: daily-point write, delete-and-write overwrite, resync.

=============================== MAINTENANCE NOTES ===============================
Module   : src/daylio_api/db/influx_store.py
Scope    : Fase 3 (R3/D4, WORKPLAN §5.4/§7 Task 3.2) — writes ONE point per
           (user, date) into InfluxDB using the official ``influxdb-client``
           and implements the **delete-and-write** overwrite strategy: every
           write deletes the whole day series ``(user, date)`` first, so fields
           that are no longer present on an upsert cannot leak into the
           analytics (risk R8 / §5.4).

Point contract (§5.4):
  - measurement : ``daylio_entry`` (configurable, ``storage.influx_measurement``)
  - tags        : ``user``, ``date`` (YYYY-MM-DD), ``mood_label``,
                  ``scala_salute_label`` (only when present)
  - numeric fields: ``mood``, ``mood_num``, ``activities_count``,
                  ``scale_<key>_value`` per configured scale, ``is_favorite``,
                  ``notes_present``
  - string fields : ``activities`` (JSON array), ``scales`` (JSON), ``note_title``,
                  ``note``, ``source``
  - timestamp   : fixed at the START of the day (``YYYY-MM-DDT00:00:00Z``) so the
                  daily point is always overwritten by a later upsert.

  Injectable client : ``InfluxStore(config, client=...)`` accepts any object
   implementing the minimal duck-typed contract used by this module:
      ``ping() -> bool``
      ``write(bucket: str, record: dict) -> None``
      ``delete(start: str, stop: str, predicate: str, bucket: str, org: str) -> None``
      ``query(flux: str) -> list``
      ``close() -> None``
  The real client is wrapped in :class:`_InfluxClientAdapter`; tests inject a
  fake (``tests/fakes.py``) so no server is required. Importing this module
  never connects (R8 tolerance: a missing/unreachable server is logged and the
  caller decides, it is never fatal for the SQLite source of truth).

Analytics (Fase 6, R4): :func:`pivot_daily_points` + :meth:`InfluxStore.
  read_daily_points` turn the raw Flux rows into ONE dict per (user, date) —
  the single read abstraction the dashboard endpoints are built on, so the
  aggregations never touch SQLite (D4/R4) and stay testable with the fake.
================================================================================
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date as date_cls
from datetime import timedelta
from typing import Any

from influxdb_client import InfluxDBClient, Point

from daylio_api.config import scale_label
from daylio_api.logger import get_logger

log = get_logger("db.influx")

#: Default daily timestamp for every point (start of day, D3/§5.4).
_DAY_TIMESTAMP = "T00:00:00Z"
#: Fallback query window used when no ``start`` date is given.
_DEFAULT_RANGE = "-1y"


@dataclass
class InfluxConfig:
    """Connection parameters resolved from application config/env.

    ``url``/``token`` may hold the ``env:VAR`` placeholder that ``config.load``
    keeps when the environment variable is unset: :meth:`InfluxStore.
    is_configured` treats those as "not configured", so a service without a
    real Influx endpoint never tries to connect.
    """

    url: str
    token: str = ""
    org: str = "tanadelgigante"
    bucket: str = "daylio"
    measurement: str = "daylio_entry"


# --------------------------------------------------------------------------- #
# Resolution helpers
# --------------------------------------------------------------------------- #
def resolve_influx_config(cfg: Any) -> InfluxConfig | None:
    """Extract an :class:`InfluxConfig` from an ``AppConfig``/dict, or None.

    Returns ``None`` (no Influx available) when the url or the token is
    missing or still an unresolved ``env:VAR`` placeholder, or when there is
    no ``storage`` section at all.
    """
    storage = getattr(cfg, "storage", None)
    if not isinstance(storage, dict):
        return None
    url = str(storage.get("influx_url") or "").strip()
    token = str(storage.get("influx_token") or "").strip()
    if not url or url.startswith("env:") or not token or token.startswith("env:"):
        return None
    return InfluxConfig(
        url=url,
        token=token,
        org=str(storage.get("influx_org") or "tanadelgigante"),
        bucket=str(storage.get("influx_bucket") or "daylio"),
        measurement=str(storage.get("influx_measurement") or "daylio_entry"),
    )


# --------------------------------------------------------------------------- #
# Daily point construction (§5.4)
# --------------------------------------------------------------------------- #
def _default_scale_key_map() -> dict[str, str]:
    """Display-name (casefolded) -> config key for the well-known scales."""
    return {"stress": "stress", "scala salute": "scala_salute"}


def build_scale_key_map(cfg: Any = None) -> dict[str, str]:
    """Map scale display labels (casefolded) to config keys.

    When ``cfg`` (an AppConfig) is provided every configured scale is derived
    from its ``label``/key; the well-known stress/scala_salute entries are
    always present as a fallback.
    """
    key_map = dict(_default_scale_key_map())
    scales = getattr(cfg, "scales", None)
    if isinstance(scales, dict):
        for key, definition in scales.items():
            if isinstance(definition, dict):
                display = str(definition.get("label") or scale_label(str(key)))
                key_map[display.strip().casefold()] = str(key)
    return key_map


def scale_key_for_display(display: Any, scale_keys: dict[str, str] | None = None) -> str | None:
    """Resolve a scale display label to its config key (best-effort).

    Falls back to a sanitized display name when the mapping has no entry, so a
    resync of SQLite rows never loses a scale from the analytics even when the
    config changed (documented tolerance, §5.4).

    PUBLIC on purpose: the dashboard's ``scales-trend`` ``?scale=`` validation
    must map a custom scale label to the SAME key the writer used, so the read
    and the write side can never disagree on a scale name.
    """
    key_map = scale_keys if scale_keys is not None else _default_scale_key_map()
    key = key_map.get(str(display).strip().casefold())
    if key:
        return str(key)
    sanitized = re.sub(r"[^a-z0-9_]+", "_", str(display).strip().lower()).strip("_")
    return sanitized or None


#: Backwards-compatible alias (the helper used to be private).
_scale_key_for_display = scale_key_for_display


def _normalize_record_for_influx(record: Any) -> dict[str, Any]:
    """Accept a ``NormalizedEntry`` or a canonical dict and return a dict.

    The ``is_favorite`` flag is only carried by the .daylio source (in
    ``NormalizedEntry.raw``); CSV rows and SQLite rows default it to 0.
    """
    if hasattr(record, "as_dict"):
        data = record.as_dict()
        raw = getattr(record, "raw", None) or {}
        data["is_favorite"] = int(bool(raw.get("is_favorite", False)))
        return data
    return dict(record)


def _extract_scales_fields(
    scales: Any, scale_keys: dict[str, str] | None
) -> tuple[dict[str, int], dict[str, Any] | None]:
    """Turn the scales payload into ``{scale_<key>_value: rank}`` + salute.

    Accepts both the canonical list form (``{scale_name, label, rank,
    scale_key}``) used by the import pipeline and the §5.3 object form stored in
    SQLite (``{"Stress": {"value": 3, "label": "Moderato"}}``).
    """
    fields: dict[str, int] = {}
    salute: dict[str, Any] | None = None
    if isinstance(scales, dict):
        for display, entry in scales.items():
            key = _scale_key_for_display(display, scale_keys)
            value = int((entry or {}).get("value", 0)) if isinstance(entry, dict) else 0
            if key == "scala_salute":
                salute = {"label": str((entry or {}).get("label", "")), "rank": value}
            elif key:
                fields[f"scale_{key}_value"] = value
    elif isinstance(scales, list):
        for scale in scales:
            if not isinstance(scale, dict):
                continue
            key = str(scale.get("scale_key") or "")
            rank = int(scale.get("rank") or 0)
            if key == "scala_salute":
                salute = {"label": str(scale.get("label", "")), "rank": rank}
            elif key:
                fields[f"scale_{key}_value"] = rank
    return fields, salute


def build_daily_point(
    user: str,
    record: Any,
    measurement: str = "daylio_entry",
    scale_keys: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build the §5.4 daily Influx point as a portable dict.

    Args:
        user: record owner (tag ``user``).
        record: a ``NormalizedEntry``, its canonical ``as_dict()`` output or a
            SQLite ``day_entries`` row (parsed by ``sqlite_store``).
        measurement: point measurement (configurable).
        scale_keys: optional display->key map (see :func:`build_scale_key_map`).

    Returns:
        ``{"measurement", "tags", "fields", "time"}`` where ``time`` is the
        fixed start-of-day timestamp ``YYYY-MM-DDT00:00:00Z`` (D3/§5.4).
    """
    data = _normalize_record_for_influx(record)
    full_date = str(data.get("full_date") or "").strip()
    if not full_date:
        raise ValueError("cannot build an Influx point without 'full_date' (YYYY-MM-DD)")

    mood = int(data.get("mood") or 0)
    tags: dict[str, str] = {"user": str(user), "date": full_date}
    mood_label = str(data.get("mood_label") or "")
    if mood_label:
        tags["mood_label"] = mood_label

    activities = data.get("activities") or []
    if isinstance(activities, str):
        activities = json.loads(activities) if activities.strip() else []
    activities = [str(activity) for activity in activities]

    scales: Any = data.get("scales") or []
    if isinstance(scales, str):
        scales = json.loads(scales) if scales.strip() else {}
    scale_fields, salute = _extract_scales_fields(scales, scale_keys)

    explicit_salute = data.get("scala_salute")
    if isinstance(explicit_salute, str) and explicit_salute.strip():
        try:
            explicit_salute = json.loads(explicit_salute)
        except ValueError:
            explicit_salute = None
    if isinstance(explicit_salute, dict) and explicit_salute.get("label"):
        salute = {
            "label": str(explicit_salute["label"]),
            "rank": int(explicit_salute.get("rank") or 0),
        }
    if salute and salute.get("label"):
        tags["scala_salute_label"] = str(salute["label"])
        scale_fields["scale_scala_salute_value"] = int(salute.get("rank") or 0)

    fields: dict[str, Any] = {
        "mood": mood,
        "mood_num": mood,
        "activities_count": len(activities),
        "is_favorite": int(bool(data.get("is_favorite", False))),
        "notes_present": int(bool(data.get("note") or data.get("note_title"))),
        "activities": json.dumps(activities, ensure_ascii=False),
        "scales": json.dumps(scales, ensure_ascii=False),
        "note_title": str(data.get("note_title") or ""),
        "note": str(data.get("note") or ""),
        "source": str(data.get("source") or ""),
    }
    fields.update(scale_fields)

    return {
        "measurement": measurement,
        "tags": tags,
        "fields": fields,
        "time": f"{full_date}{_DAY_TIMESTAMP}",
    }


def _next_day_iso(full_date: str) -> str:
    """Return ``YYYY-MM-DD`` of the day after ``full_date``."""
    try:
        parsed = date_cls.fromisoformat(full_date)
    except ValueError:
        return full_date
    return (parsed + timedelta(days=1)).isoformat()


def _normalize_query_rows(rows: Any) -> list[dict[str, Any]]:
    """Best-effort normalization of Influx query rows into point-style dicts.

    The real ``influxdb-client`` returns ``FluxTable``/``FluxRecord`` objects
    (one record per field); the injected fake returns plain point dicts. Both
    are flattened here; :func:`pivot_daily_points` then regroups the field rows
    into one point per (user, date) for the analytics endpoints (Fase 6).
    """
    normalized: list[dict[str, Any]] = []
    for row in rows or []:
        if isinstance(row, dict):
            normalized.append(dict(row))
        elif isinstance(getattr(row, "values", None), dict):
            normalized.append(dict(row.values))
        elif getattr(row, "records", None):
            # FluxTable: iterate the records it holds.
            for record in row.records:
                values = getattr(record, "values", None)
                if isinstance(values, dict):
                    normalized.append(dict(values))
    return normalized


# --------------------------------------------------------------------------- #
# Analytics pivot (Fase 6, R4/D4) — one record per (user, date)
# --------------------------------------------------------------------------- #
#: Flux bookkeeping keys: they are not tags and never carry record data.
_FLUX_META_KEYS = frozenset(
    {"_measurement", "_field", "_value", "_time", "_start", "_stop", "result", "table"}
)


def _new_pivot_entry(user: str, date: str, time_value: Any) -> dict[str, Any]:
    """Create the empty pivot accumulator for one (user, date) pair."""
    return {
        "user": user,
        "date": date,
        "time": time_value,
        "tags": {},
        "fields": {},
    }


def pivot_daily_points(rows: Any) -> list[dict[str, Any]]:
    """Regroup raw Flux rows into one analytics record per ``(user, date)``.

    Two input shapes are supported, both normalized by
    :func:`_normalize_query_rows`:

    - **real client**: one FluxRecord per FIELD, i.e. dicts shaped like
      ``{"_time": ..., "_field": "mood", "_value": 2, "user": "alice",
      "date": "2026-01-05", ...}``. They are re-grouped by ``(user, date)``:
      non-underscore keys become tags, ``_field``/``_value`` become fields.
    - **injected fake** (and any client returning whole points): dicts shaped
      like the §5.4 point ``{"measurement", "tags", "fields", "time"}``.

    Returns:
        A list of ``{"user", "date", "time", "tags", "fields"}`` records sorted
        by ``(date, user)``, one per day (R5/D3: a single point per day).
    """
    points: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        tags_in = row.get("tags")
        fields_in = row.get("fields")
        if isinstance(tags_in, dict) and isinstance(fields_in, dict):
            # Whole point (fake client): merge its tags/fields as they are.
            user = str(tags_in.get("user") or "")
            date = str(tags_in.get("date") or str(row.get("time") or "")[:10])
            entry = points.setdefault(
                (user, date), _new_pivot_entry(user, date, row.get("time"))
            )
            entry["tags"].update(tags_in)
            entry["fields"].update(fields_in)
            if row.get("time"):
                entry["time"] = row["time"]
            continue
        # FluxRecord shape: one field per row, tags as plain keys.
        time_value = str(row.get("_time") or "")
        user = str(row.get("user") or "")
        date = str(row.get("date") or time_value[:10])
        if not date:
            continue
        entry = points.setdefault((user, date), _new_pivot_entry(user, date, time_value))
        for key, value in row.items():
            if key in _FLUX_META_KEYS or key.startswith("_"):
                continue
            entry["tags"][key] = value
        field_name = str(row.get("_field") or "")
        if field_name:
            entry["fields"][field_name] = row.get("_value")
    return sorted(points.values(), key=lambda point: (point["date"], point["user"]))


def date_in_range(value: str, start: str | None = None, end: str | None = None) -> bool:
    """Inclusive ``YYYY-MM-DD`` range test on ISO strings (lexicographic order).

    The Influx query already bounds the query window, but the range is applied
    here as well so an injected client that ignores the Flux text (the test
    fake) honours the very same filters.
    """
    if not value:
        return False
    if start and value < start:
        return False
    if end and value > end:
        return False
    return True


# --------------------------------------------------------------------------- #
# Real client adapter
# --------------------------------------------------------------------------- #
class _InfluxClientAdapter:
    """Minimal adapter over ``influxdb-client`` exposing the store's needs.

    This is the exact duck-typed contract a test fake must implement
    (see the module MAINTENANCE NOTES).
    """

    def __init__(self, client: InfluxDBClient) -> None:
        self._client = client

    def ping(self, timeout: float = 2.0) -> bool:
        # influxdb-client's ping() has no timeout parameter on all versions;
        # requests is configured with a global timeout by configuration already.
        return bool(self._client.ping())

    def write(self, bucket: str, record: dict[str, Any]) -> None:
        org = getattr(self._client, "org", None)
        if org is None and hasattr(self._client, "_org"):
            org = self._client._org
        write_api = self._client.write_api()
        try:
            if isinstance(record, dict):
                try:
                    point = Point.from_dict(record)
                    line = point.to_line_protocol()
                    if org:
                        write_api.write(bucket=bucket, org=org, record=line)
                    else:
                        write_api.write(bucket=bucket, record=line)
                    return
                except Exception:
                    pass
            elif isinstance(record, Point):
                try:
                    line = record.to_line_protocol()
                    if org:
                        write_api.write(bucket=bucket, org=org, record=line)
                    else:
                        write_api.write(bucket=bucket, record=line)
                    return
                except Exception:
                    pass
            elif hasattr(record, "to_line_protocol"):
                try:
                    line = record.to_line_protocol()
                    if org:
                        write_api.write(bucket=bucket, org=org, record=line)
                    else:
                        write_api.write(bucket=bucket, record=line)
                    return
                except Exception:
                    pass
        except Exception:
            pass
        try:
            if org:
                write_api.write(bucket=bucket, org=org, record=record)
            else:
                write_api.write(bucket=bucket, record=record)
        except TypeError:
            write_api.write(bucket=bucket, record=record)

    def delete(
        self, start: str, stop: str, predicate: str, bucket: str, org: str
    ) -> None:
        self._client.delete_api().delete(start, stop, predicate, bucket=bucket, org=org)

    def query(self, flux: str) -> Any:
        return self._client.query_api().query(flux)

    def close(self) -> None:
        self._client.close()


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #
class InfluxStore:
    """InfluxDB write/delete/read wrapper with an injectable client.

    Args:
        config: an :class:`InfluxConfig` or a dict of its fields. Defaults are
            resolved (org ``tanadelgigante``/bucket ``daylio``, D18) when
            omitted.
        client: optional injected client implementing the module duck-typed
            contract (e.g. ``tests.fakes.FakeInfluxClient``); when None the
            real ``influxdb-client`` is lazily created. Importing/constructing
            the store never connects.
    """

    def __init__(
        self,
        config: InfluxConfig | dict[str, Any] | None = None,
        client: Any = None,
    ) -> None:
        self.config = config if isinstance(config, InfluxConfig) else InfluxConfig(**(config or {}))
        self._injected_client = client
        self._adapter: _InfluxClientAdapter | None = None
        self._scale_keys: dict[str, str] | None = None

    # --- client lifecycle --------------------------------------------------- #
    def client(self) -> Any:
        """Return the active client (injected fake or real client adapter).

        The real client is only created on first call AND when the store is
        configured (``is_configured``); callers use :func:`influx_available`
        before touching the wire.
        """
        if self._injected_client is not None:
            return self._injected_client
        if self._adapter is None:
            real = InfluxDBClient(
                url=self.config.url,
                token=self.config.token,
                org=self.config.org,
                timeout=5_000,
            )
            self._adapter = _InfluxClientAdapter(real)
            log.debug("InfluxDB client adapter created for url=%s", self.config.url)
        return self._adapter

    def is_configured(self) -> bool:
        """True when url+token are set and are not ``env:`` placeholders."""
        url = (self.config.url or "").strip()
        token = (self.config.token or "").strip()
        if not url or url.startswith("env:") or not token or token.startswith("env:"):
            return False
        return True

    def ping(self, timeout: float = 2.0) -> bool:
        """Ping the configured InfluxDB (or the injected fake)."""
        return bool(self.client().ping(timeout=timeout))

    def close(self) -> None:
        """Close the underlying client (no-op for injected fakes)."""
        if self._adapter is not None:
            self._adapter.close()
            self._adapter = None
        elif self._injected_client is not None and hasattr(self._injected_client, "close"):
            self._injected_client.close()

    # --- write/delete (delete-and-write, §5.4) ------------------------------ #
    def delete_day(self, user: str, full_date: str, measurement: str | None = None) -> None:
        """Delete every field of the daily series ``(user, date)``.

        The deletion window covers the whole day ``[date, date+1)`` with the
        predicate ``_measurement="..." AND user="..."`` — this is what makes
        the subsequent write a full overwrite (risk R8).
        """
        measurement = measurement or self.config.measurement
        start = f"{full_date}{_DAY_TIMESTAMP}"
        stop = f"{_next_day_iso(full_date)}{_DAY_TIMESTAMP}"
        escaped_user = str(user).replace(chr(34), chr(92) + chr(34))
        predicate = f'_measurement="{measurement}" AND user="{escaped_user}"'
        self.client().delete(
            start=start,
            stop=stop,
            predicate=predicate,
            bucket=self.config.bucket,
            org=self.config.org,
        )
        log.debug("Influx delete-day %s user=%s measurement=%s", full_date, user, measurement)

    def write_entry(
        self, user: str, entry: Any, measurement: str | None = None
    ) -> None:
        """Upsert one daily point for ``(user, date)`` (delete-and-write).

        The point is written at the fixed start-of-day timestamp, so even
        without the delete the TSDB would keep only the last point of the day;
        the explicit delete additionally removes fields that are no longer
        present in the record (R8/§5.4).
        """
        measurement = measurement or self.config.measurement
        point = build_daily_point(user, entry, measurement, self._scale_keys)
        self.delete_day(user, point["time"][:10], measurement=measurement)
        self.client().write(bucket=self.config.bucket, record=point)
        log.debug(
            "Influx write day=%s user=%s fields=%s",
            point["time"][:10],
            user,
            sorted(k for k in point["fields"] if not k.startswith("activities")),
        )

    # --- reads (base analytics, refined in Phase 4/6) ----------------------- #
    def read_entries(
        self, user: str, start: str | None = None, end: str | None = None
    ) -> list[dict[str, Any]]:
        """Read the user's daily points from the configured measurement.

        Returns normalized rows (see :func:`_normalize_query_rows`): one dict
        per Flux row for the real client, point dicts for an injected fake.
        The analytics pivot/serialization is deliberately deferred to
        Phase 4/6 (WORKPLAN §7).
        """
        measurement = self.config.measurement
        start_expr = f"{start}{_DAY_TIMESTAMP}" if start else _DEFAULT_RANGE
        stop_expr = f"{_next_day_iso(end)}{_DAY_TIMESTAMP}" if end else "now()"
        flux = (
            f'from(bucket: "{self.config.bucket}") '
            f"|> range(start: {start_expr}, stop: {stop_expr}) "
            f'|> filter(fn: (r) => r._measurement == "{measurement}") '
            f'|> filter(fn: (r) => r.user == "{user}")'
        )
        rows = self.client().query(flux)
        return _normalize_query_rows(rows)

    def read_daily_points(
        self, user: str, start: str | None = None, end: str | None = None
    ) -> list[dict[str, Any]]:
        """Read the user's daily points, pivoted to one record per day (Fase 6).

        This is THE read abstraction used by the dashboard analytics endpoints
        (D4/R4: they never fall back to SQLite). It is ``read_entries`` +
        :func:`pivot_daily_points` + the inclusive date filter, so the real
        Flux query and an injected fake honour the same bounds.

        Args:
            user: username (the point ``user`` tag).
            start: optional inclusive lower bound ``YYYY-MM-DD``.
            end: optional inclusive upper bound ``YYYY-MM-DD``.

        Returns:
            ``[{"user", "date", "time", "tags", "fields"}, ...]`` sorted by date
            ascending; empty when the user has no points in the window.
        """
        points = pivot_daily_points(self.read_entries(user, start, end))
        return [
            point
            for point in points
            if point["user"] == user and date_in_range(point["date"], start, end)
        ]

    def set_scale_keys(self, scale_keys: dict[str, str] | None) -> None:
        """Register the display->key map used when building points (resync)."""
        self._scale_keys = scale_keys

    # --- resync from SQLite (D4) ------------------------------------------- #
    def resync_from_sqlite(
        self, conn: Any, user: str | None = None, scale_keys: dict[str, str] | None = None
    ) -> dict[str, Any]:
        """Rewrite every day from SQLite into Influx (delete+write, coherent).

        Args:
            conn: SQLite connection (source of truth, D4).
            user: optional username; when None every user with records is
                resynchronized.
            scale_keys: optional display->key map (see :func:`build_scale_key_map`).

        Returns:
            Stats dict ``{"users": n, "days": n, "influx_errors": n,
            "detail": [...]}``. SQLite data is never modified here.
        """
        if scale_keys is not None:
            self._scale_keys = scale_keys
        from daylio_api.db.sqlite_store import list_entries

        if user:
            usernames = [user]
        else:
            rows = conn.execute(
                "SELECT DISTINCT user FROM day_entries ORDER BY user"
            ).fetchall()
            usernames = [str(row[0]) for row in rows]
        stats: dict[str, Any] = {
            "users": len(usernames),
            "days": 0,
            "influx_errors": 0,
            "detail": [],
        }
        for username in usernames:
            for record in list_entries(conn, username):
                try:
                    self.write_entry(username, record, measurement=self.config.measurement)
                    stats["days"] += 1
                except Exception as exc:  # noqa: BLE001 — per-day tolerance, R8
                    stats["influx_errors"] += 1
                    stats["detail"].append(
                        {
                            "level": "error",
                            "user": username,
                            "day": record.get("full_date"),
                            "message": str(exc),
                        }
                    )
                    log.error(
                        "Influx resync failed for %s %s: %s",
                        username,
                        record.get("full_date"),
                        exc,
                    )
        log.info("Influx resync done: %s", stats)
        return stats


# --------------------------------------------------------------------------- #
# Availability helper
# --------------------------------------------------------------------------- #
def influx_available(store: InfluxStore | None, timeout: float = 2.0) -> bool:
    """Return True when ``store`` is configured AND its InfluxDB responds.

    A store without url/token (or with unresolved ``env:`` placeholders) is
    simply not available — no network attempt is made. A live ping failure
    logs ERROR and returns False (behaviour is tolerant, R8, but the error is
    never swallowed silently).
    """
    if store is None or not store.is_configured():
        return False
    try:
        return bool(store.ping(timeout=timeout))
    except Exception as exc:  # noqa: BLE001 — availability check must not raise
        log.error("InfluxDB unavailable (ping failed): %s", exc)
        return False
