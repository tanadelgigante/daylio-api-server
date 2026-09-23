"""Record normalizer: canonical daily entries from any importer (Task 2.3).

=============================== MAINTENANCE NOTES ===============================
Module   : src/daylio_api/importers/normalizer.py
Scope    : Fase 2 (Task 2.3) — replaces the Fase 0 placeholder. Consumes an
           ``ImportResult`` produced by either ``csv_importer`` or
           ``daylio_importer`` and produces one :class:`NormalizedEntry` per
           day (D3).

Rules implemented:
  - Mood (D1)     : CSV label -> rank via ``config.mood.values``; .daylio
    id -> rank via the same map (id == rank). Unknown labels produce a
    warning and ``mood_level = 0``.
  - Activities    : a ``" | "``-separated string (CSV) or a list (.daylio) is
    trimmed into a clean list. The separator comes from
    ``columns.options.activities.separator`` when configured, else ``" | "``.
  - Scala salute (D2) : extracted from the activities (CSV, matched by level
    label) or from the tags whose group equals the configured ``tag-group:N``
    of ``scales.scala_salute`` (.daylio). Rank comes from the config order
    (1=ottimo .. 8=terribile); the level is removed from ``activities``.
  - Scales (D8.c) : the CSV ``scales`` column ``"Name: Level k/N"`` and the
    .daylio ``scaleValues`` are folded into ``{scale_name, label, rank,
    levels_count, scale_key}``. Rank/levels_count are resolved from the
    config (config is the scale order authority).
  - Dedup (D3)    : "vince la più recente" happens BEFORE normalization:
      * CSV    : the most recent ``time`` (HH:MM); ties resolved by the last
        row in file order. In the real exports the file is time-ascending,
        so this coincides with "the last row of the file with that date".
      * .daylio: the entry with the greatest ``datetime`` (epoch ms); ties
        resolved by the greatest entry ``id``.
    The chosen rule is documented: it is the *most coherent* replication of
    D3 ("most recent by time, then datetime/id") and it guarantees the CSV
    and .daylio pipelines select the same winning entry for every date.
===============================================================================
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from daylio_api import config as config_module
from daylio_api.config import AppConfig, parse_tag_group, scale_label
from daylio_api.importers.base import ImportResult, NormalizedEntry
from daylio_api.logger import get_logger

log = get_logger("importers.normalizer")

#: Default activities separator when no ``columns.options`` is configured (D8.d).
_DEFAULT_ACTIVITIES_SEPARATOR = " | "
#: Runs of spaces/NBSP collapsed before scale parsing.
_WS_RE = re.compile(r"\s+")


def normalize(
    result: ImportResult,
    config: dict[str, Any] | None = None,
) -> list[NormalizedEntry]:
    """Normalize an ``ImportResult`` into one daily record per ``full_date``.

    Args:
        result: Output of ``parse_csv``/``parse_daylio`` (mutated: the
            ``imported``/``skipped``/``warnings`` counters are updated).
        config: Validated ``AppConfig`` (defaults to the project example).

    Returns:
        Deduplicated records sorted by ``full_date``.

    MAINTENANCE NOTES:
        - ``normalize`` is single-use per ``ImportResult``: counters are
          reset at the beginning so repeated calls stay idempotent.
        - Duplicate-loser rows are counted in ``result.skipped``; invalid
          rows were already counted as ``errors`` by the parser.
    """
    cfg = config if isinstance(config, AppConfig) else config_module.load()
    result.imported = 0
    result.skipped = 0

    winners = _dedupe_daily(result)
    entries: list[NormalizedEntry] = []
    for record in winners:
        entries.append(_normalize_record(record, cfg, result))

    lost = len(result.records) - len(entries)
    result.imported = len(entries)
    result.skipped = max(0, lost)
    log.info("normalizer: %d unique days, %d duplicates dropped", len(entries), lost)
    return sorted(entries, key=lambda entry: entry.full_date)


# --------------------------------------------------------------------------- #
# Deduplication: "vince la più recente" (D3)
# --------------------------------------------------------------------------- #
def _dedupe_daily(result: ImportResult) -> list[dict[str, Any]]:
    """Keep one record per ``full_date`` using the source-specific winner key.

    CSV: ``(time, file_index)`` -> most recent ``time``, last row as tie-break.
    .daylio: ``(datetime_ms, id)`` -> most recent import timestamp, greater
    ``id`` as tie-break. See the module MAINTENANCE NOTES for the rationale
    (this is the coherent replication of D3 and matches the acceptance test).
    """
    winners: dict[str, tuple[tuple[Any, ...], dict[str, Any]]] = {}
    for index, record in enumerate(result.records):
        date = str(record.get("full_date") or "").strip()
        if not date:
            continue  # defensive: parsers already filter such rows
        if record.get("source") == "daylio":
            key = (
                int(record.get("_datetime_ms") or 0),
                int(record.get("_daylio_id") or 0),
            )
        else:
            key = (str(record.get("time") or ""), index)
        if date not in winners or key >= winners[date][0]:
            winners[date] = (key, record)
    return [winner[1] for winner in winners.values()]


# --------------------------------------------------------------------------- #
# Single-record normalization
# --------------------------------------------------------------------------- #
def _normalize_record(
    record: dict[str, Any], cfg: AppConfig, result: ImportResult
) -> NormalizedEntry:
    """Build a :class:`NormalizedEntry` from one raw (already deduped) record."""
    full_date = str(record.get("full_date") or "").strip()
    source = str(record.get("source") or "csv")

    mood_level, mood_label = _normalize_mood(record, cfg, result, full_date)
    activities = _clean_activities(record, cfg)
    scala_salute, activities = _extract_scala_salute(record, cfg, activities)
    scales = _normalize_scales(record, cfg, result, full_date)

    derived = _derive_date_weekday(full_date)
    return NormalizedEntry(
        full_date=full_date,
        mood_level=mood_level,
        mood_label=mood_label,
        activities=activities,
        scales=scales,
        scala_salute=scala_salute,
        note_title=str(record.get("note_title") or ""),
        note=str(record.get("note") or ""),
        source=source,
        raw=dict(record),
        date=derived[0],
        weekday=derived[1],
        time=str(record.get("time") or ""),
    )


def _normalize_mood(
    record: dict[str, Any], cfg: AppConfig, result: ImportResult, full_date: str
) -> tuple[int, str]:
    """Resolve mood rank 1..5 and configured label (D1)."""
    mood_values = (cfg.mood or {}).get("values") or {}
    if record.get("source") == "daylio":
        level = int(record.get("mood") or 0)
        custom_name = str(record.get("mood_custom_name") or "").strip()
        label = _mood_label_for_level(mood_values, level, custom_name)
        if label is None:
            result.warn(f"{full_date}: unknown daylio mood id {level} (config D1)")
            return 0, ""
        return level, label

    raw_label = str(record.get("mood") or "").strip()
    level = int(mood_values.get(raw_label, 0))
    if level == 0 and raw_label:
        result.warn(f"{full_date}: unknown mood label {raw_label!r} (config D1)")
    return level, raw_label


def _mood_label_for_level(
    mood_values: dict[str, Any], level: int, custom_name: str = ""
) -> str | None:
    """Return the config mood label for a rank, honouring a custom name.

    A non-empty ``custom_name`` is used only when the config maps it to the
    same rank (premium users, per-user overrides); otherwise the configured
    label for the rank wins (the config is the authority, D1).
    """
    labels_by_rank = {int(rank): str(label) for label, rank in mood_values.items()}
    if custom_name and int(mood_values.get(custom_name, 0)) == level:
        return custom_name
    return labels_by_rank.get(int(level))


def _clean_activities(record: dict[str, Any], cfg: AppConfig) -> list[str]:
    """Convert the raw activities (string or list) into a clean label list."""
    raw = record.get("activities") or []
    if isinstance(raw, str):
        separator = _activities_separator(cfg)
        items = raw.split(separator)
    else:
        items = raw
    return [str(item).strip() for item in items if str(item).strip()]


def _activities_separator(cfg: AppConfig) -> str:
    """Read the configured activities separator (D8.d), fallback ``" | "``."""
    columns = cfg.columns or {}
    options = columns.get("options") if isinstance(columns.get("options"), dict) else {}
    act_options = options.get("activities") if isinstance(options.get("activities"), dict) else {}
    return str(act_options.get("separator") or _DEFAULT_ACTIVITIES_SEPARATOR)


def _extract_scala_salute(
    record: dict[str, Any], cfg: AppConfig, activities: list[str]
) -> tuple[dict[str, Any] | None, list[str]]:
    """Extract the "Scala salute" level (D2) from activities/tags.

    Returns ``(scala_salute, remaining_activities)``. For CSV the level is
    matched by label among the 8 configured levels; for .daylio it comes from
    the tags whose group equals the ``tag-group:N`` configured for the scale.
    """
    salute_cfg = (cfg.scales or {}).get("scala_salute")
    if not isinstance(salute_cfg, dict):
        return None, activities
    values = salute_cfg.get("values") or []
    ranks = {str(value).strip().casefold(): rank for rank, value in enumerate(values, 1)}
    if not ranks:
        return None, activities

    if record.get("source") == "daylio":
        group_id = parse_tag_group(str(salute_cfg.get("source") or ""))
        salute_label: str | None = None
        remaining: list[str] = []
        for tag in record.get("tags") or []:
            name = str(tag.get("name") or "").strip()
            group = tag.get("group")
            if group_id is not None and name and int(group) == group_id:
                if name.casefold() in ranks:
                    salute_label = name
                # A group-5 tag is always a Scala salute level: never keep it
                # in the activities list even if the config does not match it.
            elif name:
                remaining.append(name)
        if salute_label is None:
            return None, remaining
        return {"label": salute_label, "rank": ranks[salute_label.casefold()]}, remaining

    salute_label = None
    remaining = []
    for activity in activities:
        if activity.casefold() in ranks:
            salute_label = activity
        else:
            remaining.append(activity)
    if salute_label is None:
        return None, remaining
    return {"label": salute_label, "rank": ranks[salute_label.casefold()]}, remaining


def _normalize_scales(
    record: dict[str, Any], cfg: AppConfig, result: ImportResult, full_date: str
) -> list[dict[str, Any]]:
    """Normalize the generic scales into ``{scale_name,label,rank,levels_count}``."""
    if record.get("source") == "daylio":
        return _scales_from_daylio(record, cfg, result, full_date)
    return _scales_from_csv(record, cfg, result, full_date)


def _scale_lookup(cfg: AppConfig) -> dict[str, tuple[str, list[Any]]]:
    """Map display label -> (config scale key, ordered values).

    The display label is the canonical one used in the CSV ``scales`` column
    and in the .daylio ``scales`` objects (e.g. "Stress").
    """
    lookup: dict[str, tuple[str, list[Any]]] = {}
    for key, definition in (cfg.scales or {}).items():
        if not isinstance(definition, dict):
            continue
        display = str(definition.get("label") or scale_label(key))
        values = definition.get("values") or []
        lookup[display.strip().casefold()] = (str(key), list(values))
    return lookup


def _finalize_scale(
    cfg: AppConfig,
    display_name: str,
    level_label: str,
    fallback_rank: int | None,
    fallback_n: int | None,
    full_date: str,
    result: ImportResult,
    *,
    scale_key: str | None = None,
    scale_display: str | None = None,
) -> dict[str, Any] | None:
    """Fold one parsed/resolved scale into the canonical dict (config wins).

    ``scale_key``/``scale_display`` are the pre-resolved identity when the
    caller already knows them (daylio path); otherwise the display name is
    used to look the scale up in the config (csv path).
    """
    lookup = _scale_lookup(cfg)
    if scale_key is None:
        matched = lookup.get(display_name.strip().casefold())
        if matched is None:
            result.warn(f"{full_date}: scale {display_name!r} not configured; skipped")
            return None
        scale_key, values = matched
        scale_display = scale_label(scale_key)
    else:
        values = list(((cfg.scales or {}).get(scale_key) or {}).get("values") or [])

    ranks = {str(value).strip().casefold(): rank for rank, value in enumerate(values, 1)}
    rank = ranks.get(level_label.strip().casefold())
    if rank is None:
        result.warn(
            f"{full_date}: unknown level {level_label!r} for scale "
            f"{scale_display or display_name!r} (config)"
        )
        rank = fallback_rank
    if fallback_n is not None and values and fallback_n != len(values):
        result.warn(
            f"{full_date}: scale {scale_display or display_name!r} levels count "
            f"{fallback_n} != configured {len(values)}"
        )
    return {
        "scale_name": scale_display or display_name,
        "label": level_label.strip(),
        "rank": int(rank) if rank is not None else 0,
        "levels_count": len(values),
        "scale_key": scale_key,
    }


def _scales_from_csv(
    record: dict[str, Any], cfg: AppConfig, result: ImportResult, full_date: str
) -> list[dict[str, Any]]:
    """Parse the CSV ``scales`` column ``"Name: Level k/N"`` (D8.c)."""
    raw = record.get("scales")
    if raw is None or not str(raw).strip():
        return []
    normalized = _WS_RE.sub(" ", str(raw).replace("\xa0", " ")).strip()

    name_part, separator, rest = normalized.partition(":")
    if not separator:
        result.warn(f"{full_date}: cannot parse scales value {normalized!r} (missing ':')")
        return []
    display_name = name_part.strip()
    parts = rest.strip().rsplit(" ", 1)
    if len(parts) != 2:
        result.warn(f"{full_date}: cannot parse scales value {normalized!r} (expected 'Level k/N')")
        return []
    level_label, k_over_n = parts
    k_over_n = k_over_n.strip()
    k = n = None
    if "/" in k_over_n:
        left, right = k_over_n.split("/", 1)
        if left.strip().isdigit() and right.strip().isdigit():
            k, n = int(left.strip()), int(right.strip())
    if k is None or n is None:
        result.warn(f"{full_date}: scales value {normalized!r} has invalid rank 'k/N'")
        return []

    scale = _finalize_scale(
        cfg, display_name, level_label.strip(), k, n, full_date, result
    )
    return [scale] if scale is not None else []


def _scales_from_daylio(
    record: dict[str, Any], cfg: AppConfig, result: ImportResult, full_date: str
) -> list[dict[str, Any]]:
    """Normalize the importer-resolved ``scaleValues`` (daylio path)."""
    scales: list[dict[str, Any]] = []
    for resolved in record.get("scales") or []:
        display_name = str(resolved.get("scale_name") or "")
        level_label = str(resolved.get("label") or "")
        fallback_rank = resolved.get("fallback_rank")
        fallback_n = resolved.get("fallback_n")
        scale = _finalize_scale(
            cfg,
            display_name,
            level_label,
            int(fallback_rank) if fallback_rank is not None else None,
            int(fallback_n) if fallback_n is not None else None,
            full_date,
            result,
        )
        if scale is not None:
            scales.append(scale)
    return scales


# --------------------------------------------------------------------------- #
# Derived columns (D8.a)
# --------------------------------------------------------------------------- #
def _derive_date_weekday(full_date: str) -> tuple[str, str]:
    """Derive ``date`` (same ISO value) and English ``weekday`` from full_date.

    The full ``date`` string equals ``full_date``; ``weekday`` is the ISO
    weekday name derived identically for CSV and .daylio so the two pipelines
    always coincide. Localization is deferred to the API layer (Phase 4).
    """
    if not full_date:
        return "", ""
    try:
        parsed = datetime.strptime(full_date, "%Y-%m-%d")
    except ValueError:
        return full_date, ""
    return full_date, parsed.strftime("%A")
