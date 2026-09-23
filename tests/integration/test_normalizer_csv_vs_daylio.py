"""Integration: CSV vs .daylio normalized coincidence on ALL dates (Task 2.4).

This is the key acceptance criterion of Fase 2 (WORKPLAN §7 FASE 2): the
normalizers of the two pipelines must produce the SAME canonical record for
every common date (criterio 5 "Coincidenza CSV vs .daylio per le stesse date").

The comparison here is intentionally PERVASIVE (not a subset):
  - ``full_date`` set equality: no date is CSV-only or daylio-only;
  - for every common date: ``full_date``, ``mood_level``, ``mood_label``,
    ``date``, ``weekday``, ``time``, ``activities`` (exact order),
    ``scales`` (full canonical dict incl. ``scale_key``), ``scala_salute``,
    ``note_title``, ``note`` must be identical;
  - the ONLY allowed difference is ``source`` (provenance csv/daylio).

Additional independent verifications in this module:
  - the dedup rule "vince la più recente" (D3) is checked against the RAW
    sources for EVERY date, not just the two duplicate ones:
      * CSV     : winner = row with the most recent ``time`` (all sample times
                  are zero-padded HH:MM, so max(...) is chronological); ties ->
                  later row in file order.
      * .daylio : winner = dayEntries element with the greatest ``datetime``
                  epoch-ms; ties -> greatest entry ``id``.
  - the "Scala salute" (D2) is the SAME ``{label, rank}`` field from CSV
    activities and .daylio tag-group 5 on known dates;
  - ``NormalizedEntry`` exposes exactly the fields Fase 3 stores consume
    (upsert SQLite, WORKPLAN §5.3/§5.5 step 6) and stays coherent with the
    Fase 1 config (mood 5 labels, Stress 5 levels, Scala salute 8 levels).
"""

from __future__ import annotations

import base64
import json
import zipfile
from collections import Counter

from daylio_api.importers.csv_importer import parse_csv
from daylio_api.importers.daylio_importer import parse_daylio
from daylio_api.importers.normalizer import normalize

#: Keys that MUST coincide CSV vs .daylio for every common date.
_COMPARED_KEYS = (
    "full_date",
    "date",
    "weekday",
    "time",
    "mood",
    "mood_label",
    "activities",
    "scales",
    "scala_salute",
    "note_title",
    "note",
)


def _load_both(sample_csv, sample_daylio, config_sample):
    csv_res = parse_csv(sample_csv, config_sample)
    day_res = parse_daylio(sample_daylio, config_sample)
    csv_by_date = {e.full_date: e for e in normalize(csv_res, config_sample)}
    day_by_date = {e.full_date: e for e in normalize(day_res, config_sample)}
    return csv_res, day_res, csv_by_date, day_by_date


def _format_deviations(deviations, limit=25) -> str:
    lines = [f"{len(deviations)} deviation(s) CSV vs daylio:"]
    for date, key, csv_value, day_value in deviations[:limit]:
        lines.append(
            f"  {date} [{key}]: csv={csv_value!r} vs daylio={day_value!r}"
        )
    if len(deviations) > limit:
        lines.append(f"  ... and {len(deviations) - limit} more")
    return "\n".join(lines)


def test_all_dates_coincide_csv_vs_daylio(sample_csv, sample_daylio, config_sample) -> None:
    """Criterio 5: EVERY common date produces the same normalized record."""
    _, _, csv_by_date, day_by_date = _load_both(
        sample_csv, sample_daylio, config_sample
    )

    assert set(csv_by_date) == set(day_by_date) == {
        e.full_date for e in csv_by_date.values()
    }
    only_csv = sorted(set(csv_by_date) - set(day_by_date))
    only_day = sorted(set(day_by_date) - set(csv_by_date))
    assert not only_csv and not only_day, (
        f"date set mismatch: only_csv={only_csv}, only_day={only_day}"
    )

    deviations = []
    for date in sorted(csv_by_date):
        a = csv_by_date[date].as_dict()
        b = day_by_date[date].as_dict()
        for key in _COMPARED_KEYS:
            if a[key] != b[key]:
                deviations.append((date, key, a[key], b[key]))

    assert not deviations, _format_deviations(deviations)


