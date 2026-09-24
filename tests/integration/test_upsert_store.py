"""Fase 3 acceptance — SQLite upsert (D3/R5), Influx delete-and-write, audit.

Key criteria verified on the real sample (WORKPLAN §7 FASE 3):
  1. One row per ``(user, full_date)``: importing the 630-row CSV persists
     628 unique days (the two duplicate dates 2025-01-01/03 collapse to their
     most recent wins, D3) and count_entries == 628.
  2. Re-importing the .daylio backup updates all 628 existing rows
     (``summary.updated == 628``) instead of duplicating them.
  3. Upsert replaces every field (mood/activities/scales/notes) in SQLite AND
     the Influx point for the day, keeping exactly one point per day.
  4. R8/§5.4: an upsert whose record carries no scales removes the ``scale_*``
     fields and ``scala_salute_label`` tag from the point (delete-and-write).
  5. Every import leaves exactly one ``import_jobs`` audit row (§5.3).
"""

from __future__ import annotations

from daylio_api.db.sqlite_store import (
    SqliteStore,
    count_entries,
    get_entry,
    list_entries,
)
from daylio_api.import_flow import import_data
from daylio_api.importers.base import NormalizedEntry
from daylio_api.importers.csv_importer import parse_csv
from daylio_api.importers.daylio_importer import parse_daylio
from daylio_api.importers.normalizer import normalize


def _sample_entry(sample_csv, config):
    """Parse the sample CSV and return (entries_by_date, normalize result)."""
    result = parse_csv(sample_csv, config)
    entries = normalize(result, config)
    return {entry.full_date: entry for entry in entries}, result


def test_csv_import_persists_628_days_and_audits(
    tmp_sqlite, sample_csv, config_sample
) -> None:
    """Criterio 1: 630 rows -> 628 unique days in SQLite + one audit row."""
    store = SqliteStore(str(tmp_sqlite))
    conn = store.connect()
    result = parse_csv(sample_csv, config_sample)

    summary = import_data(conn, None, config_sample, "default", "upload", str(sample_csv), result)

    # Import/upsert counters from the actual SQLite writes.
    assert summary.status == "completed"
    assert summary.imported == 628
    assert summary.updated == 0
    assert summary.skipped == 2
    assert summary.errors == 0
    assert summary.job_id is not None

    # D3/R5: one row per day, winning times of the duplicate dates.
    assert count_entries(conn, "default") == 628
    assert get_entry(conn, "default", "2025-01-01")["time"] == "20:02"
    assert get_entry(conn, "default", "2025-01-03")["time"] == "21:36"

    # A minimal users row exists (structure only, Phase 5 auth).
    user = conn.execute("SELECT username, is_admin FROM users").fetchone()
    assert tuple(user) == ("default", 0)

    # Audit: exactly one job row with the §5.3 counters persisted.
    jobs = conn.execute(
        "SELECT source, filename, imported, updated, skipped, errors, status "
        "FROM import_jobs"
    ).fetchall()
    assert len(jobs) == 1
    assert jobs[0]["imported"] == 628
    assert jobs[0]["status"] == "completed"

    records = list_entries(conn, "default", start_date="2025-01-01", end_date="2025-01-03")
    # The sample has NO row for 2025-01-02: the window returns the two
    # duplicate-date winners, ordered full_date DESC (R5/D3).
    assert [r["full_date"] for r in records] == ["2025-01-03", "2025-01-01"]
    assert records[0]["time"] == "21:36"
    assert records[-1]["time"] == "20:02"
    assert records[0]["user"] == "default"


def test_daylio_reimport_updates_all_days(
    tmp_sqlite, sample_csv, sample_daylio, config_sample, influx_store
) -> None:
    """Criterio 2: the .daylio import updates the 628 existing rows."""
    store = SqliteStore(str(tmp_sqlite))
    conn = store.connect()

    csv_result = parse_csv(sample_csv, config_sample)
    first = import_data(
        conn, influx_store, config_sample, "default", "upload", str(sample_csv), csv_result
    )
    assert first.imported == 628 and first.influx_errors == 0
    assert influx_store._injected_client.count() == 628

    daylio_result = parse_daylio(sample_daylio, config_sample)
    second = import_data(
        conn, influx_store, config_sample, "default", "upload", str(sample_daylio), daylio_result
    )

    assert second.status == "completed"
    assert second.imported == 0
    assert second.updated == 628
    assert second.skipped == 2  # same duplicate dates in the .daylio export
    assert count_entries(conn, "default") == 628  # never duplicated

    # One point per day, and the winner time survived the re-import.
    assert influx_store._injected_client.count() == 628
    assert get_entry(conn, "default", "2025-01-03")["time"] == "21:36"

    # Two audit rows now: one per import run.
    jobs = conn.execute("SELECT filename, updated FROM import_jobs ORDER BY id").fetchall()
    assert len(jobs) == 2
    assert jobs[1]["updated"] == 628


