"""Fase 3 acceptance — Influx resync from SQLite (D4, WORKPLAN §7 FASE 3).

Scenario:
  1. The CSV is imported into SQLite + Influx (628 points).
  2. SQLite is the source of truth (D4): an external edit changes one day and
     the matching Influx point is dropped (simulating data loss / drift).
  3. ``resync_all`` rewrites EVERY day from SQLite into Influx using the
     delete-and-write strategy, restoring the edited values.
"""

from __future__ import annotations

from daylio_api.db.sqlite_store import SqliteStore
from daylio_api.import_flow import import_data, resync_all
from daylio_api.importers.csv_importer import parse_csv


def test_resync_restores_dropped_influx_day(
    tmp_sqlite, sample_csv, config_sample, influx_store
) -> None:
    store = SqliteStore(str(tmp_sqlite))
    conn = store.connect()

    summary = import_data(
        conn, influx_store, config_sample, "default", "upload", str(sample_csv),
        parse_csv(sample_csv, config_sample),
    )
    assert summary.imported == 628
    fake = influx_store._injected_client
    assert fake.count() == 628

    # --- SQLite is the truth: edit one day directly ------------------------- #
    conn.execute(
        "UPDATE day_entries SET mood = 2, mood_label = 'bene', "
        "note = 'resynced note', updated_at = datetime('now') "
        "WHERE user = 'default' AND full_date = '2025-01-01'"
    )
    conn.commit()

    # --- Simulate Influx drift/loss for that day ---------------------------- #
    fake.drop("default", "2025-01-01")
    assert fake.get("default", "2025-01-01") is None
    assert fake.count() == 627

    # --- Resync everything from SQLite (D4) --------------------------------- #
    stats = resync_all(conn, influx_store, scale_keys=None)
    assert stats["users"] == 1
    assert stats["days"] == 628
    assert stats["influx_errors"] == 0

    # --- The day is restored with the edited SQLite values ------------------ #
    assert fake.count() == 628
    point = fake.get("default", "2025-01-01")
    assert point["fields"]["mood"] == 2
    assert point["tags"]["mood_label"] == "bene"
    assert point["fields"]["note"] == "resynced note"

    # SQLite is unchanged by the resync (it is the source of truth).
    assert fake.count() == 628
