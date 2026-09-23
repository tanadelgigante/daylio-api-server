"""Unit tests for the Fase 2 importers and normalizer (Tasks 2.1-2.3).

Covers the minimum requested in WORKPLAN §7 Task 2.4 "unit":
  - CSV parser: NBSP (U+00A0) separators in the `scales` column, mandatory
    `full_date`, unknown-column warning.
  - .daylio parser: ZIP + Base64 + JSON, month 0-based, `scaleValues`
    resolution, assets ignored (D11).
  - Normalizer: mood D1, Scala salute from activities/tags (D2), "vince la
    più recente" dedup (D3) with synthetic duplicate dates.
"""

from __future__ import annotations

from pathlib import Path

from daylio_api.config import AppConfig
from daylio_api.importers.base import ImportResult, detect_format
from daylio_api.importers.csv_importer import parse_csv
from daylio_api.importers.daylio_importer import parse_daylio
from daylio_api.importers.normalizer import normalize


def _write_csv(tmp_path: Path, content: str) -> Path:
    """Write a CSV fixture to a temp file (UTF-8, no BOM by default)."""
    path = tmp_path / "sample.csv"
    path.write_text(content, encoding="utf-8")
    return path


def _csv_record(
    date: str,
    time: str = "10:00",
    mood: str = "bene",
    acts: str = "",
    scales: str = "",
) -> dict:
    """Build a synthetic CSV-style raw record for normalize() unit tests."""
    return {
        "full_date": date,
        "date": "",
        "weekday": "",
        "time": time,
        "mood": mood,
        "activities": acts,
        "scales": scales,
        "note_title": "",
        "note": "",
        "source": "csv",
    }


def _daylio_record(
    date: str,
    daylio_id: int,
    datetime_ms: int,
    mood: int = 3,
    tags: list[dict] | None = None,
) -> dict:
    """Build a synthetic .daylio-style raw record for normalize() unit tests."""
    return {
        "full_date": date,
        "time": "",
        "mood": mood,
        "mood_custom_name": "",
        "activities": [tag["name"] for tag in (tags or [])],
        "tags": tags or [],
        "scales_raw": [],
        "scales": [],
        "note_title": "",
        "note": "",
        "source": "daylio",
        "_datetime_ms": datetime_ms,
        "_daylio_id": daylio_id,
    }


# --------------------------------------------------------------------------- #
# detect_format (D7)
# --------------------------------------------------------------------------- #
def test_detect_format_recognizes_csv_and_daylio(mini_csv, mini_daylio) -> None:
    assert detect_format(mini_csv) == "csv"
    assert detect_format(mini_daylio) == "daylio"
    assert detect_format(Path("/nonexistent/x.csv")) == "unknown"


# --------------------------------------------------------------------------- #
# CSV parser (Task 2.1)
# --------------------------------------------------------------------------- #
def test_csv_parser_reads_nbsp_scales(tmp_path, config_sample) -> None:
    """The real exports separate `scales` with U+00A0 (NBSP), not a plain space."""
    content = (
        "full_date,date,weekday,time,mood,activities,scales,note_title,note\n"
        "2026-09-21,21 settembre,lunedì,21:10,bene,"
        "gratitudine | discreto,Stress:\u00a0Moderato\u00a03/5,,\n"
    )
    result = parse_csv(_write_csv(tmp_path, content), config_sample)
    assert result.total_rows == 1
    assert result.errors == 0
    assert len(result.records) == 1
    assert "\u00a0" in result.records[0]["scales"]  # raw value keeps the NBSP

    entry = normalize(result, config_sample)[0]
    assert entry.scales == [
        {
            "scale_name": "Stress",
            "label": "Moderato",
            "rank": 3,
            "levels_count": 5,
            "scale_key": "stress",
        }
    ]
    assert entry.scala_salute == {"label": "discreto", "rank": 3}
    assert entry.activities == ["gratitudine"]


def test_csv_parser_skips_rows_without_full_date(tmp_path, config_sample) -> None:
    content = "full_date,mood\n2026-09-21,bene\n,male\n"
    result = parse_csv(_write_csv(tmp_path, content), config_sample)
    assert result.total_rows == 2
    assert result.errors == 1
    assert len(result.records) == 1
    assert result.records[0]["full_date"] == "2026-09-21"


