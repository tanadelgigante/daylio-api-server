"""Integration tests for the .daylio importer on the real backup sample (Task 2.4).

Expected facts verified on ``sample-data/backup_2026_09_22.daylio``
(WORKPLAN §2.3, D9/D11):
  - the file is a real Daylio ZIP archive: ``backup.daylio`` (Base64) then a
    Daylio JSON payload, detected through the ``PK\\x03\\x04`` magic (D7);
  - 630 day entries; ``year``/``month`` are 1-based in the archive while the
    schedule is 0-based inside Daylio (month 8 = September for 2026, D9);
  - ``scaleValues`` only reference scale id 1 (Stress) and appear in 58
    entries; distribution matches the CSV ``scales`` column exactly so the two
    formats load the same Stress ranks (58 records, ranks 1..5);
  - Scala salute comes from tag-group 5 ("Scala salute") and is present in
    265 entries with ranks 3..7 (D2);
  - mood label of the real export is resolved from the configured scale
    (mood ``custom_name`` is empty in this backup, E1) -> 31/281/173/119/24;
  - two duplicate dates (2025-01-01, 2025-01-03) -> 628 unique days, "vince la
    più recente" picks the entry with the most recent ``datetime`` (D3);
  - assets/media are ignored (D11) -> no warnings, ``assets`` not imported.

"""

from __future__ import annotations

from collections import Counter

from daylio_api.importers.daylio_importer import parse_daylio
from daylio_api.importers.normalizer import normalize

#: Distribution of the resolved Stress ranks over the 58 scaleValues records
#: (identical to the CSV ``scales`` column so the two formats coincide).
EXPECTED_STRESS_RANKS = {1: 2, 2: 17, 3: 22, 4: 15, 5: 2}
#: Scala salute levels actually used in the sample (ranks 3..7 of 8, D2).
EXPECTED_SALUTE_RANKS = {3, 4, 5, 6, 7}
#: Daylio id of the winning (most recent) entry for the duplicate dates.
EXPECTED_DUP_WINNERS = {
    "2025-01-01": "20:02",
    "2025-01-03": "21:36",
}


def test_daylio_sample_raw_parsing(sample_daylio, config_sample) -> None:
    result = parse_daylio(sample_daylio, config_sample)
    assert result.total_rows == 630
    assert result.errors == 0
    assert result.warnings == 0
    assert len(result.records) == 630


def test_daylio_sample_month_is_zero_based(sample_daylio, config_sample) -> None:
    """September 2026 is stored as month 8 in the archive (0-based, D9)."""
    result = parse_daylio(sample_daylio, config_sample)
    # Several entries from the first days of September 2026.
    dates = {record["full_date"] for record in result.records}
    assert {"2026-09-21", "2026-09-20", "2026-09-17"} <= dates


def test_daylio_sample_stress_and_salute(sample_daylio, config_sample) -> None:
    """Stress/Scala-salute ranks coincide with the CSV after normalization.

    The raw parse keeps ``scales`` (with ``fallback_rank``) only; the canonical
    ``rank`` and the derived ``scala_salute`` exist on normalized entries, so
    the assertions mirror the CSV integration test — the same distribution is
    the whole point of Task 2.4 (D2/D8).
    """
    result = parse_daylio(sample_daylio, config_sample)
    entries = normalize(result, config_sample)

    stress_entries = [entry for entry in entries if entry.scales]
    assert len(stress_entries) == 58
    ranks = Counter(entry.scales[0]["rank"] for entry in stress_entries)
    assert dict(ranks) == EXPECTED_STRESS_RANKS

    salute_entries = [entry for entry in entries if entry.scala_salute]
    assert len(salute_entries) == 265
    salute_ranks = {entry.scala_salute["rank"] for entry in salute_entries}
    assert salute_ranks == EXPECTED_SALUTE_RANKS


def test_daylio_sample_normalized_counts(sample_daylio, config_sample) -> None:
    result = parse_daylio(sample_daylio, config_sample)
    entries = normalize(result, config_sample)

    assert len(entries) == 628
    assert result.imported == 628
    assert result.skipped == 2

    mood_counts = Counter(entry.mood_level for entry in entries)
    assert dict(mood_counts) == {1: 31, 2: 281, 3: 173, 4: 119, 5: 24}


def test_daylio_sample_duplicate_winners(sample_daylio, config_sample) -> None:
    result = parse_daylio(sample_daylio, config_sample)
    entries = normalize(result, config_sample)
    by_date = {entry.full_date: entry for entry in entries}
    # "Vince la più recente": the winning entry of each duplicate date keeps
    # the exact same time as its CSV counterpart (D3, coincidence).
    assert {d: by_date[d].time for d in EXPECTED_DUP_WINNERS} == EXPECTED_DUP_WINNERS
