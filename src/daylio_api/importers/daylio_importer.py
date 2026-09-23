""".daylio (ZIP + Base64 + JSON) importer for Daylio backups (Task 2.2).

=============================== MAINTENANCE NOTES ===============================
Module   : src/daylio_api/importers/daylio_importer.py
Scope    : Fase 2 (Task 2.2) — replaces the Fase 0 placeholder. Implements D7:

  1. The file is a ZIP archive containing a single ``backup.daylio`` payload;
     the first four bytes are the ZIP magic ``PK\\x03\\x04`` (see
     ``base.detect_format``).
  2. ``backup.daylio`` is Base64 text; decoding it yields the Daylio JSON
     document (backup version 15 today, parsed defensively for future
     versions, risk R1).
  3. Mapping from JSON to the canonical raw record:
     - ``customMoods``  : mood id -> mood object. The real backup stores an
       empty ``custom_name``, so the Italian label is resolved by the
       normalizer from the config (id = rank, D1).
     - ``tags`` / ``tag_groups`` : activity labels and their group; a record
       keeps ``activities`` (names) plus ``tags`` (name + group) so the
       normalizer can extract the "Scala salute" (tag_group=5, D2).
     - ``scales``       : text_scale values (ordered names) and number_scale
       min/max; ``scaleValues`` FK refs ``(id_scale, id_text_scale_value)``
       are resolved here into ``scales`` (see _resolve_scale_values) and kept
       raw in ``scales_raw`` for audit.
     - ``dayEntries``   : date fields are ``year/month/day`` with **month
       0-based** (January=0, risk R11) and ``datetime`` epoch **milliseconds**
       (local-time consistent with the CSV ``time`` column).
     - ``assets``       : media array — ignored by default (D11); each asset
       is counted in the ``warning`` traffic light.
===============================================================================
"""

from __future__ import annotations

import base64
import json
import zipfile
from pathlib import Path
from typing import Any

from daylio_api import config as config_module
from daylio_api.config import AppConfig
from daylio_api.importers.base import FORMAT_DAYLIO, ImportResult
from daylio_api.logger import get_logger

log = get_logger("importers.daylio")

#: Name of the Base64 payload inside the .daylio ZIP archive (D7).
_BACKUP_MEMBER = "backup.daylio"


def parse_daylio(path: str | Path, config: dict[str, Any] | None = None) -> ImportResult:
    """Parse a .daylio backup archive into an :class:`ImportResult`.

    Args:
        path: Path to the ``.daylio`` file (ZIP containing ``backup.daylio``).
        config: Validated ``AppConfig`` (defaults to the project example
            config when None).

    Raises:
        ValueError: on non-ZIP input, missing ``backup.daylio`` member, or
            invalid Base64/JSON payload.
    """
    config if isinstance(config, AppConfig) else config_module.load()
    file_path = Path(path)
    result = ImportResult(source="upload", filename=str(file_path), status="running")

    doc = _read_backup_document(file_path)
    result.total_rows = len(doc.get("dayEntries") or [])

    _index_moods(doc)
    tags = _index_tags(doc)
    scales = _index_scales(doc)

    for raw_index, entry in enumerate(doc.get("dayEntries") or [], start=1):
        record = _entry_to_record(entry, tags, scales, result, raw_index)
        result.records.append(record)

    log.info(
        "daylio %s parsed: %d entries, %d records, %d warnings",
        file_path,
        result.total_rows,
        len(result.records),
        result.warnings,
    )
    result.finish()
    return result


# --------------------------------------------------------------------------- #
# Internal helpers
# --------------------------------------------------------------------------- #
def _read_backup_document(path: Path) -> dict[str, Any]:
    """Open the ZIP, Base64-decode ``backup.daylio`` and parse the JSON."""
    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            if _BACKUP_MEMBER not in names:
                raise ValueError(
                    f".daylio {path}: archive does not contain {_BACKUP_MEMBER!r} "
                    f"(members: {names[:5]})"
                )
            payload = zf.read(_BACKUP_MEMBER)
    except zipfile.BadZipFile as exc:
        raise ValueError(f".daylio {path}: not a valid ZIP archive (D7): {exc}") from exc
    try:
        raw = base64.b64decode(payload).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError(f".daylio {path}: {_BACKUP_MEMBER!r} is not valid Base64 text") from exc
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f".daylio {path}: {_BACKUP_MEMBER!r} contains invalid JSON: {exc}"
        ) from exc
    version = doc.get("version")
    if version != 15:
        log.info("daylio %s: backup version %r (reference is 15, parse defensively)", path, version)
    return doc


def _index_moods(doc: dict[str, Any]) -> dict[int, dict[str, Any]]:
    """Index ``customMoods`` by numeric id (D1: id equals the rank 1..5)."""
    return {
        int(mood["id"]): mood
        for mood in doc.get("customMoods") or []
        if isinstance(mood, dict) and "id" in mood
    }