def test_csv_parser_warns_on_unknown_column(tmp_path, config_sample) -> None:
    content = "full_date,mood,extra_col\n2026-09-21,bene,x\n"
    result = parse_csv(_write_csv(tmp_path, content), config_sample)
    assert result.warnings >= 1
    assert any("extra_col" in detail["message"] for detail in result.detail)


def test_csv_parser_rejects_missing_mandatory_header(tmp_path, config_sample) -> None:
    import pytest

    result = _write_csv(tmp_path, "date,mood\n21 settembre,bene\n")
    with pytest.raises(ValueError, match="full_date"):
        parse_csv(result, config_sample)


def test_csv_parser_utf8_sig_bom_and_header_case(tmp_path, config_sample) -> None:
    """D8.b: BOM is stripped (utf-8-sig) and header names are case-insensitive."""
    path = tmp_path / "bom_upper.csv"
    body = b"FULL_DATE,MOOD,ACTIVITIES\n2026-09-21,bene,gratitudine\n"
    path.write_bytes(b"\xef\xbb\xbf" + body)
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")  # real BOM present

    result = parse_csv(path, config_sample)
    assert result.total_rows == 1
    assert result.errors == 0
    assert len(result.records) == 1
    # Header names are lowercased/trimmed by the parser (`_clean_row`).
    assert set(result.records[0]) >= {"full_date", "mood", "activities", "source"}
    assert result.records[0]["full_date"] == "2026-09-21"
    assert result.records[0]["mood"] == "bene"
    assert result.records[0]["activities"] == "gratitudine"


# --------------------------------------------------------------------------- #
# .daylio parser (Task 2.2)
# --------------------------------------------------------------------------- #
def test_daylio_parser_zip_base64_json_month_zero_based(mini_daylio, config_sample) -> None:
    result = parse_daylio(mini_daylio, config_sample)
    assert result.total_rows == 3
    assert result.errors == 0
    assert len(result.records) == 3

    by_id = {record["_daylio_id"]: record for record in result.records}
    # Entry id=631: year 2026, month 8 (0-based = September), day 21.
    record = by_id[631]
    assert record["full_date"] == "2026-09-21"
    assert record["time"] == "21:10"
    assert record["_datetime_ms"] == 1790017813477
    assert {r["full_date"] for r in result.records} == {
        "2026-09-21",
        "2026-09-20",
        "2026-09-17",
    }


def test_daylio_parser_resolves_scale_values(mini_daylio, config_sample) -> None:
    result = parse_daylio(mini_daylio, config_sample)
    by_date = {record["full_date"]: record for record in result.records}
    stress = by_date["2026-09-21"]["scales"]
    assert stress == [
        {"scale_name": "Stress", "label": "Moderato", "fallback_rank": 3, "fallback_n": 5}
    ]

    entry = normalize(result, config_sample)
    normalized = {e.full_date: e for e in entry}
    assert normalized["2026-09-21"].scales[0]["rank"] == 3
    assert normalized["2026-09-21"].scales[0]["label"] == "Moderato"


def test_daylio_parser_ignores_assets(mini_daylio, config_sample) -> None:
    result = parse_daylio(mini_daylio, config_sample)
    assert all(record["_assets_count"] == 0 for record in result.records)
    assert result.warnings == 0  # no media in the mini fixture (D11)


def _write_daylio_archive(tmp_path: Path, doc: dict) -> Path:
    """Build a synthetic .daylio archive (ZIP -> Base64 -> JSON) in tmp_path."""
    import base64
    import json
    import zipfile

    payload = base64.b64encode(json.dumps(doc, ensure_ascii=False).encode("utf-8"))
    path = tmp_path / "synthetic.daylio"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("backup.daylio", payload)
    return path


