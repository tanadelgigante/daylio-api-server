"""Integration tests for the CSV importer on the real 630-row sample (Task 2.4).

Expected facts verified on ``sample-data/daylio_export_2026_09_22.csv``
(WORKPLAN §2.3):
  - 630 data rows, header with UTF-8 BOM, ``scales`` separated by U+00A0 NBSP;
  - raw mood counts 31/282/174/119/24 (alla grande/bene/così così/male/malissimo);
  - ``scales`` present in 58 rows, distribution 3/5=22, 2/5=17, 4/5=15, 5/5=2,
    1/5=2;
  - two duplicate dates (2025-01-01, 2025-01-03) -> 628 unique days after the
    "vince la più recente" dedup (D3), mood counts shift to 31/281/173/119/24
    because both duplicate winners keep the same mood of their date;
  - Scala salute levels appear inside the ``activities`` column (D2).
"""

from __future__ import annotations

from collections import Counter

from daylio_api.importers.csv_importer import parse_csv
from daylio_api.importers.normalizer import normalize

EXPECTED_RAW_MOOD_COUNTS = {
    "alla grande": 31,
    "bene": 282,
    "così così": 174,
    "male": 119,
    "malissimo": 24,
}
#: After daily dedup the two duplicate dates shrink `bene` by 1 and
#: `così così` by 1 (both duplicates of a date share the same mood).
EXPECTED_UNIQUE_MOOD_COUNTS = {1: 31, 2: 281, 3: 173, 4: 119, 5: 24}
EXPECTED_SCALES_COUNTS = {"1/5": 2, "2/5": 17, "3/5": 22, "4/5": 15, "5/5": 2}
#: 8-level Scala salute (D2); only 5 levels are actually used in the sample.
EXPECTED_SALUTE_SEEN = {"discreto", "insufficiente", "mediocre", "pessimo", "sufficiente"}


def test_csv_sample_raw_counts(sample_csv, config_sample) -> None:
    result = parse_csv(sample_csv, config_sample)
    assert result.total_rows == 630
    assert result.errors == 0
    assert result.warnings == 0
    assert len(result.records) == 630

    raw_moods = Counter(record["mood"] for record in result.records)
    assert dict(raw_moods) == EXPECTED_RAW_MOOD_COUNTS


def test_csv_sample_scales_distribution(sample_csv, config_sample) -> None:
    result = parse_csv(sample_csv, config_sample)
    rows_with_scales = [
        record for record in result.records if (record.get("scales") or "").strip()
    ]
    assert len(rows_with_scales) == 58
    # Real export uses U+00A0 as separator: "Stress:\xa0Moderato\xa03/5".
    assert any("\u00a0" in record["scales"] for record in rows_with_scales)

    suffix = Counter(
        record["scales"].replace("\u00a0", " ").rsplit(" ", 1)[-1]
        for record in rows_with_scales
    )
    assert dict(suffix) == EXPECTED_SCALES_COUNTS


def test_csv_sample_scala_salute_in_activities(sample_csv, config_sample) -> None:
    result = parse_csv(sample_csv, config_sample)
    salute_values = set(config_sample.scales["scala_salute"]["values"])
    seen = {
        activity
        for record in result.records
        for activity in record["activities"].split(" | ")
        if activity in salute_values
    }
    assert seen == EXPECTED_SALUTE_SEEN


def test_csv_sample_normalized_dedup_and_counts(sample_csv, config_sample) -> None:
    result = parse_csv(sample_csv, config_sample)
    entries = normalize(result, config_sample)

    # 630 rows -> 628 unique days (2025-01-01 and 2025-01-03 deduplicated).
    assert len(entries) == 628
    assert result.imported == 628
    assert result.skipped == 2

    by_date = {entry.full_date: entry for entry in entries}
    # "Vince la più recente": the most recent `time` of each duplicate date.
    assert by_date["2025-01-01"].time == "20:02"
    assert by_date["2025-01-03"].time == "21:36"

    mood_counts = Counter(entry.mood_level for entry in entries)
    assert dict(mood_counts) == EXPECTED_UNIQUE_MOOD_COUNTS

    stress_entries = [entry for entry in entries if entry.scales]
    assert len(stress_entries) == 58
    stress_ranks = Counter(entry.scales[0]["rank"] for entry in stress_entries)
    assert dict(stress_ranks) == {1: 2, 2: 17, 3: 22, 4: 15, 5: 2}

    salute_entries = [entry for entry in entries if entry.scala_salute]
    assert len(salute_entries) == 265
    salute_ranks = Counter(entry.scala_salute["rank"] for entry in salute_entries)
    assert sorted(salute_ranks) == [3, 4, 5, 6, 7]