def test_upsert_replaces_every_field_and_influx_point(
    tmp_sqlite, sample_csv, config_sample, influx_store
) -> None:
    """Criterio 3: an upsert fully replaces the SQLite row and the daily point."""
    store = SqliteStore(str(tmp_sqlite))
    conn = store.connect()
    entries, _ = _sample_entry(sample_csv, config_sample)
    original = entries["2025-01-01"]

    summary = import_data(
        conn, influx_store, config_sample, "default", "upload", str(sample_csv),
        parse_csv(sample_csv, config_sample),
    )
    assert summary.imported == 628
    assert influx_store._injected_client.count() == 628
    point_before = influx_store._injected_client.get("default", "2025-01-01")
    assert point_before is not None
    # The CSV winner for this date is a "così così" row (no Stress column: only
    # 58 sample rows carry a Stress value, all in 2026-07+).
    assert point_before["fields"]["mood"] == 3
    assert point_before["tags"]["date"] == "2025-01-01"

    # A heavily-edited record for the same day (every mutable field changes).
    modified = NormalizedEntry(
        full_date="2025-01-01",
        date="2025-01-01",
        weekday="Wednesday",
        time="20:02",
        mood_level=4,
        mood_label="male",
        activities=["A", "B"],
        scales=[{
            "scale_name": "Stress",
            "label": "Alto",
            "rank": 4,
            "levels_count": 5,
            "scale_key": "stress",
        }],
        scala_salute={"label": "discreto", "rank": 3},
        note_title="T2",
        note="N2",
        source="csv",
        raw=dict(original.raw),
    )
    upsert = import_data(
        conn, influx_store, config_sample, "default", "upload", "modified.csv", [modified]
    )
    assert upsert.status == "completed"
    assert upsert.imported == 0
    assert upsert.updated == 1
    assert upsert.errors == 0

    # SQLite: every field replaced.
    row = get_entry(conn, "default", "2025-01-01")
    assert row["mood"] == 4
    assert row["mood_label"] == "male"
    assert row["activities"] == ["A", "B"]
    assert row["scales"] == {
        "Stress": {"value": 4, "label": "Alto"},
        "Scala salute": {"value": 3, "label": "discreto"},
    }
    assert row["note_title"] == "T2"
    assert row["note"] == "N2"

    # Influx: the single point for the day was rewritten (not appended).
    assert influx_store._injected_client.count() == 628
    point = influx_store._injected_client.get("default", "2025-01-01")
    assert point["tags"]["user"] == "default"
    assert point["tags"]["date"] == "2025-01-01"
    assert point["tags"]["mood_label"] == "male"
    assert point["tags"]["scala_salute_label"] == "discreto"
    assert point["fields"]["mood"] == 4
    assert point["fields"]["scale_stress_value"] == 4
    assert point["fields"]["scale_scala_salute_value"] == 3
    assert point["fields"]["activities_count"] == 2
    assert point["fields"]["note"] == "N2"
    assert point["fields"]["note_title"] == "T2"
    assert point["time"] == "2025-01-01T00:00:00Z"

    # Delete-and-write: the delete-day call for the upsert covered the whole day.
    last_delete = influx_store._injected_client.deleted[-1]
    assert last_delete["start"] == "2025-01-01T00:00:00Z"
    assert last_delete["stop"] == "2025-01-02T00:00:00Z"
    assert 'user="default"' in last_delete["predicate"]
    assert '_measurement="daylio_entry"' in last_delete["predicate"]


def test_upsert_without_scales_removes_scale_fields_r8(
    tmp_sqlite, sample_csv, config_sample, influx_store
) -> None:
    """R8/§5.4: removing scales on re-import purges them from the point too."""
    store = SqliteStore(str(tmp_sqlite))
    conn = store.connect()
    import_data(
        conn, influx_store, config_sample, "default", "upload", str(sample_csv),
        parse_csv(sample_csv, config_sample),
    )
    # 2026-07-24 is one of the 58 sample rows carrying "Stress: Moderato 3/5"
    # plus a Scala salute level inside activities.
    before = influx_store._injected_client.get("default", "2026-07-24")
    assert before is not None
    assert "scale_stress_value" in before["fields"]
    assert before["tags"].get("scala_salute_label")

    no_scales = NormalizedEntry(
        full_date="2026-07-24",
        date="2026-07-24",
        weekday="Friday",
        time="20:00",
        mood_level=3,
        mood_label="così così",
        activities=["Solo"],
        scales=[],
        scala_salute=None,
        note_title="",
        note="",
        source="csv",
        raw={},
    )
    upsert = import_data(
        conn, influx_store, config_sample, "default", "upload", "no_scales.csv", [no_scales]
    )
    assert upsert.updated == 1

    after = influx_store._injected_client.get("default", "2026-07-24")
    assert after is not None
    assert "scale_stress_value" not in after["fields"]
    assert "scale_scala_salute_value" not in after["fields"]
    assert "scala_salute_label" not in after["tags"]
    assert after["fields"]["mood"] == 3
    assert after["fields"]["activities_count"] == 1

    # SQLite scales object no longer carries the removed fields either.
    row = get_entry(conn, "default", "2026-07-24")
    assert row["scales"] == {}