def _synthetic_doc(day_entries: list[dict], scales: list[dict] | None = None) -> dict:
    """Minimal but structurally valid Daylio backup document (version 15)."""
    return {
        "version": 15,
        "customMoods": [{"id": i, "mood_group_id": i} for i in range(1, 6)],
        "tags": [
            {"id": 6, "name": "stanchezza", "id_tag_group": 1},
            {"id": 20, "name": "discreto", "id_tag_group": 5},
        ],
        "tag_groups": [{"id": 5, "name": "Scala salute"}],
        "scales": scales
        if scales is not None
        else [
            {
                "id": 1,
                "type": 0,
                "name": "Stress",
                "text_scale": {
                    "values": [
                        {"id": i, "name": n}
                        for i, n in enumerate(
                            ["Calma", "Basso", "Moderato", "Alto", "Sopraffatto"], 1
                        )
                    ]
                },
                "number_scale": {"min": 1, "max": 10},
            }
        ],
        "dayEntries": day_entries,
    }


def test_daylio_parser_counts_and_warns_ignored_assets(tmp_path, config_sample) -> None:
    """D11: assets are never imported; a non-empty assets array warns (D11)."""
    doc = _synthetic_doc(
        [
            {
                "id": 1,
                "year": 2026,
                "month": 8,  # 0-based -> September
                "day": 21,
                "hour": 10,
                "minute": 0,
                "datetime": 1790017813477,
                "mood": 2,
                "tags": [6],
                "scaleValues": [],
                "assets": [{"type": "photo", "url": "media/x.jpg"}],
                "note_title": "",
                "note": "",
            }
        ]
    )
    archive = _write_daylio_archive(tmp_path, doc)
    result = parse_daylio(archive, config_sample)

    assert result.total_rows == 1
    assert len(result.records) == 1
    record = result.records[0]
    # The asset itself is dropped from the canonical record (D11)...
    assert "assets" not in record
    assert record["_assets_count"] == 1
    # ...but counted in the traffic-light warning counter.
    assert result.warnings == 1
    assert any("asset" in detail["message"] for detail in result.detail)


def test_daylio_parser_resolves_numeric_scale_values() -> None:
    """number_scale values are resolved like text ones (fallback_rank = value)."""
    from daylio_api.importers.base import ImportResult
    from daylio_api.importers.daylio_importer import _resolve_scale_values

    scale_index = {
        7: {
            "id": 7,
            "type": 1,
            "name": "Energia",
            "number_scale": {"min": 0, "max": 10},
        }
    }
    entry = {
        "id": 42,
        "scaleValues": [
            {"id_scale": 7, "id_number_scale_value": 4},
        ],
    }
    result = ImportResult()
    resolved = _resolve_scale_values(entry, scale_index, result)
    assert resolved == [
        {
            "scale_name": "Energia",
            "label": "4",
            "fallback_rank": 4,
            "fallback_n": None,
        }
    ]
    assert result.warnings == 0


def test_daylio_parser_warns_unknown_id_scale(tmp_path, config_sample) -> None:
    """An unknown id_scale FK is kept as a warning, not an import error."""
    doc = _synthetic_doc(
        [
            {
                "id": 1,
                "year": 2026,
                "month": 8,
                "day": 21,
                "hour": 10,
                "minute": 0,
                "datetime": 1790017813477,
                "mood": 2,
                "tags": [6],
                "scaleValues": [{"id_scale": 99, "id_text_scale_value": 3}],
                "assets": [],
                "note_title": "",
                "note": "",
            }
        ]
    )
    archive = _write_daylio_archive(tmp_path, doc)
    result = parse_daylio(archive, config_sample)
    assert result.total_rows == 1
    assert result.errors == 0
    assert result.warnings == 1
    assert any("id_scale" in detail["message"] for detail in result.detail)


# --------------------------------------------------------------------------- #
# Normalizer (Task 2.3)
# --------------------------------------------------------------------------- #
def test_normalizer_scala_salute_from_csv_activities(mini_csv, config_sample) -> None:
    entries = {e.full_date: e for e in normalize(parse_csv(mini_csv, config_sample), config_sample)}
    assert entries["2026-09-21"].scala_salute == {"label": "discreto", "rank": 3}
    assert entries["2026-09-21"].activities == [
        "gratitudine",
        "contentezza",
        "noia",
        "ambiente affollato",
        "buon sonno",
    ]
    assert entries["2026-09-17"].scala_salute == {"label": "terribile", "rank": 8}
    assert entries["2026-09-17"].activities == ["tristezza", "stanchezza"]


