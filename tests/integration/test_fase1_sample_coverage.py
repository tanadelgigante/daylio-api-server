"""Fase 1 acceptance — criterion 2: the default config covers the real samples.

The configured scales/columns (mood 1..5, `scales` column Stress k/5, Scala
salute from tag_group=5 / activities, 8 levels) must be able to represent
EVERY value that actually appears in the real sample data:
``sample-data/daylio_export_2026_09_22.csv`` and
``sample-data/backup_2026_09_22.daylio`` (WORKPLAN §2.3 / D1/D2/D8).
"""

from __future__ import annotations

import base64
import csv
import json
import zipfile

CONFIG_DEFAULT_MOOD = {"alla grande", "bene", "così così", "male", "malissimo"}
CONFIG_DEFAULT_STRESS = ["Calma", "Basso", "Moderato", "Alto", "Sopraffatto"]
CONFIG_DEFAULT_SALUTE = [
    "ottimo",
    "buono",
    "discreto",
    "sufficiente",
    "mediocre",
    "insufficiente",
    "pessimo",
    "terribile",
]


def test_config_covers_real_csv_sample(sample_csv, config_sample) -> None:
    """Every mood/scales/scala-salute value in the real CSV is in the config."""
    with open(sample_csv, encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 630, "sample CSV unchanged expectation (630 rows)"

    # D1: the 5 mood labels present in the CSV are exactly the configured ones.
    csv_moods = {row["mood"] for row in rows}
    assert csv_moods == CONFIG_DEFAULT_MOOD
    assert csv_moods == set(config_sample.mood["values"])

    # D8.c: the `scales` column is "Stress: <Level> k/5" and each level label
    # must be one of the configured Stress values.
    configured_stress = set(config_sample.scales["stress"]["values"])
    assert configured_stress == set(CONFIG_DEFAULT_STRESS)
    scales_rows = [row for row in rows if row.get("scales", "").strip()]
    assert len(scales_rows) == 58, "58 rows with a `scales` value (sample)"
    for row in scales_rows:
        raw = row["scales"]
        # The CSV uses U+00A0 (non-breaking space) as separator:
        # "Stress:\xa0Moderato\xa03/5" -> "Stress: Moderato 3/5".
        normalized = raw.replace("\xa0", " ")
        assert normalized.startswith("Stress:"), normalized
        suffix = normalized.split(":", 1)[1].strip()
        assert suffix.endswith("/5"), normalized
        level, _k5 = suffix.split(" ")
        assert level in configured_stress, f"{level} not configured"

    # D2: every Scala salute level that appears inside `activities` is one of
    # the 8 configured levels (the sample contains a subset of them).
    configured_salute = set(config_sample.scales["scala_salute"]["values"])
    assert configured_salute == set(CONFIG_DEFAULT_SALUTE)
    salute_seen: set[str] = set()
    for row in rows:
        for activity in row.get("activities", "").split(" | "):
            if activity in configured_salute:
                salute_seen.add(activity)
    assert salute_seen, "no Scala salute level found in CSV activities"
    assert salute_seen <= configured_salute


def test_config_covers_real_daylio_sample(sample_daylio, config_sample) -> None:
    """Moods (1..5), Stress text levels and tag_group=5 match the .daylio JSON."""
    with zipfile.ZipFile(sample_daylio) as zf:
        raw = base64.b64decode(zf.read("backup.daylio")).decode("utf-8")
    doc = json.loads(raw)
    assert doc["version"] == 15
    assert len(doc["dayEntries"]) == 630

    # D1: customMoods are 5 entries with ids 1..5 (1=best ... 5=worst).
    assert {int(mood["id"]) for mood in doc["customMoods"]} == {1, 2, 3, 4, 5}

    # Stress scale: the only scale, with text levels matching the config.
    scales = doc["scales"]
    assert len(scales) == 1 and scales[0]["name"] == "Stress"
    text_values = [value["name"] for value in scales[0]["text_scale"]["values"]]
    assert text_values == CONFIG_DEFAULT_STRESS
    assert text_values == config_sample.scales["stress"]["values"]

    # D2: tag_group=5 contains exactly the 8 Scala salute levels (config order
    # is authoritative; the JSON `order` field is intentionally not trusted).
    group5 = {
        tag["name"]
        for tag in doc["tags"]
        if tag.get("id_tag_group") == 5
    }
    configured = set(config_sample.scales["scala_salute"]["values"])
    assert group5 == configured
    assert len(group5) == 8

    # The real sample stores stress values only through id_scale=1 and only in
    # 58 entries (consistent with the CSV `scales` column).
    entries_with_scales = [
        entry for entry in doc["dayEntries"] if entry.get("scaleValues")
    ]
    assert len(entries_with_scales) == 58
    used_ids = {
        value["id_scale"]
        for entry in entries_with_scales
        for value in entry["scaleValues"]
    }
    assert used_ids == {1}
