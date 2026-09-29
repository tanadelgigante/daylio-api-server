"""Test doubles for the Fase 3 stores (injectable clients).

=============================== MAINTENANCE NOTES ===============================
Module   : tests/fakes.py
Scope    : Fase 3 — the InfluxDB client is injectable (``InfluxStore(client=...)``,
           see ``daylio_api/db/influx_store.py``) so integration tests never
           need a real server. :class:`FakeInfluxClient` implements the exact
           duck-typed contract of the store:

             ``ping(**kwargs) -> bool``
             ``write(bucket, record) -> None``
             ``delete(start, stop, predicate, bucket, org) -> None``
             ``query(flux) -> list[dict]``
             ``close() -> None``

            plus test helpers:
              - ``get(user, date)``   the latest point for (user, date)
              - ``count(user=None)``  number of stored points
              - ``points``            raw point list (deep copies of writes)
              - ``deleted``           trail of every delete-day call
              - ``queries``           trail of every Flux query (Fase 6: proves
                                       an endpoint really read from Influx)
              - ``available``         flip to False to simulate an unreachable
                                       InfluxDB (``ping() -> False`` -> the
                                       dashboard analytics answer 503, R4/R8)

Behaviour mirrors the real delete-and-write strategy (§5.4): a write for a
``(user, date)`` always goes through ``delete`` first, so the resulting point
set never contains leftover fields of a previous write (R8).
================================================================================
"""

from __future__ import annotations

import copy
import re
from typing import Any

#: Matches the predicate emitted by ``InfluxStore.delete_day``:
#: ``_measurement="daylio_entry" AND user="default"``.
_PREDICATE_RE = re.compile(r'_measurement="([^"]+)" AND user="([^"]+)"')


class FakeInfluxClient:
    """In-memory InfluxDB representation for integration tests.

    Points are stored as the exact dicts built by
    ``daylio_api.db.influx_store.build_daily_point``. ``delete`` removes every
    point whose measurement, user tag and timestamp window match — the same
    semantic the real delete API implements on the TSDB.

    ``query`` intentionally IGNORES the Flux text and returns every point: the
    real range/user filtering is applied by
    ``InfluxStore.read_daily_points`` on top of the pivot, so the fake exercises
    exactly the same Python code path as a live server.
    """

    def __init__(self) -> None:
        self.points: list[dict[str, Any]] = []
        self.deleted: list[dict[str, str]] = []
        self.queries: list[str] = []
        #: Flip to False to simulate an unreachable InfluxDB (Fase 6 503 path).
        self.available = True

    # ------------------------------------------------------------------ #
    # Duck-typed client contract
    # ------------------------------------------------------------------ #
    def ping(self, **kwargs: Any) -> bool:
        """Report the simulated availability (``available`` flag)."""
        return bool(self.available)


    def write(self, bucket: str, record: dict[str, Any]) -> None:
        """Store a deep copy of the written point (bucket ignored: single fake)."""
        self.points.append(copy.deepcopy(record))

    def delete(
        self, start: str, stop: str, predicate: str, bucket: str, org: str
    ) -> None:
        """Remove matching points and record the delete-day trail."""
        self.deleted.append(
            {"start": start, "stop": stop, "predicate": predicate, "bucket": bucket, "org": org}
        )
        match = _PREDICATE_RE.match(predicate)
        if match is None:
            return  # nothing to filter defensively if the predicate changes
        measurement = match.group(1)
        user = match.group(2)
        self.points = [
            point
            for point in self.points
            if not (
                point.get("measurement") == measurement
                and (point.get("tags") or {}).get("user") == user
                and start <= point.get("time", "") < stop
            )
        ]

    def query(self, flux: str) -> list[dict[str, Any]]:
        """Return deep copies of all stored points (read contract, not pivoted).

        The Flux text is recorded in ``queries`` (Fase 6 asserts the dashboard
        really goes through InfluxDB) but not interpreted: range/user filtering
        happens in ``InfluxStore.read_daily_points``.
        """
        self.queries.append(flux)
        return copy.deepcopy(self.points)

    def close(self) -> None:
        """No-op: in-memory client has nothing to release."""

    # ------------------------------------------------------------------ #
    # Test helpers
    # ------------------------------------------------------------------ #
    def get(self, user: str, date: str) -> dict[str, Any] | None:
        """Return the current point for ``(user, date)`` or None."""
        for point in reversed(self.points):
            tags = point.get("tags") or {}
            if tags.get("user") == user and tags.get("date") == date:
                return copy.deepcopy(point)
        return None

    def count(self, user: str | None = None) -> int:
        """Number of stored points (optionally filtered by user tag)."""
        if user is None:
            return len(self.points)
        return sum(1 for point in self.points if (point.get("tags") or {}).get("user") == user)

    def drop(self, user: str, date: str) -> None:
        """Remove every point for ``(user, date)`` (simulate data loss)."""
        self.points = [
            point
            for point in self.points
            if not (
                (point.get("tags") or {}).get("user") == user
                and (point.get("tags") or {}).get("date") == date
            )
        ]