def test_normalizer_scala_salute_from_daylio_tags(mini_daylio, config_sample) -> None:
    entries = {
        e.full_date: e
        for e in normalize(parse_daylio(mini_daylio, config_sample), config_sample)
    }
    assert entries["2026-09-21"].scala_salute == {"label": "discreto", "rank": 3}
    assert entries["2026-09-17"].scala_salute == {"label": "terribile", "rank": 8}
    assert entries["2026-09-17"].activities == ["stanchezza"]


def test_normalizer_mood_d1_mapping(config_sample) -> None:
    result = ImportResult(records=[_csv_record("2026-09-21", mood="alla grande")])
    entries = normalize(result, config_sample)
    assert entries[0].mood_level == 1
    assert entries[0].mood_label == "alla grande"


def test_normalizer_unknown_mood_warns(config_sample) -> None:
    result = ImportResult(records=[_csv_record("2026-09-21", mood="sconosciuto")])
    result.total_rows = 1
    entries = normalize(result, config_sample)
    assert entries[0].mood_level == 0
    assert result.warnings >= 1


def test_normalizer_unknown_scale_warns(config_sample) -> None:
    result = ImportResult(records=[_csv_record("2026-09-21", scales="Foo: Bar 1/3")])
    result.total_rows = 1
    entries = normalize(result, config_sample)
    assert entries[0].scales == []
    assert result.warnings >= 1


def test_normalizer_keeps_most_recent_csv_row(config_sample) -> None:
    """CSV dedup: most recent `time` wins; ties -> later row in file (D3)."""
    result = ImportResult(
        records=[
            _csv_record("2025-01-01", time="15:44", mood="così così", acts="stanchezza"),
            _csv_record("2025-01-01", time="20:02", mood="così così", acts="stanchezza"),
        ]
    )
    result.total_rows = 2
    entries = normalize(result, config_sample)
    assert len(entries) == 1
    assert entries[0].time == "20:02"
    assert result.imported == 1
    assert result.skipped == 1


def test_normalizer_csv_tie_break_last_in_file(config_sample) -> None:
    result = ImportResult(
        records=[
            _csv_record("2025-01-01", time="20:02", mood="bene"),
            _csv_record("2025-01-01", time="20:02", mood="male"),
        ]
    )
    result.total_rows = 2
    entries = normalize(result, config_sample)
    assert len(entries) == 1
    assert entries[0].mood_label == "male"  # second row (later in file) wins


def test_normalizer_keeps_most_recent_daylio_entry(config_sample) -> None:
    """Daylio dedup: most recent `datetime` epoch ms wins; ties -> greater id."""
    result = ImportResult(
        records=[
            _daylio_record("2025-01-01", daylio_id=1, datetime_ms=1000),
            _daylio_record("2025-01-01", daylio_id=2, datetime_ms=2000),
        ]
    )
    result.total_rows = 2
    entries = normalize(result, config_sample)
    assert len(entries) == 1
    assert entries[0].raw["_daylio_id"] == 2
    assert result.skipped == 1


def test_normalizer_daylio_tie_break_greater_id(config_sample) -> None:
    result = ImportResult(
        records=[
            _daylio_record("2025-01-01", daylio_id=7, datetime_ms=5000),
            _daylio_record("2025-01-01", daylio_id=9, datetime_ms=5000),
        ]
    )
    result.total_rows = 2
    entries = normalize(result, config_sample)
    assert len(entries) == 1
    assert entries[0].raw["_daylio_id"] == 9


def test_normalizer_derives_date_and_weekday(config_sample) -> None:
    result = ImportResult(records=[_csv_record("2026-09-21")])
    entries = normalize(result, config_sample)
    assert entries[0].date == "2026-09-21"
    assert entries[0].weekday == "Monday"  # 2026-09-21 is a Monday


def test_config_argument_is_optional(tmp_path) -> None:
    """When config is None the default example configuration is loaded."""
    from daylio_api import config as config_module

    cfg = config_module.load()
    assert isinstance(cfg, AppConfig)
    content = "full_date,mood\n2026-09-21,bene\n"
    result = parse_csv(_write_csv(tmp_path, content))  # config=None
    entries = normalize(result)  # config=None
    assert entries[0].mood_level == 2