def test_normalized_counts_match_both_formats(sample_csv, sample_daylio, config_sample) -> None:
    """The aggregate counts of criterion 1-4 coincide for the two pipelines."""
    _, _, csv_by_date, day_by_date = _load_both(
        sample_csv, sample_daylio, config_sample
    )

    assert len(csv_by_date) == len(day_by_date) == 628  # 630 rows - 2 dup days
    assert Counter(e.mood_level for e in csv_by_date.values()) == Counter(
        e.mood_level for e in day_by_date.values()
    )

    csv_stress = Counter(e.scales[0]["rank"] for e in csv_by_date.values() if e.scales)
    day_stress = Counter(e.scales[0]["rank"] for e in day_by_date.values() if e.scales)
    assert dict(csv_stress) == dict(day_stress) == {1: 2, 2: 17, 3: 22, 4: 15, 5: 2}

    csv_salute = Counter(e.scala_salute["rank"] for e in csv_by_date.values() if e.scala_salute)
    day_salute = Counter(e.scala_salute["rank"] for e in day_by_date.values() if e.scala_salute)
    assert dict(csv_salute) == dict(day_salute)
    assert sorted(dict(csv_salute)) == [3, 4, 5, 6, 7]


def test_scala_salute_same_field_for_known_dates(
    sample_csv, sample_daylio, config_sample
) -> None:
    """D2: CSV (activities) and .daylio (tag-group 5) give the same field."""
    _, _, csv_by_date, day_by_date = _load_both(
        sample_csv, sample_daylio, config_sample
    )

    # Known dates with Scala salute levels (ranks 3/4/7), checked on BOTH sides.
    # Dates verified against the real backup (2026-09-22).
    expected = {
        "2026-09-21": {"label": "discreto", "rank": 3},
        "2026-01-24": {"label": "sufficiente", "rank": 4},
        "2026-01-05": {"label": "pessimo", "rank": 7},
    }
    for date, wanted in expected.items():
        assert date in csv_by_date and date in day_by_date, f"date {date} missing"
        assert csv_by_date[date].scala_salute == wanted, (
            f"{date} csv scala_salute={csv_by_date[date].scala_salute!r}"
        )
        assert day_by_date[date].scala_salute == wanted, (
            f"{date} daylio scala_salute={day_by_date[date].scala_salute!r}"
        )
        assert csv_by_date[date].scala_salute == day_by_date[date].scala_salute

    # Same {label, rank} for every date carrying a salute level (no drift).
    for date in csv_by_date:
        if csv_by_date[date].scala_salute is not None:
            assert csv_by_date[date].scala_salute == day_by_date[date].scala_salute

    # The salute level must be REMOVED from activities on both sides.
    for date in csv_by_date:
        if csv_by_date[date].scala_salute is not None:
            level = csv_by_date[date].scala_salute["label"]
            assert level not in csv_by_date[date].activities, date
            assert level not in day_by_date[date].activities, date


def test_duplicate_dates_winners_are_most_recent_from_raw_sources(
    sample_csv, sample_daylio, config_sample
) -> None:
    """D3: the kept record for 2025-01-01/03 is the most recent by raw source.

    Computes the winner independently from the RAW data (before the
    normalizer's dedup) and asserts the normalized entry equals it:
      - CSV rows    : ``max((time, file_index))`` (times are HH:MM zero-padded,
        so a chronological max equals the string max); ties -> later row.
      - .daylio JSON: ``max((datetime_ms, id))`` over the date's dayEntries.
    """
    csv_res = parse_csv(sample_csv, config_sample)
    day_res = parse_daylio(sample_daylio, config_sample)
    csv_by_date = {e.full_date: e for e in normalize(csv_res, config_sample)}
    day_by_date = {e.full_date: e for e in normalize(day_res, config_sample)}

    # --- CSV: recompute the winner independently per date ------------------- #
    for date in ("2025-01-01", "2025-01-03"):
        rows_of_date = [
            (idx, record)
            for idx, record in enumerate(csv_res.records)
            if record["full_date"] == date
        ]
        assert len(rows_of_date) == 2, f"{date}: expected 2 CSV rows"
        winner_idx, winner_row = max(rows_of_date, key=lambda t: (t[1]["time"], t[0]))
        assert winner_row["time"] == csv_by_date[date].time, (
            f"{date}: csv winner must be the most recent time "
            f"(file index {winner_idx}, rows {[r['time'] for _, r in rows_of_date]})"
        )

    # --- .daylio: recompute the winner independently from the raw JSON ------ #
    with zipfile.ZipFile(sample_daylio) as zf:
        doc = json.loads(base64.b64decode(zf.read("backup.daylio")).decode("utf-8"))
    day_entries = doc["dayEntries"]
    for date in ("2025-01-01", "2025-01-03"):
        year, month, day = 2025, 0, int(date[-2:])
        entries_of_date = [
            e for e in day_entries
            if e["year"] == year and e["month"] == month and e["day"] == day
        ]
        assert len(entries_of_date) == 2, f"{date}: expected 2 dayEntries"
        winner = max(entries_of_date, key=lambda e: (int(e["datetime"]), int(e["id"])))
        normalized = day_by_date[date]
        assert (int(winner["datetime"]), int(winner["id"])) == (
            normalized.raw["_datetime_ms"],
            normalized.raw["_daylio_id"],
        ), f"{date}: daylio winner must be the most recent (datetime, id)"
        assert normalized.time == f"{winner['hour']:02d}:{winner['minute']:02d}"

    # Explicit "documented" values for the two duplicate dates.
    assert {d: csv_by_date[d].time for d in ("2025-01-01", "2025-01-03")} == {
        "2025-01-01": "20:02",
        "2025-01-03": "21:36",
    }
    assert {d: day_by_date[d].time for d in ("2025-01-01", "2025-01-03")} == {
        "2025-01-01": "20:02",
        "2025-01-03": "21:36",
    }


