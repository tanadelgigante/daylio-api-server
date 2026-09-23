"""Pure-stdlib CSV importer for Daylio exports (Task 2.1).

=============================== MAINTENANCE NOTES ===============================
Module   : src/daylio_api/importers/csv_importer.py
Scope    : Fase 2 (Task 2.1) — replaces the Fase 0 placeholder. Parses a Daylio
           CSV export with the stdlib ``csv`` module (no pandas, D5).

Behaviour:
  - Encoding : ``utf-8-sig`` so the UTF-8 BOM (``EF BB BF``) is stripped (D8.b).
  - Header   : only ``full_date`` is mandatory (D8.b); a missing mandatory
    column raises ``ValueError`` with a clear message. ``date``/``weekday``
    columns are redundant and kept raw for audit only (D8.a ignores them).
  - Columns  : header names are lowercased/trimmed; a column that is not
    declared in the config (required+optional) produces a warning, the data
    is still kept.
  - Rows     : every data row counts towards ``total_rows``; a row without a
    non-empty ``full_date`` is rejected (``errors``) and skipped.
  - Notes    : no scale parsing here — the raw ``scales`` string (possibly
    with U+00A0 NBSP separators in the real exports) is passed to the
    normalizer (Task 2.3).
===============================================================================
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from daylio_api import config as config_module
from daylio_api.config import AppConfig
from daylio_api.importers.base import FORMAT_CSV, ImportResult
from daylio_api.logger import get_logger

log = get_logger("importers.csv")


def parse_csv(path: str | Path, config: dict[str, Any] | None = None) -> ImportResult:
    """Parse a Daylio CSV export into an :class:`ImportResult`.

    Args:
        path: Path to the ``.csv`` file (UTF-8, optional BOM).
        config: Validated ``AppConfig`` (defaults to the project example
            config when None).

    Raises:
        ValueError: if the file cannot be read or a mandatory header column
            (``full_date``) is missing.
    """
    cfg = config if isinstance(config, AppConfig) else config_module.load()
    file_path = Path(path)
    result = ImportResult(source="upload", filename=str(file_path), status="running")

    known_columns = _known_columns(cfg)
    required = [str(col) for col in (cfg.columns or {}).get("required") or []]

    try:
        with file_path.open(encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            header = [str(h).strip().lower() for h in (reader.fieldnames or []) if h]
            if not header:
                raise ValueError(
                    f"CSV {file_path}: empty header, expected at least 'full_date' (D8.b)"
                )
            missing = [col for col in required if col not in header]
            if missing:
                raise ValueError(
                    f"CSV {file_path}: mandatory column(s) missing: {', '.join(missing)} "
                    "(D8.b: only 'full_date' is mandatory)"
                )
            for column in header:
                if column not in known_columns:
                    log.warning("CSV %s: column %r is not declared in config", file_path, column)
                    result.warn(f"unexpected CSV column {column!r} (not declared in config)")

            for raw_row in reader:
                result.total_rows += 1
                record = _clean_row(raw_row)
                full_date = str(record.get("full_date") or "").strip()
                if not full_date:
                    log.warning(
                        "CSV %s: row %d skipped, missing full_date", file_path, result.total_rows
                    )
                    result.add_error(
                        f"row {result.total_rows}: missing 'full_date'",
                        row_number=result.total_rows,
                    )
                    continue
                record["source"] = FORMAT_CSV
                result.records.append(record)
    except OSError as exc:
        raise ValueError(f"cannot read CSV {file_path}: {exc}") from exc

    log.info(
        "CSV %s parsed: %d rows, %d records, %d errors",
        file_path,
        result.total_rows,
        len(result.records),
        result.errors,
    )
    result.finish()
    return result


def _known_columns(cfg: AppConfig) -> set[str]:
    """Return the set of columns declared by the config (required+optional)."""
    columns = cfg.columns or {}
    required = [str(col) for col in columns.get("required") or []]
    optional = [str(col) for col in columns.get("optional") or []]
    return set(required) | set(optional)


def _clean_row(raw_row: dict[str, Any]) -> dict[str, Any]:
    """Lowercase/trim header names and trim string values of one CSV row."""
    cleaned: dict[str, Any] = {}
    for key, value in raw_row.items():
        if key is None or str(key).strip() == "":
            continue
        name = str(key).strip().lower()
        cleaned[name] = str(value).strip() if isinstance(value, str) else value
    return cleaned