def _index_tags(doc: dict[str, Any]) -> dict[int, dict[str, Any]]:
    """Index ``tags`` by numeric id (id -> {name, id_tag_group})."""
    return {
        int(tag["id"]): tag
        for tag in doc.get("tags") or []
        if isinstance(tag, dict) and "id" in tag
    }


def _index_scales(doc: dict[str, Any]) -> dict[int, dict[str, Any]]:
    """Index ``scales`` by numeric id (id -> scale object)."""
    return {
        int(scale["id"]): scale
        for scale in doc.get("scales") or []
        if isinstance(scale, dict) and "id" in scale
    }


def _entry_to_record(
    entry: dict[str, Any],
    tags: dict[int, dict[str, Any]],
    scales: dict[int, dict[str, Any]],
    result: ImportResult,
    row_number: int,
) -> dict[str, Any]:
    """Convert one ``dayEntries`` JSON object to the canonical raw record."""
    full_date = _entry_date(entry)
    tag_ids = [int(tid) for tid in (entry.get("tags") or []) if str(tid).isdigit()]
    tag_infos = [
        {
            "name": str(tags[tid].get("name") or f"tag:{tid}"),
            "group": int(tags[tid].get("id_tag_group", 0)),
        }
        for tid in tag_ids
        if tid in tags
    ]
    assets = entry.get("assets") or []
    if assets:
        result.warn(f"entry {entry.get('id')}: {len(assets)} asset(s) ignored (D11)")

    return {
        "full_date": full_date,
        "date": "",  # derived by the normalizer (D8.a)
        "weekday": "",
        "time": f"{int(entry.get('hour', 0)):02d}:{int(entry.get('minute', 0)):02d}",
        "mood": int(entry.get("mood", 0)),
        "mood_custom_name": str((entry.get("customMoodName") or "") or ""),
        "activities": [tag["name"] for tag in tag_infos],
        "tags": tag_infos,
        "scales_raw": [dict(sv) for sv in (entry.get("scaleValues") or [])],
        "scales": _resolve_scale_values(entry, scales, result),
        "note_title": str(entry.get("note_title") or ""),
        "note": str(entry.get("note") or ""),
        "is_favorite": bool(entry.get("isFavorite", False)),
        "source": FORMAT_DAYLIO,
        "_datetime_ms": int(entry.get("datetime", 0)),
        "_daylio_id": int(entry.get("id", 0)),
        "_daylio_row": row_number,
        "_assets_count": len(assets),
    }


def _entry_date(entry: dict[str, Any]) -> str:
    """Build ``YYYY-MM-DD`` from a dayEntries date (month is 0-based, R11)."""
    try:
        year = int(entry.get("year", 0))
        month = int(entry.get("month", 0)) + 1  # month 0-based -> 1-based
        day = int(entry.get("day", 0))
        return f"{year:04d}-{month:02d}-{day:02d}"
    except (TypeError, ValueError):
        return ""


def _resolve_scale_values(
    entry: dict[str, Any], scales: dict[int, dict[str, Any]], result: ImportResult
) -> list[dict[str, Any]]:
    """Resolve ``scaleValues`` FK references with the scale definitions.

    Each output dict keeps the source-facing fields plus fallback rank/count:
    ``{scale_name, label, fallback_rank, fallback_n}``; the normalizer turns
    them into the canonical ``{scale_name, label, rank, levels_count}`` using
    the config (config is the rank authority, D2/D8.c).
    """
    resolved: list[dict[str, Any]] = []
    for sv in entry.get("scaleValues") or []:
        scale = scales.get(int(sv.get("id_scale", 0)))
        if scale is None:
            result.warn(f"entry {entry.get('id')}: unknown id_scale {sv.get('id_scale')}")
            resolved.append(
                {
                    "scale_name": f"scale_{sv.get('id_scale')}",
                    "label": str(sv),
                    "fallback_rank": None,
                    "fallback_n": None,
                }
            )
            continue
        name = str(scale.get("name") or f"scale_{sv.get('id_scale')}")
        text_values = (scale.get("text_scale") or {}).get("values") or []
        fallback_n = len(text_values)
        matched: tuple[str, int] | None = None
        for position, text_value in enumerate(text_values, start=1):
            if int(text_value.get("id", -1)) == int(sv.get("id_text_scale_value", -1)):
                matched = (str(text_value.get("name") or ""), position)
                break
        if matched is not None:
            resolved.append(
                {
                    "scale_name": name,
                    "label": matched[0],
                    "fallback_rank": matched[1],
                    "fallback_n": fallback_n,
                }
            )
        elif "id_number_scale_value" in sv and sv["id_number_scale_value"] is not None:
            value = int(sv["id_number_scale_value"])
            resolved.append(
                {
                    "scale_name": name,
                    "label": str(value),
                    "fallback_rank": value,
                    "fallback_n": None,
                }
            )
        else:
            resolved.append(
                {
                    "scale_name": name,
                    "label": str(sv),
                    "fallback_rank": None,
                    "fallback_n": None,
                }
            )
    return resolved
