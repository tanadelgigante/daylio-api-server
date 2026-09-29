"""Independent QA acceptance suite for WORKPLAN FASE 6 (R4/D4).

=============================== MAINTENANCE NOTES ================================
Module   : tests/integration/test_dashboard_acceptance_qa.py
Scope    : QA (Tester) re-verification of FASE 6 — the InfluxDB-only dashboard
           (WORKPLAN §7 Task 6.1/6.2/6.3, §5.6, §5.7, R4/D4). Deliberately
           INDEPENDENT from ``test_dashboard_api.py``:

             - its own seed dataset (2026-03-02..08, a different week and a
               different mood/activities/scales mix) so the expectations are a
               hand-computed cross-check, not a re-run of the author's seeds;
             - an allow-list of the SQL statements an analytics request may
               execute (the author's spy only greps for ``day_entries``, which
               would miss any other SQL table);
             - extra paths the author did not cover (API-key auth parity,
               heatmap hour column driven by the point timestamp, unresolved
               ``mood=0`` points, ``?user=`` empty value, static-asset
               content types, the unknown-``?scale=`` contract).

Dataset (one Influx point per day, written through the REAL
``InfluxStore.write_entry`` -> ``build_daily_point``, so the §5.4 point contract
is the real one):

    2026-03-02 Mon mood 1 "alla grande" [corsa, lettura]      stress 1 Calma     salute ottimo(1)
    2026-03-03 Tue mood 2 "bene"       [corsa]                stress -           salute -
    2026-03-04 Wed mood 3 "così così"  []                     stress 5 Sopraff. salute mediocre(5)
    2026-03-05 Thu mood 4 "male"       [lavoro, corsa, ottimo] stress 2 Basso     salute ottimo(1)
    2026-03-06 Fri mood 5 "malissimo"  [lavoro]               stress -           salute -
    2026-03-07 Sat -- NO RECORD (proves the missing-day semantics)
    2026-03-08 Sun mood 2 "bene"       [corsa, studio]        stress 4 Alto      salute discreto(3)

Hand-computed expectations used below:
  * mean mood per day      -> [1, 2, 3, 4, 5, 2], average 17/6 = 2.83
  * mood level counts 1..5 -> [1, 2, 1, 1, 1], total 6
  * heatmap (Mon-first)     -> Mon..Fri = 1 record each, Sat = 0, Sun = 1 (total 6)
  * scales-trend stress    -> [1, None, 5, 2, None, 4], days_with_value 4
  * scales-trend salute    -> [1, None, 5, 1, None, 3], days_with_value 4
  * activities-top          -> corsa 4, lavoro 2, lettura 1, studio 1
    ("ottimo" on 2026-03-05 is a D2 health-scale level, NOT an activity)

Follow-up (QA round 2): the two minor divergences this suite had flagged as
``xfail`` are FIXED and their markers removed, so the whole file must now run
green with no expected failures:
  * BUG-1 ``?scale=not-a-scale`` answered 200 with an all-null series ->
    ``test_qa_unknown_scale_name_is_rejected`` now expects the 400;
  * BUG-2 ``?user=`` (empty) resolved the scope to ``""`` ->
    ``test_qa_empty_user_param_falls_back_to_the_viewer`` now expects the
    viewer's own scope.
================================================================================
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any

import pytest

from daylio_api.app import create_app
from daylio_api.dashboard import views as dashboard_views
from daylio_api.db.influx_store import InfluxConfig, InfluxStore, build_daily_point
from tests.fakes import FakeInfluxClient
from tests.helpers import login

# --------------------------------------------------------------------------- #
# QA seed dataset (independent from the author's SEED_DAYS)
# --------------------------------------------------------------------------- #
QA_DAYS: tuple[tuple[str, int, str, list[str], int | None, str | None], ...] = (
    ("2026-03-02", 1, "alla grande", ["corsa", "lettura"], 1, "ottimo"),
    ("2026-03-03", 2, "bene", ["corsa"], None, None),
    ("2026-03-04", 3, "così così", [], 5, "mediocre"),
    ("2026-03-05", 4, "male", ["lavoro", "corsa", "ottimo"], 2, "ottimo"),
    ("2026-03-06", 5, "malissimo", ["lavoro"], None, None),
    # 2026-03-07 (Sat) intentionally missing.
    ("2026-03-08", 2, "bene", ["corsa", "studio"], 4, "discreto"),
)
QA_OTHER_USER: tuple[tuple[str, int, str, list[str], int | None, str | None], ...] = (
    ("2026-04-13", 3, "così così", ["nuoto", "lettura"], 2, "buono"),
)

_STRESS_LABELS = {1: "Calma", 2: "Basso", 3: "Moderato", 4: "Alto", 5: "Sopraffatto"}
_SALUTE_RANKS = {"ottimo": 1, "buono": 2, "discreto": 3, "sufficiente": 4, "mediocre": 5}

#: The five charts the deliverable must expose (criterio 1).
CHARTS = ("mood-over-time", "mood-distribution", "heatmap", "scales-trend", "activities-top")

#: Inclusive QA window: Monday 2026-03-02 .. Sunday 2026-03-08.
RANGE = "start_date=2026-03-02&end_date=2026-03-08"


def _record(
    full_date: str,
    mood: int,
    mood_label: str,
    activities: list[str],
    stress_rank: int | None,
    salute_label: str | None,
) -> dict[str, Any]:
    """Canonical record consumed by ``build_daily_point`` (the real §5.4 writer)."""
    scales: list[dict[str, Any]] = []
    if stress_rank is not None:
        scales.append(
            {
                "scale_name": "Stress",
                "label": _STRESS_LABELS[stress_rank],
                "rank": stress_rank,
                "levels_count": 5,
                "scale_key": "stress",
            }
        )
    return {
        "full_date": full_date,
        "date": full_date,
        "weekday": "",
        "time": "12:00",
        "mood": mood,
        "mood_label": mood_label,
        "activities": list(activities),
        "scales": scales,
        "scala_salute": (
            {"label": salute_label, "rank": _SALUTE_RANKS.get(salute_label, 0)}
            if salute_label
            else None
        ),
        "note_title": "",
        "note": "",
        "source": "csv",
    }


def _seed(store: InfluxStore, user: str, days: Any) -> None:
    for day in days:
        store.write_entry(user, _record(*day))


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def qa_fake() -> FakeInfluxClient:
    return FakeInfluxClient()


@pytest.fixture
def qa_influx(qa_fake: FakeInfluxClient) -> InfluxStore:
    return InfluxStore(
        InfluxConfig(
            url="http://qa-influx:8126",
            token="qa-token",
            org="tanadelgigante",
            bucket="daylio",
            measurement="daylio_entry",
        ),
        client=qa_fake,
    )


@pytest.fixture
def qa_app(tmp_path: Path, monkeypatch, qa_influx: InfluxStore) -> Any:
    monkeypatch.setenv("DB_PATH", str(tmp_path / "qa_dashboard.db"))
    application = create_app()
    application.config["TESTING"] = True
    application.extensions["daylio_influx"] = qa_influx
    _seed(qa_influx, "admin", QA_DAYS)
    _seed(qa_influx, "bob", QA_OTHER_USER)
    return application


@pytest.fixture
def qa_client(qa_app: Any) -> Any:
    client = qa_app.test_client()
    assert login(client).status_code == 200
    return client


def _create_user(app: Any, username: str, password: str = "qa-pass-123") -> None:
    from daylio_api.api.auth import hash_password
    from daylio_api.db.sqlite_store import create_user

    conn = app.extensions["daylio_store"].connect()
    create_user(conn, username, password_hash=hash_password(password), is_admin=False)
    conn.commit()


def _login_as(app: Any, username: str, password: str = "qa-pass-123") -> Any:
    client = app.test_client()
    resp = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert resp.status_code == 200, resp.get_json()
    return client


# --------------------------------------------------------------------------- #
# Criterio 4 — aggregations vs the real sample semantics
# --------------------------------------------------------------------------- #
def test_qa_mood_over_time_matches_hand_computed_means(qa_client) -> None:
    """Daily mean mood, the Saturday gap and the average are hand-checked."""
    data = qa_client.get(f"/api/analytics/mood-over-time?{RANGE}").get_json()
    assert data["user"] == "admin"
    assert data["granularity"] == "day"
    assert data["dates"] == [
        "2026-03-02",
        "2026-03-03",
        "2026-03-04",
        "2026-03-05",
        "2026-03-06",
        "2026-03-08",
    ]
    assert data["values"] == [1, 2, 3, 4, 5, 2]
    assert data["average"] == pytest.approx(2.83, abs=0.01)
    assert data["days"] == 6
    # Per-bucket level histogram (D1: 1 = best .. 5 = worst).
    assert [entry["2"] for entry in data["levels"]] == [0, 1, 0, 0, 0, 1]
    assert [entry["5"] for entry in data["levels"]] == [0, 0, 0, 0, 1, 0]


def test_qa_mood_over_time_weekly_bucket_and_auto_threshold(qa_client) -> None:
    """ISO-week bucketing (auto above 92 days, forced with ?granularity=)."""
    long_range = qa_client.get(
        "/api/analytics/mood-over-time?start_date=2024-01-01&end_date=2026-03-08"
    ).get_json()
    assert long_range["granularity"] == "week"
    assert long_range["dates"] == ["2026-W10"]  # the whole QA week is ISO week 10
    assert long_range["values"] == [pytest.approx(2.83, abs=0.01)]
    assert long_range["levels"] == [{"1": 1, "2": 2, "3": 1, "4": 1, "5": 1}]

    forced = qa_client.get(
        "/api/analytics/mood-over-time?start_date=2024-01-01&end_date=2026-03-08"
        "&granularity=day"
    ).get_json()
    assert forced["granularity"] == "day" and len(forced["dates"]) == 6
    # Enum values are case-insensitive and unknown ones are a clean 400.
    assert qa_client.get(
        f"/api/analytics/mood-over-time?{RANGE}&granularity=DAY"
    ).get_json()["granularity"] == "day"
    assert qa_client.get(
        f"/api/analytics/mood-over-time?{RANGE}&granularity=month"
    ).status_code == 400


def test_qa_mood_distribution_counts_labels_and_average(qa_client) -> None:
    """Level counts, the D1 labels coming from the Influx tags and the average."""
    data = qa_client.get(f"/api/analytics/mood-distribution?{RANGE}").get_json()
    assert data["levels"] == [1, 2, 3, 4, 5]
    assert data["counts"] == [1, 2, 1, 1, 1]
    assert data["total"] == 6
    assert data["average"] == pytest.approx(2.83, abs=0.01)
    assert data["labels"] == ["alla grande", "bene", "così così", "male", "malissimo"]


def test_qa_heatmap_weekday_matrix_and_missing_saturday(qa_client) -> None:
    """7x24 Mon-first matrix: the Saturday row is empty (null, not 0)."""
    data = qa_client.get(f"/api/analytics/heatmap?{RANGE}").get_json()
    assert data["metric"] == "mean_mood"
    assert data["weekdays"] == ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    assert data["buckets"] == [f"{h:02d}" for h in range(24)]
    assert len(data["data"]) == 7 and all(len(row) == 24 for row in data["data"])
    # One record per weekday, all at the §5.4 start-of-day timestamp.
    assert [row[0] for row in data["counts"]] == [1, 1, 1, 1, 1, 0, 1]
    assert data["total"] == 6
    # Monday..Friday means follow the seeded moods, Saturday is null everywhere.
    assert [data["data"][row][0] for row in range(5)] == [1, 2, 3, 4, 5]
    assert data["data"][5] == [None] * 24
    assert data["data"][6][0] == 2  # Sunday


def test_qa_heatmap_count_metric(qa_client) -> None:
    """``metric=count`` reports the records per cell (0.0 where there are none)."""
    data = qa_client.get(f"/api/analytics/heatmap?{RANGE}&metric=count").get_json()
    assert data["metric"] == "count"
    assert [row[0] for row in data["data"]] == [1, 1, 1, 1, 1, 0, 1]
    assert data["total"] == 6


def test_qa_heatmap_hour_column_follows_the_point_timestamp(qa_app, qa_fake) -> None:
    """The hour column is read from the point timestamp (intraday-ready).

    §5.4 fixes the daily point at ``T00:00:00Z``, so with the production writer
    every record lands in the ``00`` bucket. Writing a point with a real time
    proves the column is not hard-coded (the author only covered the 00 case).
    """
    for day, hour, mood in (("2026-05-04", 9, 2), ("2026-05-05", 21, 4)):
        point = build_daily_point(
            "admin", _record(day, mood, "bene", ["corsa"], 1, "ottimo")
        )
        point["time"] = f"{day}T{hour:02d}:30:00Z"
        qa_fake.write(bucket="daylio", record=point)
    client = _login_as(qa_app, "admin", password="admin-pass")
    data = client.get(
        "/api/analytics/heatmap?start_date=2026-05-04&end_date=2026-05-05"
    ).get_json()
    assert data["counts"][0][9] == 1  # Monday 09h
    assert data["counts"][1][21] == 1  # Tuesday 21h
    assert data["data"][0][9] == 2
    assert data["data"][1][21] == 4
    assert data["total"] == 2


def test_qa_scales_trend_stress_keeps_missing_days_as_null(qa_client) -> None:
    """Stress trend: 4 of 6 days carry a value, the others are null (not 0)."""
    data = qa_client.get(f"/api/analytics/scales-trend?{RANGE}&scale=stress").get_json()
    assert data["scale_name"] == "stress"
    assert data["scale_label"] == "Stress"
    assert data["dates"] == [
        "2026-03-02",
        "2026-03-03",
        "2026-03-04",
        "2026-03-05",
        "2026-03-06",
        "2026-03-08",
    ]
    assert data["values"] == [1, None, 5, 2, None, 4]
    assert data["labels"] == ["Calma", None, "Sopraffatto", "Basso", None, "Alto"]
    assert data["days_with_value"] == 4 and data["days"] == 6


def test_qa_scales_trend_health_scale_uses_the_tag_label(qa_client) -> None:
    """D2 scala salute: numeric rank + the label carried by the §5.4 tag."""
    data = qa_client.get(
        f"/api/analytics/scales-trend?{RANGE}&scale=Scala%20salute"
    ).get_json()
    assert data["scale_name"] == "scala_salute"
    assert data["scale_label"] == "Scala salute"
    assert data["values"] == [1, None, 5, 1, None, 3]
    assert data["labels"] == ["ottimo", None, "mediocre", "ottimo", None, "discreto"]
    assert data["days_with_value"] == 4


def test_qa_scales_trend_without_filter_discovers_every_scale(qa_client) -> None:
    """Without ``?scale=`` the endpoint returns every scale found in the data."""
    data = qa_client.get(f"/api/analytics/scales-trend?{RANGE}").get_json()
    assert [item["scale_name"] for item in data["scales"]] == ["scala_salute", "stress"]
    for item in data["scales"]:
        assert item["days"] == 6 and item["days_with_value"] == 4
    # Case-insensitive config key.
    assert qa_client.get(
        f"/api/analytics/scales-trend?{RANGE}&scale=STRESS"
    ).get_json()["scale_name"] == "stress"


def test_qa_activities_top_excludes_the_health_scale_levels(qa_client) -> None:
    """D2: "ottimo" on 2026-03-05 is a health level, not an activity."""
    data = qa_client.get(f"/api/analytics/activities-top?{RANGE}").get_json()
    assert data["excluded_scale"] == "scala_salute"
    assert data["activities"] == [
        {"activity": "corsa", "count": 4},
        {"activity": "lavoro", "count": 2},
        {"activity": "lettura", "count": 1},
        {"activity": "studio", "count": 1},
    ]
    assert data["distinct"] == 4
    assert data["days"] == 6
    assert data["limit"] == 10
    assert not any(item["activity"] == "ottimo" for item in data["activities"])


def test_qa_activities_top_limit_narrows_the_ranking(qa_client) -> None:
    assert qa_client.get(
        f"/api/analytics/activities-top?{RANGE}&limit=2"
    ).get_json()["activities"] == [
        {"activity": "corsa", "count": 4},
        {"activity": "lavoro", "count": 2},
    ]


def test_qa_date_filters_are_inclusive_and_echoed(qa_client) -> None:
    """Both bounds are inclusive and echoed back with the resolved scope."""
    data = qa_client.get(
        "/api/analytics/mood-over-time?start_date=2026-03-03&end_date=2026-03-05"
    ).get_json()
    assert data["dates"] == ["2026-03-03", "2026-03-04", "2026-03-05"]
    assert data["days"] == 3
    assert data["start_date"] == "2026-03-03" and data["end_date"] == "2026-03-05"
    single = qa_client.get(
        "/api/analytics/mood-over-time?start_date=2026-03-04&end_date=2026-03-04"
    ).get_json()
    assert single["values"] == [3]


def test_qa_unresolved_mood_zero_is_skipped_not_plotted_as_zero(qa_app, qa_fake) -> None:
    """A point with mood 0 (unresolved label) must not become a "0" data point."""
    point = build_daily_point(
        "admin", _record("2026-03-10", 0, "", [], None, None)
    )
    qa_fake.write(bucket="daylio", record=point)
    client = _login_as(qa_app, "admin", password="admin-pass")
    window = "start_date=2026-03-10&end_date=2026-03-10"
    series = client.get(f"/api/analytics/mood-over-time?{window}").get_json()
    assert series["dates"] == [] and series["values"] == [] and series["average"] is None
    assert series["days"] == 1  # the point WAS read, it just has no mood
    dist = client.get(f"/api/analytics/mood-distribution?{window}").get_json()
    assert dist["total"] == 0 and dist["counts"] == [0, 0, 0, 0, 0]
    heat = client.get(f"/api/analytics/heatmap?{window}").get_json()
    assert heat["total"] == 0


# --------------------------------------------------------------------------- #
# Criterio 1 — validation matrix (400) and 404
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("path", "fragment"),
    [
        ("/api/analytics/mood-over-time?start_date=02-03-2026&end_date=2026-03-08", "invalid date"),
        ("/api/analytics/mood-over-time?start_date=2026-03-02&end_date=2026-13-45", "invalid date"),
        ("/api/analytics/mood-over-time?start_date=2026-05-01&end_date=2026-01-01", "start_date"),
        (f"/api/analytics/mood-over-time?{RANGE}&granularity=year", "granularity"),
        (f"/api/analytics/heatmap?{RANGE}&metric=p95", "metric"),
        (f"/api/analytics/activities-top?{RANGE}&limit=0", "limit"),
        (f"/api/analytics/activities-top?{RANGE}&limit=51", "limit"),
        (f"/api/analytics/activities-top?{RANGE}&limit=many", "limit"),
    ],
)
def test_qa_validation_matrix_is_400_json(qa_client, path: str, fragment: str) -> None:
    resp = qa_client.get(path)
    assert resp.status_code == 400, path
    assert resp.is_json
    body = resp.get_json()
    assert body["type"] == "bad_request"
    assert fragment in body["error"]


@pytest.mark.parametrize("chart", CHARTS)
def test_qa_unknown_chart_is_json_404(qa_client, chart: str) -> None:
    resp = qa_client.get(f"/api/analytics/{chart}-of-doom")
    assert resp.status_code == 404
    assert resp.is_json and resp.get_json()["type"] == "not_found"


def test_qa_index_exposes_the_five_charts(qa_client) -> None:
    """The self-describing index lists exactly the five deliverable charts."""
    for path in ("/api/analytics", "/api/analytics/"):
        data = qa_client.get(path).get_json()
        assert {item["chart"] for item in data["charts"]} == set(CHARTS)
        assert all(item["title"] for item in data["charts"])
        assert data["source"] == "influxdb"


def test_qa_unknown_scale_name_is_rejected(qa_client) -> None:
    """An invalid ``?scale=`` value must be a clean 400, not an empty chart.

    FIXED (was xfail, BUG-1): ``scales-trend`` validates the filter against the
    configured scales AND the scales carried by the caller's own points.
    """
    resp = qa_client.get(f"/api/analytics/scales-trend?{RANGE}&scale=not-a-scale")
    assert resp.status_code in {400, 404}
    assert resp.get_json()["type"] in {"bad_request", "not_found"}
    assert "not-a-scale" in resp.get_json()["error"]
    # The message lists what IS available instead of silently answering null.
    assert "stress" in resp.get_json()["error"]


def test_qa_empty_user_param_falls_back_to_the_viewer(qa_client) -> None:
    """``?user=`` (empty) must not silently resolve to the empty username.

    FIXED (was xfail, BUG-2): ``_effective_user`` treats an empty value as an
    absent filter, so the scope is the authenticated viewer.
    """
    data = qa_client.get("/api/analytics/mood-distribution?user=").get_json()
    assert data["user"] == "admin"
    assert data["total"] == 6
    # Whitespace-only and missing are the same "no filter" case.
    for query in ("user=%20%20", ""):
        data = qa_client.get(f"/api/analytics/mood-distribution?{query}").get_json()
        assert data["user"] == "admin" and data["total"] == 6


# --------------------------------------------------------------------------- #
# Criterio 1 — auth parity with Fase 5 (401 / 403 / cross-user / API key)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("chart", CHARTS)
def test_qa_anonymous_is_401_for_every_chart(qa_app, chart: str) -> None:
    resp = qa_app.test_client().get(f"/api/analytics/{chart}?{RANGE}")
    assert resp.status_code == 401
    assert resp.is_json and resp.get_json()["type"] == "unauthorized"


def test_qa_anonymous_is_401_for_the_index_and_the_static_assets_are_public(qa_app) -> None:
    assert qa_app.test_client().get("/api/analytics").status_code == 401
    # The SPA shell is public on purpose (it is a login form until /auth/me).
    assert qa_app.test_client().get("/dashboard").status_code == 200


@pytest.mark.parametrize("chart", CHARTS)
def test_qa_non_admin_cannot_read_another_user(qa_app, chart: str) -> None:
    _create_user(qa_app, "dave")
    dave = _login_as(qa_app, "dave")
    for other in ("admin", "bob", "ghost"):
        resp = dave.get(f"/api/analytics/{chart}?{RANGE}&user={other}")
        assert resp.status_code == 403, (chart, other)
        assert resp.get_json()["type"] == "forbidden"


def test_qa_non_admin_reads_only_its_own_data(qa_app) -> None:
    """A non-admin without data gets empty shapes, never somebody else's rows."""
    _create_user(qa_app, "dave")
    dave = _login_as(qa_app, "dave")
    own = dave.get(f"/api/analytics/mood-distribution?{RANGE}").get_json()
    assert own["user"] == "dave" and own["total"] == 0 and own["counts"] == [0] * 5
    assert dave.get(f"/api/analytics/mood-over-time?{RANGE}").get_json()["dates"] == []
    # Passing its own username explicitly is allowed (idempotent filter).
    assert dave.get(f"/api/analytics/mood-distribution?{RANGE}&user=dave").status_code == 200


