"""LIVE InfluxDB integration smoke test (WORKPLAN FASE 7, Task 7.1 — D18, §8.4 #8).

=============================== MAINTENANCE NOTES ===============================
Module   : tests/integration/test_influx_live.py
Scope    : The only test module that talks to a REAL InfluxDB 2.x. Everything
            else in the suite is hermetic (``tests.fakes.FakeInfluxClient``), so
            this file is the proof that the wire protocol implemented by
            ``daylio_api.db.influx_store`` (daily point, delete-and-write,
            Flux read) works against the server the user actually runs on
            ``192.168.188.120:8120`` (org ``tanadelgigante``, bucket ``daylio``).

Marker   : every test carries ``@pytest.mark.influx``. The CI ``test`` job runs
            them only when the Gitea repo var ``INFLUX_INSTANCE`` is set
            (it then exports ``INFLUX_URL="http://${INFLUX_INSTANCE}"``,
            ``INFLUX_ORG``/``INFLUX_BUCKET`` and ``INFLUX_TOKEN``); otherwise
            the job runs ``pytest -m "not influx"`` and this file is skipped
            (R16, risk mitigation table). ``tests/conftest.py`` also skips it
            when the env is incomplete, so a developer never needs a server.

Data    : every write uses the throwaway user tag ``__ci_probe__`` and is
            removed in the ``finally`` block, so the shared bucket is never
            polluted with test data even if a step fails midway.
================================================================================
"""

from __future__ import annotations

import pytest

from daylio_api.db.influx_store import InfluxStore, build_daily_point

#: Tag user used for the probe; never a real account.
PROBE_USER = "__ci_probe__"
#: Day used for the probe (far enough in the past to avoid dashboard windows).
PROBE_DATE = "2020-01-01"


def _record(mood: int = 2) -> dict:
    """Minimal normalized record accepted by ``build_daily_point``."""
    return {
        "full_date": PROBE_DATE,
        "time": "08:00",
        "mood": mood,
        "mood_label": "bene",
        "activities": ["probe-activity"],
        "scales": {"Stress": {"value": 3, "label": "Moderato"}},
        "note_title": "",
        "note": "",
        "source": "csv",
    }


@pytest.mark.influx
def test_live_influx_is_reachable(live_influx: InfluxStore) -> None:
    """The configured endpoint answers a ping (org/bucket/token are valid)."""
    assert live_influx.is_configured()
    assert live_influx.ping(timeout=10.0) is True


@pytest.mark.influx
def test_live_daily_point_write_read_delete(live_influx: InfluxStore) -> None:
    """Round-trip one daily point: write -> read back -> delete.

    Validates the §5.4 contract against the real server: the point is written
    at the fixed start-of-day timestamp with the ``user``/``date`` tags and is
    returned by ``read_daily_points`` (the dashboard's read abstraction).
    """
    try:
        point = build_daily_point(PROBE_USER, _record())
        assert point["measurement"] == "daylio_entry"
        assert point["time"] == f"{PROBE_DATE}T00:00:00Z"
        assert point["tags"] == {"user": PROBE_USER, "date": PROBE_DATE, "mood_label": "bene"}
        assert point["fields"]["mood"] == 2
        assert point["fields"]["scale_stress_value"] == 3
        assert point["fields"]["activities_count"] == 1

        live_influx.write_entry(PROBE_USER, _record())

        points = live_influx.read_daily_points(PROBE_USER, PROBE_DATE, PROBE_DATE)
        assert len(points) == 1
        read_back = points[0]
        assert read_back["user"] == PROBE_USER
        assert read_back["date"] == PROBE_DATE
        assert read_back["tags"]["mood_label"] == "bene"
        assert int(read_back["fields"]["mood"]) == 2
        assert int(read_back["fields"]["scale_stress_value"]) == 3

        # The date filter is honoured by the store on the real server too.
        assert live_influx.read_daily_points(PROBE_USER, "2020-02-01", "2020-02-02") == []
    finally:
        live_influx.delete_day(PROBE_USER, PROBE_DATE)
    assert live_influx.read_daily_points(PROBE_USER, PROBE_DATE, PROBE_DATE) == []


@pytest.mark.influx
def test_live_write_overwrites_the_whole_day(live_influx: InfluxStore) -> None:
    """A second write of the same day fully replaces the previous point (R8).

    Exercises the delete-and-write strategy: a record with fewer fields (no
    Stress scale) must not leave the old ``scale_stress_value`` behind.
    """
    try:
        live_influx.write_entry(PROBE_USER, _record())
        assert int(
            live_influx.read_daily_points(PROBE_USER, PROBE_DATE, PROBE_DATE)[0]["fields"][
                "scale_stress_value"
            ]
        ) == 3

        stripped = _record()
        stripped["scales"] = {}
        live_influx.write_entry(PROBE_USER, stripped)

        fields = live_influx.read_daily_points(PROBE_USER, PROBE_DATE, PROBE_DATE)[0]["fields"]
        assert "scale_stress_value" not in fields
    finally:
        live_influx.delete_day(PROBE_USER, PROBE_DATE)
