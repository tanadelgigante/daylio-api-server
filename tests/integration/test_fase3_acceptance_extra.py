"""Fase 3 acceptance — extra coverage not present in the existing suite.

These tests target the acceptance criteria that were only partially covered by
``test_upsert_store.py`` / ``test_resync.py`` (SOLO tests; application code is
NOT touched):

  1. Criterion 3 — audit on an invalid import: ``import_data`` never leaks an
     unhandled exception; a catastrophic failure records a ``status="failed"``
     ``import_jobs`` row, a parseable-but-invalid file records its errors.
  2. Criterion 6 — InfluxDB down tolerance (R8/D4): the SQLite import continues
     when ``influx_available`` is False, the problem is counted/logged, and
     ``resync_all`` restores coherence once Influx is back.
  3. Criterion 4 — full Influx point model of §5.4 on a real sample row:
     measurement, tags, numeric + string fields, fixed start-of-day timestamp.
  4. Criterion 1 — the ``UNIQUE(user, full_date)`` constraint is active at the
     DB level and rejects a duplicate-day insert (upsert defuses it).
  5. Criterion 2 — re-importing a *modified CSV file* for a past day updates
     both SQLite and the Influx point without duplicating rows/points.
"""

from __future__ import annotations

import copy
import csv
import sqlite3

import pytest

from daylio_api.audit import list_jobs
from daylio_api.db.sqlite_store import (
    SqliteStore,
    count_entries,
    get_entry,
    get_or_create_user,
)
from daylio_api.import_flow import import_data
from daylio_api.importers.csv_importer import parse_csv


class ToggleClient:
    """Duck-typed Influx client whose ``ping`` can be switched on/off (R8).

    Mirrors the real delete-and-write semantics and records a trail so the
    tests can prove no write/delete happens while the server is reported down.
    """

    def __init__(self) -> None:
        self.points: list[dict] = []
        self.deleted: list[str] = []
        self.live = False

    def ping(self, **kwargs: object) -> bool:
        return self.live

    def write(self, bucket: str, record: dict) -> None:
        if not self.live:  # pragma: no cover - the store must not call it
            raise AssertionError("write called while Influx is down")
        self.points.append(copy.deepcopy(record))

    def delete(self, start: str, stop: str, predicate: str, bucket: str, org: str) -> None:
        if not self.live:  # pragma: no cover - the store must not call it
            raise AssertionError("delete called while Influx is down")
        self.deleted.append(predicate)
        self.points = [
            point
            for point in self.points
            if not (
                (point.get("tags") or {}).get("user") == "default"
                and start <= point.get("time", "") < stop
            )
        ]

    def query(self, flux: str) -> list[dict]:
        return copy.deepcopy(self.points)

    def close(self) -> None:
        pass


# --------------------------------------------------------------------------- #
# 3. Audit on invalid / failed imports
# --------------------------------------------------------------------------- #
def test_catastrophic_import_records_failed_audit(tmp_sqlite, config_sample) -> None:
    """A fatal error inside import_data is contained and written as failed.

    ```import_data`` receives an iterable of unsupported items; the
    normalization raises TypeError which must be contained, audit row written
    with status ``failed`` (no unhandled exception for the caller).
    """
    store = SqliteStore(str(tmp_sqlite))
    conn = store.connect()

    summary = import_data(
        conn, None, config_sample, "default", "upload", "broken.csv", [42, "nope"]
    )

    assert summary.status == "failed"
    assert summary.errors >= 1
    assert any(d["kind"] == "fatal" for d in summary.detail)

    jobs = conn.execute(
        "SELECT status, imported, updated, errors, detail FROM import_jobs"
    ).fetchall()
    assert len(jobs) == 1
    assert jobs[0]["status"] == "failed"
    assert jobs[0]["errors"] >= 1
    assert "broken.csv" in conn.execute("SELECT filename FROM import_jobs").fetchone()[0]
    assert count_entries(conn, "default") == 0


