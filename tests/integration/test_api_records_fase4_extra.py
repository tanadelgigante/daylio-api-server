"""Fase 4 extra coverage (Tester independent check, only tests).

Covers the acceptance-criteria corners that ``test_api_records.py`` does not
exercise directly:

  - combined params on /api/records (columns + start_date/end_date + page +
    limit + user applied at the same time);
  - canonical extra columns selection (mood_label, scala_salute, source,
    created_at, updated_at);
  - limit=0 / negative limit / negative page -> 400;
  - /api/records/<full_date> 404 shape (documents the actual wire contract);
  - 500 handler hides the traceback (non-TESTING client, patched store);
  - legacy /moods ordering and multiple rows.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from daylio_api.api import routes as routes_mod
from daylio_api.api.routes import _CANONICAL_EXTRA_COLUMNS
from daylio_api.app import create_app
from tests.integration.test_api_records import SEED_END, SEED_START, _seed_sample


@pytest.fixture
def api_client(tmp_path: Path, monkeypatch, sample_csv, config_sample):
    """Flask test client backed by a freshly seeded temp SQLite database."""
    tmp_db = tmp_path / "api_records_test.db"
    _seed_sample(tmp_db, sample_csv, config_sample)
    monkeypatch.setenv("DB_PATH", str(tmp_db))
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


# --------------------------------------------------------------------------- #
# Combined parameters on /api/records
# --------------------------------------------------------------------------- #
def test_records_combined_params_all_applied(api_client) -> None:
    """columns + date range + page + limit + user all applied at once."""
    resp = api_client.get(
        "/api/records?user=default"
        f"&start_date={SEED_START}&end_date={SEED_END}"
        "&columns=full_date,mood,scales"
        "&page=2&limit=6"
    )
    assert resp.status_code == 200
    payload = resp.get_json()

    assert payload["total"] == 20            # entire seeded window
    assert payload["page"] == 2 and payload["limit"] == 6
    assert len(payload["records"]) == 6      # slice 6..11 of 20 (DESC)
    for record in payload["records"]:
        assert set(record) == {"full_date", "mood", "scales"}

    # Slices are contiguous over the DESC ordering.
    first = api_client.get(
        "/api/records?user=default"
        f"&start_date={SEED_START}&end_date={SEED_END}"
        "&columns=full_date,mood,scales&page=1&limit=6"
    ).get_json()["records"]
    all_dates = [r["full_date"] for r in first + payload["records"]]
    assert all_dates == sorted(all_dates, reverse=True)


def test_records_canonical_extra_columns_selection(api_client) -> None:
    """Canonical extras (mood_label/scala_salute/source/timestamps) selectable."""
    resp = api_client.get(
        "/api/records?user=default&start_date=2026-08-04&end_date=2026-08-04"
        "&columns=full_date,mood_label,scala_salute,source,created_at,updated_at"
    )
    assert resp.status_code == 200
    record = resp.get_json()["records"][0]
    assert set(record) == {
        "full_date", "mood_label", "scala_salute", "source", "created_at", "updated_at",
    }
    assert record["full_date"] == "2026-08-04"
    assert record["source"] == "csv"
    assert record["mood_label"] == "bene"


def test_records_canonical_extra_columns_all_selectable(api_client) -> None:
    """Every canonical extra (mood_label/scala_salute/source/timestamps) is an
    accepted selectable column through the real endpoint (no 400)."""
    for extra in _CANONICAL_EXTRA_COLUMNS:
        resp = api_client.get(
            f"/api/records?user=default&columns=full_date,{extra}&limit=1"
        )
        assert resp.status_code == 200, f"column {extra!r} rejected: {resp.get_json()}"
        record = resp.get_json()["records"][0]
        assert set(record) == {"full_date", extra}


# --------------------------------------------------------------------------- #
# Page / limit bracketing
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "query, message",
    [
        ("limit=0", "limit must be >= 1"),
        ("limit=-5", "limit must be >= 1"),
        ("page=-1", "page must be > 0"),
    ],
)
def test_records_boundary_params_400(api_client, query: str, message: str) -> None:
    """Negative limits and a negative page are rejected with a clear 400."""
    resp = api_client.get(f"/api/records?{query}")
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["type"] == "bad_request"
    assert message in body["error"]


# --------------------------------------------------------------------------- #
# 404 / 500 uniform error handling (criterion 5)
# --------------------------------------------------------------------------- #
def test_record_by_date_404_shape(api_client) -> None:
    """Missing date -> 404; documents the exact wire body (error + type).

    The uniform error shape (``{"error": str, "type": "not_found"}``) is
    guaranteed by the blueprint 404 handler (routes.py) and by the app-level
    fallback for unmatched routes (app.py). This test pins the contract.
    """
    resp = api_client.get("/api/records/1999-01-01")
    assert resp.status_code == 404
    body = resp.get_json()
    assert body.get("error") == "not_found"
    assert body.get("type") == "not_found"


def test_internal_error_hides_traceback(
    tmp_path: Path, monkeypatch, sample_csv, config_sample
) -> None:
    """A store failure yields the uniform 500 JSON, never a traceback.

    Uses a non-TESTING client so the app-level 500 handler runs (with
    TESTING=True Flask re-raises exceptions and the handler is bypassed).
    """
    # Seed a real db then break the service at the store boundary.
    tmp_db = tmp_path / "api_500.db"
    _seed_sample(tmp_db, sample_csv, config_sample)
    monkeypatch.setenv("DB_PATH", str(tmp_db))

    app = create_app()
    app.config["TESTING"] = False
    app.config["PROPAGATE_EXCEPTIONS"] = False
    client = app.test_client()

    def _boom() -> None:
        raise RuntimeError("simulated hidden failure 7f3a9")

    monkeypatch.setattr(routes_mod, "_store", _boom)

    resp = client.get("/api/records")
    assert resp.status_code == 500
    assert resp.get_json() == {
        "error": "internal server error",
        "type": "internal_error",
    }
    body = resp.get_data(as_text=True)
    assert "Traceback" not in body
    assert "7f3a9" not in body  # hidden detail must never leak


def test_unmatched_api_route_unknown_404(api_client) -> None:
    """GET /api/definitely-not-a-route -> 404 (body not asserted strictly)."""
    resp = api_client.get("/api/definitely-not-a-route")
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# Legacy /moods ordering + multi-row
# --------------------------------------------------------------------------- #
def test_legacy_moods_multiple_rows_ordered(api_client) -> None:
    """Legacy /moods returns one dict per day ordered by full_date, time."""
    resp = api_client.get(
        "/moods?user=default"
        f"&start_date={SEED_START}&end_date={SEED_END}"
    )
    assert resp.status_code == 200
    moods = resp.get_json()
    assert len(moods) == 20
    dates = [m["date"] for m in moods]
    assert dates == sorted(dates)
    for m in moods:
        assert set(m) == {"date", "time", "mood", "activities", "note_title", "note"}
