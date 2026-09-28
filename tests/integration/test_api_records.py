"""Fase 4 API tests: parametric records, legacy endpoints, import jobs (Task 4.4).

The suite seeds a deterministic 20-day subset (2026-08-01 .. 2026-08-20) of the
real 630-row sample into a temporary SQLite database via the Phase-3 pipeline
(``csv_importer`` → ``normalizer`` → ``import_flow.import_data``), then boots a
Flask app on that database (``DB_PATH`` env override) and exercises the Fase 4
endpoints against the real ``day_entries``/``import_jobs`` rows.

Coverage (WORKPLAN §7 FASE 4 / Task 4.4):
  - GET /api/records basic, column selection, date filters + validation,
    pagination, limit>1000 / page<=0 / unknown column -> 400;
  - GET /api/records/<full_date> -> canonical record incl. scales/scala_salute
    and 404 for a missing date;
  - GET /api/import/jobs -> at least one job after an import;
  - legacy GET /users and GET /moods with the EXACT old dict keys.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from daylio_api.app import create_app
from daylio_api.db.sqlite_store import SqliteStore
from daylio_api.import_flow import import_data
from daylio_api.importers.csv_importer import parse_csv
from daylio_api.importers.normalizer import normalize
from tests.helpers import login_assert_ok

#: Deterministic 20-day window taken from the real sample (all dates exist).
SEED_START = "2026-08-01"
SEED_END = "2026-08-20"
SEED_COUNT = 20


def _seed_sample(tmp_db: Path, sample_csv, config_sample) -> None:
    """Persist the 20-day subset into ``tmp_db`` (source of truth, D4)."""
    store = SqliteStore(str(tmp_db))
    conn = store.connect()
    result = parse_csv(sample_csv, config_sample)
    entries = normalize(result, config_sample)
    subset = [
        entry
        for entry in entries
        if SEED_START <= entry.full_date <= SEED_END
    ]
    assert len(subset) == SEED_COUNT, "expected exactly 20 seed dates"

    summary = import_data(
        conn,
        None,
        config_sample,
        "default",
        "upload",
        "subset_2026_08.csv",
        sorted(subset, key=lambda entry: entry.full_date),
    )
    assert summary.status == "completed"
    assert summary.imported == SEED_COUNT
    assert summary.errors == 0
    conn.close()
    store.close()


@pytest.fixture
def api_client(tmp_path: Path, monkeypatch, sample_csv, config_sample):
    """Flask test client backed by a freshly seeded temp SQLite database."""
    tmp_db = tmp_path / "api_records_test.db"
    _seed_sample(tmp_db, sample_csv, config_sample)
    monkeypatch.setenv("DB_PATH", str(tmp_db))
    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()
    # D6: the data routes require authentication; the admin may read any user.
    login_assert_ok(client)
    return client


# --------------------------------------------------------------------------- #
# GET /api/records — basic + column selection
# --------------------------------------------------------------------------- #
def test_records_basic_canonical_shape(api_client) -> None:
    """GET /api/records -> total 20, canonical keys, full_date DESC order."""
    resp = api_client.get("/api/records?user=default")
    assert resp.status_code == 200
    payload = resp.get_json()

    assert payload["total"] == SEED_COUNT
    assert payload["page"] == 1
    assert payload["limit"] == 100
    assert len(payload["records"]) == SEED_COUNT

    dates = [record["full_date"] for record in payload["records"]]
    assert dates == sorted(dates, reverse=True)
    assert dates[0] == SEED_END
    assert dates[-1] == SEED_START

    record = payload["records"][0]
    assert set(record) == {
        "full_date", "date", "weekday", "time", "mood", "mood_label",
        "activities", "scales", "scala_salute", "note_title", "note",
        "source", "created_at", "updated_at",
    }
    # Derived fields are computed by the serializer (D8.a) because day_entries
    # does not persist date/weekday.
    assert record["date"] == dates[0]
    assert record["weekday"] == "giovedì"  # 2026-08-20 is a Thursday.
    assert isinstance(record["activities"], list)
    assert record["source"] == "csv"


def test_records_columns_selection(api_client) -> None:
    """GET /api/records?columns=full_date,mood,scales -> only those keys."""
    resp = api_client.get(
        "/api/records?user=default&columns=full_date,mood,scales"
    )
    assert resp.status_code == 200
    records = resp.get_json()["records"]
    assert records, "expected seeded records"
    for record in records:
        assert set(record) == {"full_date", "mood", "scales"}
        assert isinstance(record["scales"], dict)


def test_records_scales_labels_and_levels_count(api_client) -> None:
    """Task 4.2: /api/records exposes value/label (and levels_count) per scale."""
    resp = api_client.get("/api/records?user=default&columns=full_date,scales,scala_salute")
    assert resp.status_code == 200
    records = resp.get_json()["records"]
    by_date = {record["full_date"]: record for record in records}

    # 2026-08-04: Stress "Alto 4/5", Scala salute "insufficiente" (D2).
    record = by_date["2026-08-04"]
    assert record["scales"]["Stress"] == {
        "value": 4,
        "label": "Alto",
        "levels_count": 5,
    }
    assert record["scales"]["Scala salute"] == {
        "value": 6,
        "label": "insufficiente",
        "levels_count": 8,
    }
    assert record["scala_salute"] == record["scales"]["Scala salute"]


# --------------------------------------------------------------------------- #
# GET /api/records — filters + validation
# --------------------------------------------------------------------------- #
def test_records_date_filter_inclusive(api_client) -> None:
    """start_date/end_date are inclusive bounds (only 2026-08-04 in range)."""
    resp = api_client.get(
        "/api/records?user=default&start_date=2026-08-04&end_date=2026-08-04"
    )
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["total"] == 1
    assert payload["records"][0]["full_date"] == "2026-08-04"


def test_records_invalid_date_400(api_client) -> None:
    """Malformed dates -> 400 with a clear message."""
    resp = api_client.get("/api/records?start_date=04-08-2026")
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["type"] == "bad_request"
    assert "date format" in body["error"]

    resp = api_client.get("/api/records?end_date=2026/08/04")
    assert resp.status_code == 400
    assert resp.get_json()["type"] == "bad_request"


def test_records_start_after_end_400(api_client) -> None:
    """start_date > end_date -> 400 (defensive validation)."""
    resp = api_client.get("/api/records?start_date=2026-08-20&end_date=2026-08-01")
    assert resp.status_code == 400
    assert resp.get_json()["type"] == "bad_request"


def test_records_pagination(api_client) -> None:
    """page/limit slicing with total preserved."""
    resp = api_client.get("/api/records?user=default&page=2&limit=8")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["total"] == SEED_COUNT
    assert payload["page"] == 2
    assert payload["limit"] == 8
    assert len(payload["records"]) == 8

    # Pages are contiguous slices over the DESC ordering.
    first_page = api_client.get(
        "/api/records?user=default&page=1&limit=8"
    ).get_json()["records"]
    all_dates = [record["full_date"] for record in first_page + payload["records"]]
    assert all_dates == sorted(all_dates, reverse=True)


def test_records_limit_too_large_400(api_client) -> None:
    """limit > 1000 -> 400 (WORKPLAN §7 Task 4.1)."""
    resp = api_client.get("/api/records?limit=1001")
    assert resp.status_code == 400
    assert resp.get_json()["type"] == "bad_request"


def test_records_page_non_positive_400(api_client) -> None:
    """page <= 0 -> 400."""
    resp = api_client.get("/api/records?page=0")
    assert resp.status_code == 400
    assert resp.get_json()["type"] == "bad_request"


def test_records_unknown_column_400(api_client) -> None:
    """Unknown column name -> 400 listing the offenders."""
    resp = api_client.get("/api/records?columns=full_date,nope")
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["type"] == "bad_request"
    assert "nope" in body["error"]


# --------------------------------------------------------------------------- #
# GET /api/records/<full_date>
# --------------------------------------------------------------------------- #
def test_record_by_date_canonical(api_client) -> None:
    """Single record -> canonical shape incl. scales/scala_salute (Task 4.2)."""
    resp = api_client.get("/api/records/2026-08-04?user=default")
    assert resp.status_code == 200
    record = resp.get_json()
    assert record["full_date"] == "2026-08-04"
    assert record["date"] == "2026-08-04"
    assert record["weekday"] == "martedì"
    assert record["mood"] == 2
    assert record["mood_label"] == "bene"
    assert record["scales"]["Stress"]["label"] == "Alto"
    assert record["scales"]["Stress"]["value"] == 4
    assert record["scala_salute"]["label"] == "insufficiente"
    assert "note_title" in record
    assert "note" in record


def test_record_by_date_not_found(api_client) -> None:
    """Missing date -> uniform 404 {"error": "not_found", "type": "not_found"}."""
    resp = api_client.get("/api/records/1999-01-01")
    assert resp.status_code == 404
    assert resp.get_json() == {"error": "not_found", "type": "not_found"}


def test_record_by_date_invalid_400(api_client) -> None:
    """Malformed date path -> 400."""
    resp = api_client.get("/api/records/not-a-date")
    assert resp.status_code == 400
    assert resp.get_json()["type"] == "bad_request"


# --------------------------------------------------------------------------- #
# GET /api/import/jobs
# --------------------------------------------------------------------------- #
def test_import_jobs_after_seed(api_client) -> None:
    """At least one audit job exists after an import."""
    resp = api_client.get("/api/import/jobs")
    assert resp.status_code == 200
    jobs = resp.get_json()["jobs"]
    assert len(jobs) >= 1
    assert jobs[0]["user"] == "default"
    assert jobs[0]["status"] == "completed"
    assert jobs[0]["imported"] == SEED_COUNT
    # detail is parsed back to a JSON array.
    assert isinstance(jobs[0]["detail"], list)


def test_import_jobs_user_and_limit(api_client) -> None:
    """user/limit params filter the audit list."""
    resp = api_client.get("/api/import/jobs?user=default&limit=1")
    assert resp.status_code == 200
    assert len(resp.get_json()["jobs"]) == 1

    resp = api_client.get("/api/import/jobs?user=ghost")
    assert resp.status_code == 200
    assert resp.get_json()["jobs"] == []

    resp = api_client.get("/api/import/jobs?limit=1001")
    assert resp.status_code == 400
    assert resp.get_json()["type"] == "bad_request"


# --------------------------------------------------------------------------- #
# Legacy backward compatibility (old server.py wire contract)
# --------------------------------------------------------------------------- #
def test_legacy_users_shape(api_client) -> None:
    """GET /users -> [{"user": ..., "records": n}] for EVERY users row (D6).

    The legacy wire contract used the YAML user list; we now serve the ``users``
    table, so the bootstrapped admin is listed with 0 records alongside the
    seeded ``default`` user with their record count.
    """
    resp = api_client.get("/users")
    assert resp.status_code == 200
    by_user = {entry["user"]: entry["records"] for entry in resp.get_json()}
    assert by_user == {"admin": 0, "default": SEED_COUNT}


def test_legacy_moods_exact_keys(api_client) -> None:
    """GET /moods -> old keys date,time,mood,activities,note_title,note."""
    resp = api_client.get(
        "/moods?user=default&start_date=2026-08-04&end_date=2026-08-04"
    )
    assert resp.status_code == 200
    moods = resp.get_json()
    assert len(moods) == 1
    assert set(moods[0]) == {"date", "time", "mood", "activities", "note_title", "note"}
    assert moods[0]["date"] == "2026-08-04"
    assert moods[0]["time"]
    assert moods[0]["mood"] == 2
    assert isinstance(moods[0]["activities"], list)
    assert moods[0]["note_title"] == ""
    assert moods[0]["note"] == ""


def test_legacy_moods_missing_user_400(api_client) -> None:
    """Legacy /moods without user now returns a clear 400 (old: None query)."""
    resp = api_client.get("/moods")
    assert resp.status_code == 400
    body = resp.get_json()
    assert "user" in body["error"].lower()
    assert body["type"] == "bad_request"