def test_invalid_csv_file_raises_and_invalid_rows_are_audited(
    tmp_sqlite, tmp_path, config_sample
) -> None:
    """An unparseable CSV raises at parse stage; a bad-row CSV audits errors."""
    # A CSV without the mandatory full_date column is rejected at parse time.
    bad_header = tmp_path / "bad_header.csv"
    bad_header.write_text("date,mood,note\n2025-01-01,4,x\n", encoding="utf-8")
    with pytest.raises(ValueError):
        parse_csv(bad_header, config_sample)

    # A parseable CSV whose rows lack full_date counts errors, not exceptions.
    bad_row = tmp_path / "bad_row.csv"
    bad_row.write_text(
        "full_date,mood\n,4\n2025-01-01,3\n,5\n2025-01-03,2\n", encoding="utf-8-sig"
    )
    result = parse_csv(bad_row, config_sample)
    assert result.errors == 2  # the two empty-full_date rows
    assert len(result.records) == 2

    store = SqliteStore(str(tmp_sqlite))
    conn = store.connect()
    summary = import_data(conn, None, config_sample, "default", "upload", str(bad_row), result)
    assert summary.status == "completed"  # bad rows never kill the import
    assert summary.errors == 2
    assert summary.imported == 2
    job = list_jobs(conn)[0]
    assert job["status"] == "completed"
    assert job["imported"] == 2
    assert job["errors"] == 2
    error_entries = [d for d in job["detail"] if d.get("level") == "error"]
    assert len(error_entries) == 2


# --------------------------------------------------------------------------- #
# 6. Influx down tolerance + resync recovery (R8 / D4)
# --------------------------------------------------------------------------- #
def test_import_survives_influx_down_and_resync_recovers(
    tmp_sqlite, sample_csv, config_sample
) -> None:
    from daylio_api.db.influx_store import InfluxConfig, InfluxStore
    from daylio_api.import_flow import resync_all as resync

    store = SqliteStore(str(tmp_sqlite))
    conn = store.connect()
    client = ToggleClient()  # default live=False -> Influx "down"
    down_store = InfluxStore(
        InfluxConfig(
            url="http://fake-down:8120",
            token="token",
            org="tanadelgigante",
            bucket="daylio",
            measurement="daylio_entry",
        ),
        client=client,
    )

    summary = import_data(
        conn, down_store, config_sample, "default", "upload", str(sample_csv),
        parse_csv(sample_csv, config_sample),
    )
    # SQLite import succeeds, Influx problem is warned/counted, no write attempted.
    assert summary.status == "completed"
    assert count_entries(conn, "default") == 628
    assert summary.influx_errors == 0
    assert summary.warnings >= 1
    assert any(d["level"] == "warning" and "Influx" in d["message"] for d in summary.detail)
    assert client.points == []  # nothing was written to a "down" server

    # A resync while still down is abortive but harmless.
    stats = resync(conn, down_store, scale_keys=None)
    assert stats["influx_errors"] == 1
    assert client.points == []

    # Server comes back -> resync_all rewrites every day from SQLite (D4).
    client.live = True
    stats = resync(conn, down_store, scale_keys=None)
    assert stats["days"] == 628
    assert stats["influx_errors"] == 0
    assert len(client.points) == 628
    assert all(p["time"].endswith("T00:00:00Z") for p in client.points)


# --------------------------------------------------------------------------- #
# 4. Influx point model (§5.4) on a real sample row
# --------------------------------------------------------------------------- #
def test_influx_point_model_section54(tmp_sqlite, sample_csv, config_sample, influx_store) -> None:
    store = SqliteStore(str(tmp_sqlite))
    conn = store.connect()
    summary = import_data(
        conn, influx_store, config_sample, "default", "upload", str(sample_csv),
        parse_csv(sample_csv, config_sample),
    )
    assert summary.imported == 628
    fake = influx_store._injected_client

    point = fake.get("default", "2026-07-24")  # carries Stress level + salute
    assert point is not None
    # measurement + tags
    assert point["measurement"] == "daylio_entry"
    assert point["tags"]["user"] == "default"
    assert point["tags"]["date"] == "2026-07-24"
    assert point["tags"]["mood_label"]
    assert point["tags"].get("scala_salute_label")
    # numeric fields (§5.4)
    fields = point["fields"]
    for name in ("mood", "mood_num", "activities_count", "is_favorite", "notes_present"):
        assert isinstance(fields.get(name), int), f"field {name!r} is not int"
    assert "scale_stress_value" in fields and isinstance(fields["scale_stress_value"], int)
    salute_val = fields.get("scale_scala_salute_value")
    assert salute_val is not None and isinstance(salute_val, int)
    # string fields (§5.4)
    for name in ("activities", "scales", "note_title", "note", "source"):
        assert isinstance(fields.get(name), str), f"field {name!r} is not str"
    assert fields["source"] == "csv"
    # activities JSON must be a comma-separated JSON array of strings
    activities = fields["activities"]
    assert activities.startswith("[") and activities.endswith("]")
    # fixed start-of-day timestamp (D3/§5.4)
    assert point["time"] == "2026-07-24T00:00:00Z"