def test_qa_admin_user_filter_returns_the_other_users_series(qa_client) -> None:
    """An admin may scope to another user and sees THAT user's data only."""
    bob = qa_client.get(
        "/api/analytics/mood-distribution?user=bob&start_date=2026-04-01&end_date=2026-04-30"
    ).get_json()
    assert bob["user"] == "bob" and bob["total"] == 1 and bob["counts"] == [0, 0, 1, 0, 0]

    mine = qa_client.get(f"/api/analytics/mood-distribution?{RANGE}").get_json()
    assert mine["user"] == "admin" and mine["total"] == 6
    # The admin's own series must not be polluted by the other user's point.
    assert qa_client.get(
        "/api/analytics/activities-top?user=bob&start_date=2026-04-01&end_date=2026-04-30"
    ).get_json()["activities"] == [
        {"activity": "lettura", "count": 1},
        {"activity": "nuoto", "count": 1},
    ]


def test_qa_analytics_accepts_an_api_key_like_the_rest_of_the_api(qa_client, qa_app) -> None:
    """Fase 5 auth parity: a personal API key also opens the analytics (R6)."""
    created = qa_client.post("/api/auth/api-keys", json={"name": "qa"})
    assert created.status_code == 201
    key = created.get_json()["key"]

    bearer = qa_app.test_client().get(
        f"/api/analytics/mood-distribution?{RANGE}",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert bearer.status_code == 200 and bearer.get_json()["user"] == "admin"
    header = qa_app.test_client().get(
        f"/api/analytics/mood-distribution?{RANGE}", headers={"X-API-Key": key}
    )
    assert header.status_code == 200 and header.get_json()["counts"] == [1, 2, 1, 1, 1]
    assert qa_app.test_client().get(
        f"/api/analytics/mood-distribution?{RANGE}", headers={"X-API-Key": "daylio_bogus"}
    ).status_code == 401


# --------------------------------------------------------------------------- #
# Criterio 2 — R4/D4: Influx only, never SQLite
# --------------------------------------------------------------------------- #
def test_qa_analytics_sql_surface_is_identity_only(qa_client, qa_app) -> None:
    """Allow-list spy: the ONLY SQL an analytics request may run is the identity.

    The author's spy only greps for ``day_entries``; this pins the whole
    statement set, so any other table (``columns_config``, ``scales_config``,
    ``import_jobs``, ``api_keys``...) would fail the test too.
    """
    conn = qa_app.extensions["daylio_store"].connect()
    statements: list[str] = []
    conn.set_trace_callback(statements.append)

    for chart in CHARTS:
        del statements[:]
        resp = qa_client.get(f"/api/analytics/{chart}?{RANGE}")
        assert resp.status_code == 200, chart
        assert statements, f"{chart}: no SQL at all is even better, got none"
        for statement in statements:
            normalised = " ".join(statement.split()).lower()
            assert normalised.startswith("select"), (chart, statement)
            assert "from users" in normalised, (chart, statement)
            assert "day_entries" not in normalised, (chart, statement)


def test_qa_analytics_never_reads_day_entries_even_when_influx_is_down(
    qa_app, qa_fake
) -> None:
    """The 503 path is SQL-free too: no silent fallback to the source of truth."""
    from daylio_api.db.sqlite_store import upsert_entry

    conn = qa_app.extensions["daylio_store"].connect()
    upsert_entry(conn, "admin", _record("2026-06-01", 1, "alla grande", ["corsa"], 1, "ottimo"))
    conn.commit()
    statements: list[str] = []
    conn.set_trace_callback(statements.append)

    qa_fake.available = False
    client = _login_as(qa_app, "admin", password="admin-pass")
    del statements[:]
    assert client.get("/api/analytics/mood-distribution").status_code == 503
    assert [s for s in statements if "day_entries" in s.lower()] == []
    # SQLite remains the source of truth for the SQL API (D4 is not reversed).
    assert client.get("/api/records").get_json()["total"] == 1


@pytest.mark.parametrize("chart", CHARTS)
def test_qa_each_analytics_call_issues_one_bounded_influx_query(
    qa_client, qa_fake, chart: str
) -> None:
    """Every chart reads Influx with the user tag and the date range pushed down."""
    before = len(qa_fake.queries)
    assert qa_client.get(f"/api/analytics/{chart}?{RANGE}").status_code == 200
    issued = qa_fake.queries[before:]
    assert len(issued) == 1, chart
    flux = issued[0]
    assert 'from(bucket: "daylio")' in flux
    assert 'r._measurement == "daylio_entry"' in flux
    assert 'r.user == "admin"' in flux
    assert "range(start: 2026-03-02T00:00:00Z, stop: 2026-03-09T00:00:00Z)" in flux


def test_qa_analytics_user_filter_is_pushed_down_to_flux(qa_client, qa_fake) -> None:
    before = len(qa_fake.queries)
    assert qa_client.get(
        "/api/analytics/mood-distribution?user=bob&start_date=2026-04-01&end_date=2026-04-30"
    ).status_code == 200
    flux = qa_fake.queries[before:]
    assert len(flux) == 1 and 'r.user == "bob"' in flux[0]
    assert "range(start: 2026-04-01T00:00:00Z, stop: 2026-05-01T00:00:00Z)" in flux[0]


def test_qa_dashboard_package_never_imports_the_sql_store() -> None:
    """Static (AST) guarantee for the WHOLE dashboard package, not one module."""
    package_dir = Path(dashboard_views.__file__).parent
    python_files = sorted(package_dir.rglob("*.py"))
    assert python_files, "the dashboard package must ship python modules"
    for path in python_files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                # Both the module path and the imported names, so that
                # ``from daylio_api.db import sqlite_store`` is caught too.
                imported.add(node.module or "")
                imported.update(alias.name for alias in node.names)
        assert not any("sqlite" in name for name in imported), (path.name, imported)
    # The only storage the analytics module may touch is InfluxDB.
    views_imports = {
        node.module or ""
        for node in ast.walk(ast.parse(Path(dashboard_views.__file__).read_text("utf-8")))
        if isinstance(node, ast.ImportFrom)
    }
    assert "daylio_api.db.influx_store" in views_imports


def test_qa_analytics_aggregations_are_driven_by_read_daily_points(
    qa_client, qa_app, monkeypatch
) -> None:
    """The read abstraction is ``InfluxStore.read_daily_points`` (one call/chart)."""
    calls: list[tuple[Any, ...]] = []
    original = InfluxStore.read_daily_points

    def spy(self, user, start=None, end=None):  # noqa: ANN001, ANN202 - test spy
        calls.append((user, start, end))
        return original(self, user, start, end)

    monkeypatch.setattr(InfluxStore, "read_daily_points", spy)
    store = qa_app.extensions["daylio_influx"]
    for chart in CHARTS:
        del calls[:]
        assert qa_client.get(f"/api/analytics/{chart}?{RANGE}").status_code == 200
        assert calls == [("admin", "2026-03-02", "2026-03-08")], (chart, calls)
    # And never the Fase 3/4 raw accessor.
    assert not hasattr(store, "read_records_from_sqlite")


# --------------------------------------------------------------------------- #
# Criterio 1/3 — 503 when InfluxDB is not usable
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("chart", CHARTS)
def test_qa_503_when_influx_ping_fails(qa_app, qa_fake, chart: str) -> None:
    qa_fake.available = False
    client = _login_as(qa_app, "admin", password="admin-pass")
    resp = client.get(f"/api/analytics/{chart}")
    assert resp.status_code == 503, chart
    body = resp.get_json()
    assert body["type"] == "service_unavailable"
    assert "Influx" in body["error"]
    # The message must never suggest a SQL fallback (R4).
    assert "sqlite" not in json.dumps(body).lower()


@pytest.mark.parametrize("chart", CHARTS)
def test_qa_503_when_the_influx_store_is_not_configured(qa_app, chart: str) -> None:
    qa_app.extensions["daylio_influx"] = None
    client = _login_as(qa_app, "admin", password="admin-pass")
    resp = client.get(f"/api/analytics/{chart}")
    assert resp.status_code == 503 and resp.get_json()["type"] == "service_unavailable"


def test_qa_503_when_the_flux_query_raises(qa_client, qa_fake) -> None:
    """A raising query is a 503 with no internal detail leaked to the client."""
    def boom(flux: str) -> list[dict[str, Any]]:
        raise RuntimeError("dial tcp 10.0.0.1:8126: connection refused")

    qa_fake.query = boom  # type: ignore[method-assign]
    resp = qa_client.get("/api/analytics/mood-over-time")
    assert resp.status_code == 503
    body = resp.get_json()
    assert body["type"] == "service_unavailable"
    assert "connection refused" not in json.dumps(body)
    assert "10.0.0.1" not in json.dumps(body)


def test_qa_empty_range_returns_empty_shapes_not_500(qa_client) -> None:
    window = "start_date=2031-01-01&end_date=2031-01-31"
    empty = qa_client.get(f"/api/analytics/mood-over-time?{window}").get_json()
    assert empty["dates"] == [] and empty["values"] == [] and empty["average"] is None
    assert qa_client.get(f"/api/analytics/mood-distribution?{window}").get_json()[
        "counts"
    ] == [0, 0, 0, 0, 0]
    assert qa_client.get(f"/api/analytics/scales-trend?{window}").get_json()["scales"] == []
    assert qa_client.get(f"/api/analytics/activities-top?{window}").get_json()[
        "activities"
    ] == []


# --------------------------------------------------------------------------- #
# Criterio 3 — the SPA, its assets and the vendored Chart.js
# --------------------------------------------------------------------------- #
def test_qa_dashboard_serves_the_spa_shell(qa_client) -> None:
    resp = qa_client.get("/dashboard")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["Content-Type"]
    body = resp.get_data(as_text=True)
    assert "/dashboard/static/vendor/chart.umd.min.js" in body
    assert "/dashboard/static/app.js" in body
    assert "/dashboard/static/app.css" in body
    # Both spellings answer 200 (no redirect) so the absolute asset URLs work.
    assert qa_client.get("/dashboard/").status_code == 200


@pytest.mark.parametrize(
    ("asset", "content_type"),
    [
        ("/dashboard/static/app.js", "javascript"),
        ("/dashboard/static/app.css", "text/css"),
        ("/dashboard/static/vendor/chart.umd.min.js", "javascript"),
    ],
)
def test_qa_static_assets_are_served_with_the_right_type(
    qa_client, asset: str, content_type: str
) -> None:
    resp = qa_client.get(asset)
    assert resp.status_code == 200 and content_type in resp.headers["Content-Type"]
    assert len(resp.get_data()) > 1000


def test_qa_unknown_static_asset_is_404(qa_client) -> None:
    assert qa_client.get("/dashboard/static/nope.js").status_code == 404


def test_qa_vendored_chartjs_is_a_real_build_with_a_version() -> None:
    """The vendored UMD is a genuine Chart.js release, not a stub or a shim."""
    vendor = Path(dashboard_views.__file__).parent / "static" / "vendor" / "chart.umd.min.js"
    content = vendor.read_text(encoding="utf-8")
    version = re.search(r"Chart\.js v(\d+\.\d+\.\d+)", content)
    assert version, "the vendored file must carry its Chart.js version banner"
    assert version.group(1) >= "4.0.0", version.group(1)
    assert len(content) > 150_000  # a full minified UMD build
    # UMD wrapper + real v4 API surface (register / the v4 controllers).
    assert "define.amd" in content[:600] and "chartjs.org" in content[:300]
    for api in ("register", "BarController", "DoughnutController", "LineController"):
        assert api in content, api


def test_qa_spa_dom_contract_between_index_and_app() -> None:
    """Every element id app.js touches must exist in the served HTML shell."""
    static_dir = Path(dashboard_views.__file__).parent / "static"
    app_js = (static_dir / "app.js").read_text(encoding="utf-8")
    index_html = (static_dir / "index.html").read_text(encoding="utf-8")
    html_ids = set(re.findall(r'id="([^"]+)"', index_html))
    # Canvas ids are passed to mountChart() as literals, not through $("id").
    mounted = set(re.findall(r'mountChart\("([^"]+)"', app_js))
    used = set(re.findall(r'\$\("([^"]+)"\)', app_js)) | mounted
    assert used, "app.js must drive the page"
    missing = sorted(used - html_ids)
    assert not missing, f"app.js references ids absent from index.html: {missing}"
    # The five chart canvases of the deliverable are all present.
    assert {
        "chart-mood-over-time",
        "chart-mood-distribution",
        "chart-activities-top",
    } <= html_ids
    assert {"heatmap", "scales-trend-charts", "status-banner", "login-form"} <= html_ids


def test_qa_spa_only_talks_to_the_influx_analytics_endpoints() -> None:
    """app.js must not read the SQL source of truth nor any non-analytics feed."""
    static_dir = Path(dashboard_views.__file__).parent / "static"
    app_js = (static_dir / "app.js").read_text(encoding="utf-8")
    for chart in CHARTS:
        assert f"/api/analytics/{chart}" in app_js, chart
    assert "/api/records" not in app_js
    assert "/api/import" not in app_js
    assert "/api/auth/me" in app_js
    feeds = sorted(set(re.findall(r'"(/api/[^"?]+)', app_js)))
    assert set(feeds) <= {
        "/api/analytics/mood-over-time",
        "/api/analytics/mood-distribution",
        "/api/analytics/heatmap",
        "/api/analytics/scales-trend",
        "/api/analytics/activities-top",
        "/api/analytics",
        "/api/auth/me",
        "/api/auth/login",
        "/api/auth/logout",
        "/api/users",
    }, feeds


def test_qa_spa_renders_a_503_banner_and_never_a_sql_fallback() -> None:
    """The SPA treats 503 as a hard error (R4), it does not retry elsewhere."""
    app_js = (Path(dashboard_views.__file__).parent / "static" / "app.js").read_text("utf-8")
    assert "503" in app_js and "status === 503" in app_js
    assert "setInfluxStatus(false)" in app_js