def test_dedup_winners_verified_for_every_date(
    sample_csv, sample_daylio, config_sample
) -> None:
    """D3 (pervasive): EVERY day's kept record matches the raw-source winner."""
    csv_res = parse_csv(sample_csv, config_sample)
    day_res = parse_daylio(sample_daylio, config_sample)
    csv_by_date = {e.full_date: e for e in normalize(csv_res, config_sample)}
    day_by_date = {e.full_date: e for e in normalize(day_res, config_sample)}

    problems = []
    for date, entry in csv_by_date.items():
        rows = [(i, r) for i, r in enumerate(csv_res.records) if r["full_date"] == date]
        _, winner = max(rows, key=lambda t: (t[1]["time"], t[0]))
        if winner["time"] != entry.time:
            problems.append((date, "csv", winner["time"], entry.time))
    assert not problems, f"CSV dedup deviates from max(time, index): {problems[:10]}"

    with zipfile.ZipFile(sample_daylio) as zf:
        doc = json.loads(base64.b64decode(zf.read("backup.daylio")).decode("utf-8"))
    by_date = {}
    for e in doc["dayEntries"]:
        date = f"{e['year']:04d}-{e['month'] + 1:02d}-{e['day']:02d}"
        by_date.setdefault(date, []).append(e)
    problems = []
    for date, entry in day_by_date.items():
        winner = max(by_date[date], key=lambda x: (int(x["datetime"]), int(x["id"])))
        if (
            int(winner["datetime"]),
            int(winner["id"]),
        ) != (
            entry.raw["_datetime_ms"],
            entry.raw["_daylio_id"],
        ):
            problems.append((date, "daylio", winner["id"], entry.raw["_daylio_id"]))
    assert not problems, f"daylio dedup deviates from max(datetime,id): {problems[:10]}"


def test_normalized_entries_ready_for_fase3(sample_csv, config_sample) -> None:
    """NormalizedEntry schema matches the Fase 3 upsert contract (WORKPLAN §5.3)."""
    csv_res = parse_csv(sample_csv, config_sample)
    entries = normalize(csv_res, config_sample)
    entry = next(e for e in entries if e.full_date == "2026-09-21")

    # ---- Fase 1 config coherence ----------------------------------------- #
    # mood: 5 labels -> ranks 1..5 (D1); label must be a configured label.
    assert entry.mood_level in range(1, 6)
    assert entry.mood_label in config_sample.mood["values"]
    assert len(config_sample.mood["values"]) == 5
    # Stress: 5 configured levels; scala salute: 8 configured levels (D2).
    assert len(config_sample.scales["stress"]["values"]) == 5
    assert len(config_sample.scales["scala_salute"]["values"]) == 8

    # ---- as_dict: keys directly usable by the SQLite upsert (Phase 3) ----- #
    payload = entry.as_dict()
    assert set(payload) == {
        "full_date", "date", "weekday", "time", "mood", "mood_label",
        "activities", "scales", "scala_salute", "note_title", "note", "source",
    }
    assert isinstance(payload["full_date"], str) and payload["full_date"] == "2026-09-21"
    assert isinstance(payload["mood"], int) and 1 <= payload["mood"] <= 5
    assert isinstance(payload["activities"], list)
    assert all(isinstance(a, str) for a in payload["activities"])
    assert isinstance(payload["scales"], list)
    for scale in payload["scales"]:
        assert set(scale) == {
            "scale_name", "label", "rank", "levels_count", "scale_key",
        }
    assert payload["scala_salute"] is None or set(payload["scala_salute"]) == {
        "label", "rank"
    }
    assert isinstance(payload["note_title"], str)
    assert isinstance(payload["note"], str)
    assert payload["source"] == "csv"

    # Molecular check on the 2026-09-21 record (the same row used in unit tests).
    assert entry.scala_salute == {"label": "discreto", "rank": 3}
    assert entry.scales == [
        {
            "scale_name": "Stress",
            "label": "Moderato",
            "rank": 3,
            "levels_count": 5,
            "scale_key": "stress",
        }
    ]