# --------------------------------------------------------------------------- #
# 1. DB-level uniqueness on (user, full_date)
# --------------------------------------------------------------------------- #
def test_day_entries_unique_constraint_rejects_duplicate_day(tmp_sqlite) -> None:
    """The UNIQUE(user, full_date) constraint is active at the DB level.

    A raw duplicate insert must raise IntegrityError; the store's upsert paths
    (ON CONFLICT DO UPDATE) never trigger it.
    """
    store = SqliteStore(str(tmp_sqlite))
    conn = store.connect()
    get_or_create_user(conn, "default")
    get_or_create_user(conn, "alice")
    conn.commit()
    conn.execute(
        "INSERT INTO day_entries (user, full_date, time, mood, mood_label) "
        "VALUES ('default', '2025-01-01', '20:02', 3, 'così così')"
    )
    conn.commit()

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO day_entries (user, full_date, mood) "
            "VALUES ('default', '2025-01-01', 4)"
        )
    conn.rollback()

    # A different user may have the same date: invariant is per (user, date).
    conn.execute(
        "INSERT INTO day_entries (user, full_date, mood) "
        "VALUES ('alice', '2025-01-01', 4)"
    )
    conn.commit()
    assert count_entries(conn, "default") == 1


# --------------------------------------------------------------------------- #
# 2. Re-import of a MODIFIED CSV file for a past day (real file path)
# --------------------------------------------------------------------------- #
def _write_modified_csv(sample_csv, tmp_path, *, mood="male", activities="A | B | C",
                        scales="Stress: Alto 4/5", note_title="T2", note="N2") -> str:
    """Rewrite the full sample CSV with 2025-01-01 (and 03) rows edited."""
    out = tmp_path / "modified_sample.csv"
    with open(sample_csv, encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        fieldnames = reader.fieldnames
        rows = list(reader)
    for row in rows:
        if row["full_date"].strip() in ("2025-01-01", "2025-01-03"):
            row["mood"] = mood
            row["activities"] = activities
            row["scales"] = scales
            row["note_title"] = note_title
            row["note"] = note
    with open(out, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return str(out)


def test_reimport_modified_csv_updates_both_stores(
    tmp_sqlite, sample_csv, config_sample, influx_store, tmp_path
) -> None:
    store = SqliteStore(str(tmp_sqlite))
    conn = store.connect()
    first = import_data(
        conn, influx_store, config_sample, "default", "upload", str(sample_csv),
        parse_csv(sample_csv, config_sample),
    )
    assert first.imported == 628
    fake = influx_store._injected_client
    assert fake.count() == 628
    assert get_entry(conn, "default", "2025-01-01")["mood_label"] == "così così"

    modified_path = _write_modified_csv(sample_csv, tmp_path)
    second = import_data(
        conn, influx_store, config_sample, "default", "upload", modified_path,
        parse_csv(modified_path, config_sample),
    )
    # Same 628 unique days; every existing key is an upsert-update.
    assert second.status == "completed"
    assert second.imported == 0
    assert second.updated == 628
    assert count_entries(conn, "default") == 628
    assert fake.count() == 628

    # SQLite updated (all mutable fields of a past day).
    row = get_entry(conn, "default", "2025-01-01")
    assert row["mood_label"] == "male"
    assert row["activities"] == ["A", "B", "C"]
    assert row["scales"] == {"Stress": {"value": 4, "label": "Alto"}}
    assert row["note_title"] == "T2"
    assert row["note"] == "N2"

    # Influx point rewritten via delete-and-write (no leftover old values).
    point = fake.get("default", "2025-01-01")
    assert point["fields"]["mood"] == 4
    assert point["tags"]["mood_label"] == "male"
    assert point["fields"]["scale_stress_value"] == 4
    assert point["fields"]["activities_count"] == 3
    assert point["fields"]["note"] == "N2"
    assert point["time"] == "2025-01-01T00:00:00Z"
    # exactly one delete window for the day was issued before the write
    assert any(
        d["start"] == "2025-01-01T00:00:00Z" and d["stop"] == "2025-01-02T00:00:00Z"
        for d in fake.deleted
    )
