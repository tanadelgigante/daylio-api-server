"""Fase 6 acceptance — dashboard SPA + Influx-only analytics API (R4/D4).

=============================== MAINTENANCE NOTES ===============================
Module   : tests/integration/test_dashboard_api.py
Scope    : WORKPLAN §7 FASE 6 (Tasks 6.1/6.2/6.3) + §5.6/§5.7. Pins:

  1. ``GET /dashboard`` serves the SPA shell referencing the vendored Chart.js
     and ``app.js``; the static assets are really served by Flask.
  2. Every ``/api/analytics/*`` endpoint answers 200 with the documented JSON
     shape, for a known dataset, with INCLUSIVE ``start_date``/``end_date``
     filters, and returns empty (never 500) for a user without data.
  3. Validation: bad date format / start > end / bad ``limit`` / bad
     ``granularity`` / bad ``metric`` -> uniform 400.
  4. Auth (D6): 401 anonymous, 403 for a non-admin asking another user,
     admin can scope to any user and sees THAT user's data.
  5. **Influx-only (R4/D4)**: a sqlite3 trace-callback spy proves no analytics
     request touches ``day_entries``, the Influx fake records a Flux query for
     every call, and a source-level check pins that ``dashboard/views.py`` does
     not import ``db.sqlite_store`` at all.
  6. 503 when InfluxDB is not usable (no store / ping False), with a message
     that never suggests a SQL fallback.

Seed data: synthetic daily points (Mon..Sat of 2026-01-05..10) written through
the real ``InfluxStore.write_entry`` (delete-and-write, §5.4), so the tests use
the exact point contract the real server produces. A second user ("bob") has
his own points to prove the ``?user=`` scoping.
================================================================================
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest

from daylio_api.app import create_app
from daylio_api.dashboard import views as dashboard_views
from daylio_api.db.influx_store import (
    InfluxConfig,
    InfluxStore,
    build_daily_point,
    pivot_daily_points,
)
from tests.fakes import FakeInfluxClient
from tests.helpers import login

#: Seed dataset: Mon 05 .. Sat 10 January 2026 (6 days, one per weekday+).
#: ``(full_date, mood, mood_label, activities, stress_rank, salute_label)``
SEED_DAYS: tuple[tuple[str, int, str, list[str], int | None, str | None], ...] = (
    ("2026-01-05", 1, "alla grande", ["run", "ottimo"], 3, "ottimo"),
    ("2026-01-06", 2, "bene", ["run", "study"], None, None),
    ("2026-01-07", 4, "male", ["discreto"], 4, "discreto"),
    ("2026-01-08", 3, "così così", [], 2, "sufficiente"),
    ("2026-01-09", 5, "malissimo", ["study"], None, None),
    ("2026-01-10", 2, "bene", ["ottimo", "run"], None, "ottimo"),
)

#: Bob owns a single far-away day, used by the cross-user/403 assertions.
BOB_DAYS: tuple[tuple[str, int, str, list[str], int | None, str | None], ...] = (
    ("2026-02-02", 1, "alla grande", ["swim"], 1, "ottimo"),
)

_STRESS_LABELS = {1: "Calma", 2: "Basso", 3: "Moderato", 4: "Alto", 5: "Sopraffatto"}
_SALUTE_RANKS = {"ottimo": 1, "buono": 2, "discreto": 3, "sufficiente": 4}


def _record(
    full_date: str,
    mood: int,
    mood_label: str,
    activities: list[str],
    stress_rank: int | None,
    salute_label: str | None,
) -> dict[str, Any]:
    """Build one canonical record (the shape ``build_daily_point`` consumes)."""
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
    """Write the synthetic days for ``user`` through the real write path."""
    for full_date, mood, label, activities, stress, salute in days:
        store.write_entry(user, _record(full_date, mood, label, activities, stress, salute))


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def fake_client() -> FakeInfluxClient:
    """In-memory InfluxDB (Fase 3 fake, extended with the query trail)."""
    return FakeInfluxClient()


@pytest.fixture
def influx(fake_client: FakeInfluxClient) -> InfluxStore:
    """InfluxStore bound to the fake client (same injection as the imports)."""
    return InfluxStore(
        InfluxConfig(
            url="http://fake-influx:8120",
            token="fake-token",
            org="tanadelgigante",
            bucket="daylio",
            measurement="daylio_entry",
        ),
        client=fake_client,
    )


@pytest.fixture
def app(tmp_path: Path, monkeypatch, influx: InfluxStore) -> Any:
    """Test app with a temp SQLite DB and the fake-backed Influx store.

    The Influx store is injected through ``app.extensions["daylio_influx"]``,
    the same seam the app factory uses, so NO network is ever touched (R8).
    """
    monkeypatch.setenv("DB_PATH", str(tmp_path / "dashboard.db"))
    application = create_app()
    application.config["TESTING"] = True
    application.extensions["daylio_influx"] = influx
    _seed(influx, "admin", SEED_DAYS)
    _seed(influx, "bob", BOB_DAYS)
    return application


@pytest.fixture
def client(app: Any) -> Any:
    """Flask test client logged in as the boot admin."""
    test_client = app.test_client()
    assert login(test_client).status_code == 200
    return test_client


def _create_user(app: Any, username: str, *, is_admin: bool = False) -> None:
    """Create a user directly in SQLite (faster than driving the admin API)."""
    from daylio_api.api.auth import hash_password
    from daylio_api.db.sqlite_store import create_user as insert_user

    conn = app.extensions["daylio_store"].connect()
    insert_user(conn, username, password_hash=hash_password("secret-123"), is_admin=is_admin)
    conn.commit()


def _login_as(app: Any, username: str) -> Any:
    """Fresh test client authenticated as ``username``."""
    test_client = app.test_client()
    resp = test_client.post(
        "/api/auth/login", json={"username": username, "password": "secret-123"}
    )
    assert resp.status_code == 200, resp.get_json()
    return test_client


# --------------------------------------------------------------------------- #
# 6.2 — the SPA shell and its static assets
# --------------------------------------------------------------------------- #
def test_dashboard_serves_spa_referencing_vendor_and_app(client) -> None:
    """GET /dashboard -> 200 HTML wiring vendor/chart.umd.min.js + app.js."""
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "text/html" in resp.headers["Content-Type"]
    assert "/dashboard/static/vendor/chart.umd.min.js" in body
    assert "/dashboard/static/app.js" in body


def test_dashboard_trailing_slash_also_serves_spa(client) -> None:
    """Both spellings answer 200 (no redirect for the SPA entry point)."""
    assert client.get("/dashboard/").status_code == 200


def test_dashboard_is_public_without_session(app) -> None:
    """The SPA shell is public: the page itself only shows the login form."""
    assert app.test_client().get("/dashboard").status_code == 200


@pytest.mark.parametrize(
    "asset",
    [
        "/dashboard/static/app.js",
        "/dashboard/static/app.css",
        "/dashboard/static/vendor/chart.umd.min.js",
    ],
)
def test_dashboard_static_assets_are_served(client, asset: str) -> None:
    """Chart.js (vendored) and the app assets are served by Flask itself."""
    resp = client.get(asset)
    assert resp.status_code == 200
    assert resp.get_data()


def test_vendored_chartjs_is_a_real_chartjs_build() -> None:
    """The vendored file is the real Chart.js UMD build, not a stub."""
    vendor = Path(dashboard_views.__file__).parent / "static" / "vendor" / "chart.umd.min.js"
    assert vendor.exists(), "vendor/chart.umd.min.js must be committed (D13)"
    content = vendor.read_text(encoding="utf-8")
    assert "Chart.js" in content
    assert len(content) > 100_000  # a real minified build, not a placeholder


def test_spa_source_calls_only_influx_analytics_endpoints() -> None:
    """app.js talks to /api/analytics/* (+ auth) and never to /api/records."""
    static_dir = Path(dashboard_views.__file__).parent / "static"
    app_js = (static_dir / "app.js").read_text(encoding="utf-8")
    index = (static_dir / "index.html").read_text(encoding="utf-8")
    assert "/api/analytics/mood-over-time" in app_js
    assert "/api/analytics/mood-distribution" in app_js
    assert "/api/analytics/heatmap" in app_js
    assert "/api/analytics/scales-trend" in app_js
    assert "/api/analytics/activities-top" in app_js
    assert "/api/records" not in app_js  # SQL source of truth stays out of the UI
    assert "/api/auth/me" in app_js
    assert "vendor/chart.umd.min.js" in index


# --------------------------------------------------------------------------- #
# 6.1 — mood-over-time
# --------------------------------------------------------------------------- #
def test_mood_over_time_daily_series(client) -> None:
    """Daily buckets: mean mood per day + per-level counts."""
    resp = client.get(
        "/api/analytics/mood-over-time?start_date=2026-01-05&end_date=2026-01-10"
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["user"] == "admin"
    assert data["granularity"] == "day"
    assert data["dates"] == [d[0] for d in SEED_DAYS]
    assert data["values"] == [1, 2, 4, 3, 5, 2]
    assert data["levels"][0] == {"1": 1, "2": 0, "3": 0, "4": 0, "5": 0}
    assert data["levels"][2] == {"1": 0, "2": 0, "3": 0, "4": 1, "5": 0}
    assert data["average"] == pytest.approx(2.83, abs=0.01)
    assert data["days"] == 6
    # ``buckets`` is the length of ``dates``: identical to ``days`` in day mode.
    assert data["buckets"] == 6 == len(data["dates"])


def test_mood_over_time_week_aggregation_and_auto_granularity(client) -> None:
    """A long range (or a long window) is aggregated per ISO week."""
    long_range = client.get(
        "/api/analytics/mood-over-time?start_date=2024-01-01&end_date=2026-01-10"
    ).get_json()
    assert long_range["granularity"] == "week"
    # Every seed day of 2026-01-05..10 belongs to the same ISO week.
    assert long_range["dates"] == ["2026-W02"]
    assert long_range["values"] == [pytest.approx(2.83, abs=0.01)]
    assert sum(entry["3"] for entry in long_range["levels"]) == 1
    # ``days`` = records read, ``buckets`` = plotted points: with a weekly
    # granularity the 6 days collapse into 1 bucket, and the two numbers say so.
    assert long_range["days"] == 6
    assert long_range["buckets"] == 1 == len(long_range["dates"])

    forced = client.get(
        "/api/analytics/mood-over-time?start_date=2024-01-01&end_date=2026-01-10"
        "&granularity=day"
    ).get_json()
    assert forced["granularity"] == "day"
    assert len(forced["dates"]) == 6
    assert forced["buckets"] == 6


def test_mood_over_time_buckets_exclude_days_without_a_mood(app, client) -> None:
    """A point read from Influx but without a resolvable mood is not a bucket.

    ``days`` counts the record (it WAS read), ``buckets`` counts the plotted
    points, so the two differ exactly by the unresolved days.
    """
    point = build_daily_point("admin", _record("2026-01-11", 0, "", [], None, None))
    app.extensions["daylio_influx"].client().write(bucket="daylio", record=point)
    data = client.get(
        "/api/analytics/mood-over-time?start_date=2026-01-11&end_date=2026-01-11"
    ).get_json()
    assert data["days"] == 1
    assert data["buckets"] == 0
    assert data["dates"] == [] and data["values"] == [] and data["average"] is None


def test_mood_over_time_range_is_inclusive(client) -> None:
    """start_date/end_date are inclusive on both ends."""
    data = client.get(
        "/api/analytics/mood-over-time?start_date=2026-01-06&end_date=2026-01-08"
    ).get_json()
    assert data["dates"] == ["2026-01-06", "2026-01-07", "2026-01-08"]
    assert data["days"] == 3


# --------------------------------------------------------------------------- #
# 6.1 — mood-distribution
# --------------------------------------------------------------------------- #
def test_mood_distribution_counts_per_level(client) -> None:
    """Counts per mood level 1..5 with the labels from the Influx tags."""
    data = client.get(
        "/api/analytics/mood-distribution?start_date=2026-01-05&end_date=2026-01-10"
    ).get_json()
    assert data["levels"] == [1, 2, 3, 4, 5]
    assert data["labels"][0] == "alla grande"
    assert data["labels"][4] == "malissimo"
    assert data["counts"] == [1, 2, 1, 1, 1]
    assert data["total"] == 6
    assert data["average"] == pytest.approx(2.83, abs=0.01)


# --------------------------------------------------------------------------- #
# 6.1 — heatmap
# --------------------------------------------------------------------------- #
def test_heatmap_matrix_is_7_weekdays_by_24_hours(client) -> None:
    """Weekday (Mon-first) x hour matrix with mean mood and record counts."""
    data = client.get(
        "/api/analytics/heatmap?start_date=2026-01-05&end_date=2026-01-10"
    ).get_json()
    assert data["metric"] == "mean_mood"
    assert data["weekdays"] == ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    assert data["buckets"] == [f"{hour:02d}" for hour in range(24)]
    assert len(data["data"]) == 7 and len(data["data"][0]) == 24
    assert len(data["counts"]) == 7 and len(data["counts"][0]) == 24
    # Monday (index 0) holds 2026-01-05 with mood 1, in the 00:00 bucket (§5.4).
    assert data["counts"][0][0] == 1
    assert data["data"][0][0] == 1
    # Sunday (index 6) has no record in the range.
    assert data["counts"][6] == [0] * 24
    assert data["data"][6] == [None] * 24
    assert data["total"] == 6


def test_heatmap_count_metric(client) -> None:
    """``metric=count`` returns the number of records per cell."""
    data = client.get(
        "/api/analytics/heatmap?start_date=2026-01-05&end_date=2026-01-10&metric=count"
    ).get_json()
    assert data["metric"] == "count"
    assert data["data"][0][0] == 1.0
    assert data["data"][6][0] == 0.0


# --------------------------------------------------------------------------- #
# 6.1 — scales-trend
# --------------------------------------------------------------------------- #
def test_scales_trend_single_scale_keeps_missing_days_as_null(client) -> None:
    """A scale trend flags the days without the scale instead of faking a 0."""
    data = client.get(
        "/api/analytics/scales-trend?scale=stress&start_date=2026-01-05&end_date=2026-01-10"
    ).get_json()
    assert data["scale_name"] == "stress"
    assert data["scale_label"] == "Stress"
    assert data["dates"] == [d[0] for d in SEED_DAYS]
    assert data["values"] == [3, None, 4, 2, None, None]
    assert data["labels"] == ["Moderato", None, "Alto", "Basso", None, None]
    assert data["days_with_value"] == 3


def test_scales_trend_display_name_is_accepted(client) -> None:
    """``?scale=Scala salute`` (display name, spaces, case-insensitive)."""
    data = client.get(
        "/api/analytics/scales-trend?scale=Scala%20salute"
        "&start_date=2026-01-05&end_date=2026-01-10"
    ).get_json()
    assert data["scale_name"] == "scala_salute"
    assert data["scale_label"] == "Scala salute"
    assert data["values"] == [1, None, 3, 4, None, 1]
    assert data["labels"][0] == "ottimo"
    assert data["days_with_value"] == 4


def test_scales_trend_without_filter_returns_every_scale(client) -> None:
    """Without ``?scale=`` every scale found in the range is returned."""
    data = client.get(
        "/api/analytics/scales-trend?start_date=2026-01-05&end_date=2026-01-10"
    ).get_json()
    names = [item["scale_name"] for item in data["scales"]]
    assert names == ["scala_salute", "stress"]
    stress = data["scales"][1]
    assert stress["days_with_value"] == 3


# --- BUG-1 (QA round 2): an unknown ?scale= must be a 400, not an empty chart --
@pytest.mark.parametrize(
    "value", ["not-a-scale", "stresso", "scala salute 2", "scala_salute_x", "0"]
)
def test_scales_trend_unknown_scale_is_400(client, value: str) -> None:
    """An invented ``?scale=`` is a uniform 400 naming the available scales."""
    resp = client.get(
        f"/api/analytics/scales-trend?scale={value}"
        "&start_date=2026-01-05&end_date=2026-01-10"
    )
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["type"] == "bad_request"
    assert value in body["error"]
    # The message helps the caller: it lists what can be requested.
    assert "stress" in body["error"] and "scala_salute" in body["error"]


@pytest.mark.parametrize("value", ["", "%20%20", "none"])
def test_scales_trend_absent_or_blank_scale_returns_every_scale(client, value: str) -> None:
    """``?scale=`` missing/blank is the "all scales" case (``none`` is unknown)."""
    resp = client.get(
        f"/api/analytics/scales-trend?scale={value}"
        "&start_date=2026-01-05&end_date=2026-01-10"
    )
    if value == "none":
        assert resp.status_code == 400
    else:
        assert resp.status_code == 200
        assert [item["scale_name"] for item in resp.get_json()["scales"]] == [
            "scala_salute",
            "stress",
        ]


def _write_raw_point(
    app: Any, user: str, record: dict[str, Any], extra_fields: dict[str, Any]
) -> None:
    """Write a §5.4 point with hand-added fields (a scale the config doesn't know)."""
    point = build_daily_point(user, record)
    point["fields"].update(extra_fields)
    app.extensions["daylio_influx"].client().write(bucket="daylio", record=point)


def test_scales_trend_accepts_a_custom_scale_found_only_in_the_data(client, app) -> None:
    """A per-user scale absent from the config is still reachable (200).

    §5.4 tolerance: a point may carry a scale that is no longer configured, and
    the SPA's scale selector is built from the data — rejecting it would make
    the chart unreachable.
    """
    _write_raw_point(
        app,
        "admin",
        _record("2026-01-07", 3, "così così", [], None, None),
        {
            "scale_energia_value": 4,
            "scales": json.dumps(
                [
                    {
                        "scale_name": "Energia",
                        "label": "Bassa",
                        "rank": 4,
                        "scale_key": "energia",
                    }
                ]
            ),
        },
    )

    # By its config key...
    by_key = client.get(
        "/api/analytics/scales-trend?scale=energia"
        "&start_date=2026-01-05&end_date=2026-01-10"
    )
    assert by_key.status_code == 200, by_key.get_json()
    data = by_key.get_json()
    assert data["scale_name"] == "energia"
    assert data["values"] == [None, None, 4, None, None, None]
    assert data["labels"][2] == "Bassa"
    # ...and by the display label stored in the point payload.
    by_label = client.get(
        "/api/analytics/scales-trend?scale=Energia"
        "&start_date=2026-01-05&end_date=2026-01-10"
    )
    assert by_label.status_code == 200
    assert by_label.get_json()["scale_name"] == "energia"


def test_scales_trend_custom_scale_of_another_user_stays_unknown(client, app) -> None:
    """The data-based fallback is scoped: bob's custom scale is not admin's."""
    _write_raw_point(
        app,
        "bob",
        _record("2026-01-07", 3, "così così", [], None, None),
        {"scale_energia_value": 4},
    )
    assert (
        client.get(
            "/api/analytics/scales-trend?scale=energia"
            "&start_date=2026-01-05&end_date=2026-01-10"
        ).status_code
        == 400
    )
    # But bob can still read his own.
    assert (
        client.get(
            "/api/analytics/scales-trend?user=bob&scale=energia"
            "&start_date=2026-01-05&end_date=2026-01-10"
        ).status_code
        == 200
    )


# --------------------------------------------------------------------------- #
# 6.1 — activities-top
# --------------------------------------------------------------------------- #
def test_activities_top_ranking_excludes_scala_salute_levels(client) -> None:
    """D2: the health scale levels are not activities (top-N, ties sorted)."""
    data = client.get(
        "/api/analytics/activities-top?start_date=2026-01-05&end_date=2026-01-10"
    ).get_json()
    assert data["excluded_scale"] == "scala_salute"
    assert data["activities"] == [
        {"activity": "run", "count": 3},
        {"activity": "study", "count": 2},
    ]
    assert data["distinct"] == 2
    assert data["days"] == 6


def test_activities_top_limit_is_validated_and_applied(client) -> None:
    """``?limit=`` narrows the ranking; out-of-range values are a clean 400."""
    data = client.get(
        "/api/analytics/activities-top?start_date=2026-01-05&end_date=2026-01-10&limit=1"
    ).get_json()
    assert data["limit"] == 1
    assert data["activities"] == [{"activity": "run", "count": 3}]

    for bad in ("0", "51", "abc"):
        resp = client.get(f"/api/analytics/activities-top?limit={bad}")
        assert resp.status_code == 400, bad
        assert resp.get_json()["type"] == "bad_request"


# --------------------------------------------------------------------------- #
# 6.3 — validation, empty results, unknown chart
# --------------------------------------------------------------------------- #
ANALYTICS_ENDPOINTS = (
    "mood-over-time",
    "mood-distribution",
    "heatmap",
    "scales-trend",
    "activities-top",
)


def test_unknown_chart_is_404_json(client) -> None:
    """A typo in the chart name answers the uniform 404 JSON envelope."""
    resp = client.get("/api/analytics/pie-of-doom")
    assert resp.status_code == 404
    assert resp.get_json()["type"] == "not_found"
    assert resp.get_json()["error"]


def test_an_influx_query_failure_is_503(client, fake_client) -> None:
    """A raising Flux query is a 503, never a 500 and never a SQLite read."""
    boom = RuntimeError("connection reset by peer")

    def broken_query(flux: str) -> list[dict[str, Any]]:
        raise boom

    fake_client.query = broken_query  # type: ignore[method-assign]
    resp = client.get("/api/analytics/mood-over-time")
    assert resp.status_code == 503
    data = resp.get_json()
    assert data["type"] == "service_unavailable"
    assert "temporarily unavailable" in data["error"]
    # The internal cause is logged, never leaked to the client.
    assert "connection reset" not in json.dumps(data)


def test_a_failing_chart_answers_json_500_not_html(client, app, monkeypatch) -> None:
    """An unexpected failure inside a chart must not leak an HTML error page.

    The app-level 500 handler owned by ``api/routes.py`` covers the whole app
    (the dashboard blueprint deliberately registers no competing one). The bug
    is injected in the *aggregation* layer, past the read that maps to 503.
    """

    def boom(*args: Any, **kwargs: Any) -> dict[int, int]:
        raise ValueError("kaboom")

    monkeypatch.setattr(dashboard_views, "_level_counts", boom)
    # TESTING propagates exceptions instead of calling the error handler, so
    # switch it off to exercise the real 500 path.
    monkeypatch.setitem(app.config, "TESTING", False)
    resp = client.get("/api/analytics/mood-over-time")
    assert resp.status_code == 500
    assert resp.is_json
    assert resp.get_json() == {"error": "internal server error", "type": "internal_error"}


@pytest.mark.parametrize("chart", ANALYTICS_ENDPOINTS)
def test_empty_range_returns_empty_shapes(client, chart: str) -> None:
    """A window without records answers 200 with empty arrays, never 500."""
    data = client.get(
        f"/api/analytics/{chart}?start_date=2030-01-01&end_date=2030-01-31"
    ).get_json()
    assert data["user"] == "admin"
    if chart == "mood-over-time":
        assert data["dates"] == [] and data["values"] == [] and data["average"] is None
    elif chart == "mood-distribution":
        assert data["counts"] == [0, 0, 0, 0, 0] and data["total"] == 0
    elif chart == "heatmap":
        assert data["total"] == 0 and data["data"][0][0] is None
    elif chart == "scales-trend":
        assert data["scales"] == []
    else:
        assert data["activities"] == []


@pytest.mark.parametrize("chart", ANALYTICS_ENDPOINTS)
def test_invalid_date_range_is_400(client, chart: str) -> None:
    """Both bounds are validated: bad format and start > end -> 400."""
    bad_format = client.get(f"/api/analytics/{chart}?start_date=05-01-2026")
    assert bad_format.status_code == 400
    assert bad_format.get_json()["type"] == "bad_request"

    reversed_range = client.get(
        f"/api/analytics/{chart}?start_date=2026-02-01&end_date=2026-01-01"
    )
    assert reversed_range.status_code == 400
    assert "start_date" in reversed_range.get_json()["error"]


def test_unknown_granularity_and_metric_are_400(client) -> None:
    """Enum query params are validated instead of being silently ignored."""
    assert client.get("/api/analytics/mood-over-time?granularity=hourly").status_code == 400
    assert client.get("/api/analytics/heatmap?metric=median").status_code == 400


def test_unknown_chart_returns_json_404(client) -> None:
    """An unknown analytics chart is a JSON 404 (never an HTML page)."""
    resp = client.get("/api/analytics/does-not-exist")
    assert resp.status_code == 404
    assert resp.get_json()["type"] == "not_found"


def test_analytics_index_lists_the_charts(client) -> None:
    """The self-describing index tells the SPA what it can render."""
    data = client.get("/api/analytics").get_json()
    assert {item["chart"] for item in data["charts"]} == set(ANALYTICS_ENDPOINTS)
    assert data["source"] == "influxdb"


# --------------------------------------------------------------------------- #
# 6.3 — auth, isolation and the admin ?user= filter (D6)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("chart", ANALYTICS_ENDPOINTS)
def test_analytics_requires_login(app, chart: str) -> None:
    """Anonymous callers get the uniform 401 JSON, never data."""
    resp = app.test_client().get(f"/api/analytics/{chart}")
    assert resp.status_code == 401
    assert resp.get_json()["type"] == "unauthorized"


def test_analytics_requires_login_for_the_index(app) -> None:
    """The chart index is protected too."""
    assert app.test_client().get("/api/analytics").status_code == 401


def test_non_admin_cannot_read_another_user_analytics(app) -> None:
    """A non-admin viewer is scoped to themselves: 403 on ?user=other."""
    _create_user(app, "alice")
    alice = _login_as(app, "alice")
    own = alice.get("/api/analytics/mood-distribution")
    assert own.status_code == 200
    assert own.get_json()["user"] == "alice"
    assert own.get_json()["total"] == 0

    for chart in ANALYTICS_ENDPOINTS:
        resp = alice.get(f"/api/analytics/{chart}?user=admin")
        assert resp.status_code == 403, chart
        assert resp.get_json()["type"] == "forbidden"


def test_admin_user_filter_returns_that_users_data(client) -> None:
    """An admin may scope to any user and gets THAT user's series."""
    data = client.get(
        "/api/analytics/mood-distribution?user=bob&start_date=2026-02-01&end_date=2026-02-28"
    ).get_json()
    assert data["user"] == "bob"
    assert data["total"] == 1
    assert data["counts"] == [1, 0, 0, 0, 0]

    mine = client.get(
        "/api/analytics/mood-distribution?user=admin&start_date=2026-01-05&end_date=2026-01-10"
    ).get_json()
    assert mine["user"] == "admin"
    assert mine["total"] == 6


def test_admin_filter_isolates_activities_per_user(client) -> None:
    """The top-activities ranking is per user (bob only has ``swim``)."""
    data = client.get("/api/analytics/activities-top?user=bob").get_json()
    assert data["activities"] == [{"activity": "swim", "count": 1}]


def test_unknown_user_returns_empty_not_error(client) -> None:
    """A valid username without Influx data answers 200 + empty shapes."""
    data = client.get("/api/analytics/mood-over-time?user=ghost").get_json()
    assert data["user"] == "ghost"
    assert data["dates"] == [] and data["values"] == []


# --- BUG-2 (QA round 2): an empty ?user= is an ABSENT filter, not username "" --
@pytest.mark.parametrize("chart", ANALYTICS_ENDPOINTS)
@pytest.mark.parametrize("query", ["user=", "user=%20%20"])
def test_empty_user_param_falls_back_to_the_viewer(client, chart: str, query: str) -> None:
    """``?user=`` (empty or blank) must scope to the viewer, never to ``""``."""
    data = client.get(
        f"/api/analytics/{chart}?{query}&start_date=2026-01-05&end_date=2026-01-10"
    ).get_json()
    assert data["user"] == "admin", chart


def test_empty_user_param_on_records_falls_back_to_the_viewer(app, client) -> None:
    """The SQL records API shares ``_effective_user``: same fallback, same fix."""
    from daylio_api.db.sqlite_store import upsert_entry

    conn = app.extensions["daylio_store"].connect()
    upsert_entry(conn, "admin", _record("2026-01-07", 3, "così così", ["run"], None, None))
    conn.commit()

    # The canonical record and the envelope have no ``user`` key, so the
    # resolved scope is observed through WHICH rows come back.
    data = client.get("/api/records?user=")
    assert data.status_code == 200
    body = data.get_json()
    assert body["total"] == 1
    assert body["records"][0]["full_date"] == "2026-01-07"
    # Whitespace-only and absent are the same "no filter" case.
    blank = client.get("/api/records?user=%20%20").get_json()
    assert blank["total"] == 1
    assert blank["records"][0]["full_date"] == "2026-01-07"
    # An explicit other user is still scoped (and empty for a data-less one).
    assert client.get("/api/records?user=ghost").get_json()["total"] == 0


def test_empty_user_param_is_not_a_403_for_a_non_admin(app) -> None:
    """A viewer sending ``?user=`` gets their own scope, not a 403."""
    from daylio_api.db.sqlite_store import upsert_entry

    _create_user(app, "alice")
    conn = app.extensions["daylio_store"].connect()
    upsert_entry(conn, "alice", _record("2026-01-07", 3, "così così", ["run"], None, None))
    conn.commit()
    alice = _login_as(app, "alice")
    resp = alice.get("/api/analytics/mood-distribution?user=")
    assert resp.status_code == 200
    assert resp.get_json()["user"] == "alice"
    records = alice.get("/api/records?user=").get_json()
    assert records["total"] == 1
    assert records["records"][0]["full_date"] == "2026-01-07"
    # A REAL other username is still forbidden.
    assert alice.get("/api/analytics/mood-distribution?user=admin").status_code == 403
    assert alice.get("/api/records?user=admin").status_code == 403


def test_admin_can_still_scope_to_another_user_with_the_explicit_filter(client) -> None:
    """The fix only touches the empty value: explicit scoping is unchanged."""
    data = client.get(
        "/api/analytics/mood-over-time?user=bob&start_date=2026-02-01&end_date=2026-02-28"
    ).get_json()
    assert data["user"] == "bob" and data["values"] == [1.0]


# --------------------------------------------------------------------------- #
# 6.3 — R4/D4: analytics read InfluxDB and NEVER SQLite
# --------------------------------------------------------------------------- #
def test_analytics_never_touches_sqlite(client, app) -> None:
    """No analytics request may read the SQL source of truth (R4/D4).

    Spy: the app's sqlite3 connection gets a trace callback that records EVERY
    statement. Identity resolution (``api/auth.py``) legitimately reads the
    ``users`` table once per request; the record table ``day_entries`` must stay
    untouched, and the Influx fake must see a Flux query for each chart.
    """
    conn = app.extensions["daylio_store"].connect()
    statements: list[str] = []
    conn.set_trace_callback(statements.append)

    for chart in ANALYTICS_ENDPOINTS:
        del statements[:]
        resp = client.get(f"/api/analytics/{chart}?start_date=2026-01-01&end_date=2026-01-31")
        assert resp.status_code == 200, chart
        assert statements, f"{chart} should resolve the session identity"
        # Only the session identity may hit SQL — never the record table.
        assert [stmt for stmt in statements if "day_entries" in stmt] == [], chart


def test_analytics_never_touches_sqlite_even_for_a_failing_influx(
    app, fake_client
) -> None:
    """A 503 path is just as SQL-free: no silent source-of-truth fallback."""
    conn = app.extensions["daylio_store"].connect()
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    fake_client.available = False
    client = app.test_client()
    assert login(client).status_code == 200
    del statements[:]
    assert client.get("/api/analytics/mood-over-time").status_code == 503
    assert [stmt for stmt in statements if "day_entries" in stmt] == []


def test_analytics_issues_influx_queries_only(client, fake_client) -> None:
    """Every analytics call goes through the Influx store (Flux query trail)."""
    before = len(fake_client.queries)
    client.get("/api/analytics/mood-over-time?start_date=2026-01-05&end_date=2026-01-10")
    issued = fake_client.queries[before:]
    assert len(issued) == 1
    assert 'from(bucket: "daylio")' in issued[0]
    assert 'r._measurement == "daylio_entry"' in issued[0]
    assert 'r.user == "admin"' in issued[0]
    # The range bounds are pushed down to the Flux query (2026-01-01..2026-01-31).
    assert "2026-01-05T00:00:00Z" in issued[0]
    assert "2026-01-11T00:00:00Z" in issued[0]


def test_dashboard_module_never_imports_the_sql_store() -> None:
    """Static guarantee: the analytics module has no SQLite import at all.

    Parsed with :mod:`ast` (not a substring match) so the MAINTENANCE NOTES may
    keep naming the module while the code never imports it.
    """
    source = Path(dashboard_views.__file__).read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert not any("sqlite" in name for name in imported), imported
    # The only storage it touches is the Influx store.
    assert "daylio_api.db.influx_store" in imported
    assert "read_daily_points" in source


# --------------------------------------------------------------------------- #
# 6.1 — 503 when InfluxDB is not usable (R8 tolerance, never a SQL fallback)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("chart", ANALYTICS_ENDPOINTS)
def test_analytics_503_without_influx_store(app, chart: str) -> None:
    """No Influx store configured -> 503 with a clear, explicit message."""
    app.extensions["daylio_influx"] = None
    client = app.test_client()
    assert login(client).status_code == 200
    resp = client.get(f"/api/analytics/{chart}")
    assert resp.status_code == 503
    body = resp.get_json()
    assert body["type"] == "service_unavailable"
    assert "Influx" in body["error"]


@pytest.mark.parametrize("chart", ANALYTICS_ENDPOINTS)
def test_analytics_503_when_influx_ping_fails(app, fake_client, chart: str) -> None:
    """An unreachable InfluxDB (ping False) is a 503, not an empty chart."""
    fake_client.available = False
    client = app.test_client()
    assert login(client).status_code == 200
    resp = client.get(f"/api/analytics/{chart}")
    assert resp.status_code == 503
    assert "Influx" in resp.get_json()["error"]


def test_503_does_not_fall_back_to_sqlite(app, fake_client) -> None:
    """Even with data in SQLite, an unavailable Influx answers 503 (R4/D4)."""
    from daylio_api.db.sqlite_store import upsert_entry

    conn = app.extensions["daylio_store"].connect()
    upsert_entry(conn, "admin", _record("2026-03-01", 1, "alla grande", ["run"], None, None))
    conn.commit()

    fake_client.available = False
    client = app.test_client()
    assert login(client).status_code == 200
    assert client.get("/api/analytics/mood-distribution").status_code == 503
    # The SQL endpoint is unaffected: SQLite is still the source of truth (D4).
    assert client.get("/api/records").get_json()["total"] == 1


# --------------------------------------------------------------------------- #
# Pivot contract (db/influx_store.py) — the read abstraction behind R4
# --------------------------------------------------------------------------- #
def test_pivot_regroups_flux_records_into_daily_points() -> None:
    """One FluxRecord per FIELD is regrouped into one point per (user, date)."""
    rows = [
        {
            "_time": "2026-01-05T00:00:00Z",
            "_field": "mood",
            "_value": 2,
            "user": "admin",
            "date": "2026-01-05",
            "mood_label": "bene",
        },
        {
            "_time": "2026-01-05T00:00:00Z",
            "_field": "scale_stress_value",
            "_value": 3,
            "user": "admin",
            "date": "2026-01-05",
            "mood_label": "bene",
        },
        {
            "_time": "2026-01-06T00:00:00Z",
            "_field": "mood",
            "_value": 5,
            "user": "admin",
            "date": "2026-01-06",
            "mood_label": "malissimo",
        },
    ]
    points = pivot_daily_points(rows)
    assert [point["date"] for point in points] == ["2026-01-05", "2026-01-06"]
    assert points[0]["fields"] == {"mood": 2, "scale_stress_value": 3}
    assert points[0]["tags"]["mood_label"] == "bene"
    assert points[1]["fields"]["mood"] == 5


def test_pivot_accepts_whole_point_dicts(fake_client) -> None:
    """The fake returns whole §5.4 points: the pivot uses them as they are."""
    influx = InfluxStore(
        InfluxConfig(url="http://fake:8120", token="t"),
        client=fake_client,
    )
    influx.write_entry("alice", _record("2026-04-01", 3, "così così", ["walk"], 1, None))
    points = pivot_daily_points(fake_client.query('from(bucket: "daylio")'))
    assert len(points) == 1
    assert points[0]["user"] == "alice"
    assert points[0]["date"] == "2026-04-01"
    assert points[0]["fields"]["mood"] == 3
    assert json.loads(points[0]["fields"]["activities"]) == ["walk"]


def test_read_daily_points_filters_user_and_date_range(influx) -> None:
    """read_daily_points honours user + inclusive bounds (fake-safe)."""
    _seed(influx, "admin", SEED_DAYS)
    _seed(influx, "bob", BOB_DAYS)
    days = influx.read_daily_points("admin", "2026-01-06", "2026-01-08")
    assert [point["date"] for point in days] == ["2026-01-06", "2026-01-07", "2026-01-08"]
    assert all(point["user"] == "admin" for point in days)
    assert influx.read_daily_points("ghost") == []
