"""Import/resync coordinator: pipeline orchestration + audit (Task 3.3).

=============================== MAINTENANCE NOTES ===============================
Module   : src/daylio_api/import_flow.py
Scope    : Fase 3 (R3/R5, WORKPLAN §7 Task 3.3) — the single public entry point
           that turns a parsed/normalized import into persisted records:

             import_data(conn, influx, config, user, source, filename, records)
               -> ImportSummary   (SQLite upsert D3 + Influx delete-and-write +
                                    import_jobs audit row, one transaction)

             resync_all(conn, influx, user=None, ...)
               -> stats           (full Influx rewrite from the SQLite truth, D4)

           Both are intentionally source-agnostic: ``records`` accepts the
           ``ImportResult`` produced by ``csv_importer``/``daylio_importer``
           OR an iterable of ``NormalizedEntry`` (e.g. a folder scan reusing
           a previously normalized result). ``import_data`` normalizes the
           ``ImportResult`` when given one.

Tolerance (R8):  a missing/unreachable InfluxDB never fails the SQLite import.
           ``influx_available`` is checked once before the write loop; per-day
           Influx failures are counted in ``influx_errors`` and logged. SQLite
           remains the source of truth (D4).

Atomicity     :  all SQLite writes (upserts + audit row) happen in the caller
           transaction. ``commit=True`` (default) commits once at the end; pass
           ``commit=False`` to defer the commit to an outer transaction.
================================================================================
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from daylio_api import config as config_module
from daylio_api.audit import record_job
from daylio_api.config import AppConfig
from daylio_api.db.influx_store import (
    InfluxStore,
    build_scale_key_map,
    influx_available,
)
from daylio_api.db.sqlite_store import get_or_create_user, upsert_entry
from daylio_api.importers.base import ImportResult, NormalizedEntry
from daylio_api.importers.normalizer import normalize
from daylio_api.logger import get_logger

log = get_logger("import_flow")


@dataclass
class ImportSummary:
    """Outcome of one persisted import (mirrors the audit row, §5.3).

    ``imported`` counts days INSERTED as new rows, ``updated`` days that were
    overwritten by an existing row (upsert), ``skipped`` duplicate-loser rows
    from normalization, ``errors`` parse+persistence failures. ``influx_errors``
    holds per-day Influx write failures (R8 tolerance); ``job_id`` is the id of
    the written ``import_jobs`` audit row.
    """

    user: str = ""
    source: str = "upload"      # upload | folder | daily
    filename: str = ""
    imported: int = 0
    updated: int = 0
    skipped: int = 0
    errors: int = 0
    influx_errors: int = 0
    warnings: int = 0
    total_rows: int = 0
    status: str = "completed"   # running | completed | failed
    job_id: int | None = None
    detail: list[dict[str, Any]] = field(default_factory=list)
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None


def _entries_from(
    records: ImportResult | Iterable[NormalizedEntry | dict[str, Any]],
    cfg: AppConfig | None,
    summary: ImportSummary,
) -> list[NormalizedEntry]:
    """Turn the input into a (deduplicated) list of NormalizedEntry.

    An ``ImportResult`` is normalized here (duplicate-loser rows land in
    ``summary.skipped``, parse errors/warnings are carried over); a raw
    iterable of dicts is accepted too and normalized on the fly for callers
    that built canonical records directly.
    """
    cfg = cfg if isinstance(cfg, AppConfig) else config_module.load()
    if isinstance(records, ImportResult):
        summary.total_rows = int(records.total_rows)
        # Normalize BEFORE copying counters: it computes imported/skipped and
        # appends normalization warnings to records.detail (D3 dedup).
        entries = normalize(records, cfg)
        summary.skipped = int(records.skipped)
        summary.errors = int(records.errors)
        summary.warnings = int(records.warnings)
        summary.detail = list(records.detail)
        return entries

    entries: list[NormalizedEntry] = []
    for item in records:
        if isinstance(item, NormalizedEntry):
            entries.append(item)
        elif isinstance(item, dict):
            # Bust a canonical dict -> NormalizedEntry so a caller can stream
            # raw as-imported rows without building the dataclass by hand.
            entries.append(
                NormalizedEntry(
                    full_date=str(item.get("full_date") or ""),
                    mood_level=int(item.get("mood") or 0),
                    mood_label=str(item.get("mood_label") or ""),
                    activities=list(item.get("activities") or []),
                    scales=list(item.get("scales") or []),
                    scala_salute=item.get("scala_salute"),
                    note_title=str(item.get("note_title") or ""),
                    note=str(item.get("note") or ""),
                    source=str(item.get("source") or "csv"),
                    time=str(item.get("time") or ""),
                )
            )
        else:
            raise TypeError(f"unsupported record item: {type(item).__name__}")
    summary.total_rows = len(entries)
    return sorted(entries, key=lambda entry: entry.full_date)


def import_data(
    conn: Any,
    influx: InfluxStore | None,
    config: AppConfig | dict[str, Any] | None,
    user: str,
    source: str,
    filename: str,
    records: ImportResult | Iterable[NormalizedEntry | dict[str, Any]],
    *,
    commit: bool = True,
) -> ImportSummary:
    """Persist one import run into SQLite (and Influx when available) + audit.

    Args:
        conn: SQLite connection (transaction owned by this function, committed
            at the end when ``commit`` is True).
        influx: an :class:`InfluxStore` (or None) — availability is checked
            internally; a missing server degrades gracefully (R8).
        config: validated ``AppConfig`` (used for normalization + scale keys).
        user: record owner.
        source: audit provenance (``upload``/``folder``/``daily``).
        filename: the imported file path/name (audit).
        records: an ``ImportResult`` from the parser, an iterable of
            ``NormalizedEntry``, or an iterable of canonical dicts.
        commit: when True (default) commits the whole transaction once at the
            end; other callers that share the connection should pass
            ``commit=False`` and commit together with their own writes.

    Returns:
        An :class:`ImportSummary` with the audit counters; a failed run still
        writes a ``status="failed"`` audit row (every import leaves a trail).
    """
    started = datetime.now(UTC)
    summary = ImportSummary(
        user=user, source=source, filename=filename, started_at=started
    )

    try:
        cfg = config if isinstance(config, AppConfig) else config_module.load()
        entries = _entries_from(records, cfg, summary)
        get_or_create_user(conn, user)

        # Register the scale key map so Influx points use config keys (D8.c).
        if influx is not None:
            influx.set_scale_keys(build_scale_key_map(cfg))

        influx_on = influx_available(influx) if influx is not None else False
        if influx is not None:
            if influx_on:
                log.info(
                    "InfluxDB available: syncing %d days to %s",
                    len(entries),
                    influx.config.bucket,
                )
            else:
                summary.warnings += 1
                summary.detail.append(
                    {
                        "level": "warning",
                        "message": "InfluxDB unavailable; SQLite import continues (R8)",
                    }
                )
                log.warning("InfluxDB unavailable: SQLite import continues (R8)")

        for entry in entries:
            try:
                outcome = upsert_entry(conn, user, entry)
                if outcome == "imported":
                    summary.imported += 1
                else:
                    summary.updated += 1
                if influx_on:
                    try:
                        influx.write_entry(user, entry, measurement=influx.config.measurement)
                    except Exception as exc:  # noqa: BLE001 — per-day tolerance, R8
                        summary.influx_errors += 1
                        summary.detail.append(
                            {
                                "level": "error",
                                "kind": "influx",
                                "day": entry.full_date,
                                "message": str(exc),
                            }
                        )
                        log.error("Influx write failed for %s %s: %s", user, entry.full_date, exc)
            except Exception as exc:  # noqa: BLE001 — one bad day never kills the run
                summary.errors += 1
                summary.detail.append(
                    {
                        "level": "error",
                        "kind": "sqlite",
                        "day": entry.full_date,
                        "message": str(exc),
                    }
                )
                log.error("SQLite upsert failed for %s %s: %s", user, entry.full_date, exc)

        summary.status = "completed"
    except Exception as exc:  # noqa: BLE001 — catastrophic error -> failed audit row
        summary.status = "failed"
        summary.errors += 1
        summary.detail.append({"level": "error", "kind": "fatal", "message": str(exc)})
        log.exception("import_data failed for %s %s", user, filename)

    summary.finished_at = datetime.now(UTC)

    try:
        summary.job_id = record_job(
            conn,
            user=user,
            source=source,
            filename=filename,
            imported=summary.imported,
            updated=summary.updated,
            skipped=summary.skipped,
            errors=summary.errors,
            detail=summary.detail,
            started_at=summary.started_at,
            finished_at=summary.finished_at,
            status=summary.status,
        )
    except Exception as exc:  # noqa: BLE001 — audit must never mask the import
        log.error("audit record_job failed: %s", exc)
        summary.detail.append({"level": "error", "kind": "audit", "message": str(exc)})

    if commit:
        conn.commit()
    log.info(
        "import %s user=%s imported=%d updated=%d skipped=%d errors=%d "
        "influx_errors=%d status=%s",
        filename,
        user,
        summary.imported,
        summary.updated,
        summary.skipped,
        summary.errors,
        summary.influx_errors,
        summary.status,
    )
    return summary


def resync_all(
    conn: Any,
    influx: InfluxStore | None,
    user: str | None = None,
    *,
    scale_keys: dict[str, str] | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    """Rewrite every daily record from SQLite into Influx (D4 resync).

    Args:
        conn: SQLite connection (source of truth).
        influx: an :class:`InfluxStore`; None returns a zeroed stats dict.
        user: optional username filter (all users when None).
        scale_keys: optional display->key map (defaults derived from config).
        commit: when True commits no transaction of its own (resync only
            writes to Influx); the parameter exists to keep the store
            transactions symmetric with ``import_data``.

    Returns:
        Stats dict ``{"users", "days", "influx_errors", "detail"}`` from
        :meth:`InfluxStore.resync_from_sqlite`. SQLite is never modified.
    """
    if influx is None:
        log.info("resync requested without Influx store; nothing to do")
        return {"users": 0, "days": 0, "influx_errors": 0, "detail": []}
    if scale_keys is None:
        cfg = config_module.load()
        scale_keys = build_scale_key_map(cfg)
    if not influx_available(influx):
        log.warning("InfluxDB unavailable during resync; aborting (R8)")
        return {"users": 0, "days": 0, "influx_errors": 1, "detail": [
            {"level": "error", "kind": "influx", "message": "InfluxDB unavailable"}
        ]}
    stats = influx.resync_from_sqlite(conn, user=user, scale_keys=scale_keys)
    if commit:
        conn.commit()
    return stats
