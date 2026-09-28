"""Per-user periodic import scheduler (Fase 5, R7/D6, APScheduler).

=============================== MAINTENANCE NOTES ===============================
Module   : src/daylio_api/scheduler.py
Scope    : Fase 0 placeholder (Task 0.2) extended in Fase 5 (WORKPLAN §5.2/§7
           Task 5.4) with the per-user periodic import jobs. The backend is
           APScheduler (NOT native threading, confirmed 2026-09-22).

Job model   : one ``interval`` job per user that has an ``import_dir``. The
           interval and the first-run hour come from:
             1. the per-user ``scheduler.per_user.<name>`` section of the base
                config;
             2. the ``users.<name>.scheduler`` override in the per-user file;
             3. the global ``scheduler.interval_seconds`` / ``scheduler.start_hour``
                defaults.
           The first run is scheduled at the next occurrence of ``start_hour``
           (config ``app.timezone``), then repeats every ``interval_seconds``.

Runtime     : the job body (``run_import_job``) opens its OWN ``SqliteStore``
           and ``InfluxStore`` inside the worker thread — never reusing the
           main-thread sqlite connection (``PRAGMA check_same_thread``). Every
           job invokes ``import_flow.import_folder(..., source="daily")`` so a
           scan is audited like any other import and moves files to ``done/``
           or ``failed/``.

Lifecycle   : ``create_scheduler`` builds and REGISTERS the jobs but does NOT
           start the scheduler; the app factory starts it only when
           ``scheduler.enabled`` is true (config.example.yml ships ``false``).
           ``build_scheduler`` remains the thin, side-effect-free constructor.
==================================================================================
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler

from daylio_api.config import AppConfig
from daylio_api.db.influx_store import InfluxStore, resolve_influx_config
from daylio_api.db.sqlite_store import SqliteStore, list_users
from daylio_api.import_flow import import_folder
from daylio_api.logger import get_logger

log = get_logger("scheduler")


def build_scheduler(config: dict[str, Any] | None = None) -> BackgroundScheduler:
    """Build a configured but NOT started APScheduler instance.

    Args:
        config: the ``scheduler`` section of the validated config (or the raw
            `AppConfig` object). Optional — the scheduler can also be created
            without arguments and configured later.

    Returns:
        A ``BackgroundScheduler`` with ``coalesce=True`` and job-defaults set.
        Call ``start()`` explicitly (Phase 5) when jobs are registered.
    """
    scheduler_section = config or {}
    if hasattr(config, "scheduler"):
        scheduler_section = config.scheduler  # allow passing AppConfig

    scheduler = BackgroundScheduler(
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 60}
    )
    scheduler_state = scheduler_section.get("backend", "apscheduler")
    log.info(
        "APScheduler configured (backend=%s, interval=%s, start_hour=%s) — not started",
        scheduler_state,
        scheduler_section.get("interval_seconds"),
        scheduler_section.get("start_hour"),
    )
    return scheduler


def _next_occurrence(hour: int, tz_name: str = "UTC") -> datetime:
    """Return the next ``datetime`` at ``hour`` in ``tz_name`` (strictly > now)."""
    try:
        tz = ZoneInfo(tz_name or "UTC")
    except Exception:  # noqa: BLE001 — a bad tz must not kill job registration
        log.warning("unknown timezone %r, falling back to UTC", tz_name)
        tz = ZoneInfo("UTC")
    now = datetime.now(tz)
    candidate = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


def create_scheduler(
    store: SqliteStore,
    influx: InfluxStore | None,
    config: AppConfig,
) -> BackgroundScheduler:
    """Build an APScheduler with one periodic import job per user (R7/D6).

    Jobs are only registered for users whose ``users.import_dir`` is set (the
    per-user import folder is stored on the ``users`` row by the admin API or
    seeded by the operator). The scheduler is returned NOT started — the app
    factory decides whether to call ``start()`` based on ``scheduler.enabled``.

    Args:
        store: the bootstrapped SqliteStore (used to read ``users.import_dir``);
            the jobs themselves open their own store inside the worker thread.
        influx: the app InfluxStore; passed as a hint of availability but each
            job builds its own store (see ``run_import_job``).
        config: validated ``AppConfig`` (scheduler + per-user sections).

    Returns:
        A ``BackgroundScheduler`` with the registered per-user jobs.
    """
    scheduler = build_scheduler(config)
    scheduler_section = config.scheduler if isinstance(config, AppConfig) else (config or {})
    global_interval = int(scheduler_section.get("interval_seconds") or 86400)
    global_hour = int(scheduler_section.get("start_hour") or 0)
    per_user_scheduler = (
        scheduler_section.get("per_user") if isinstance(scheduler_section, dict) else None
    ) or {}
    timezone_name = str((config.app or {}).get("timezone") or "UTC")

    conn = store.connect()
    registered = 0
    for row in list_users(conn):
        username = str(row["username"])
        import_dir = row.get("import_dir")
        if not import_dir:
            continue

        overrides: dict[str, Any] = dict(per_user_scheduler.get(username) or {})
        user_config = (config.users or {}).get(username)
        if isinstance(user_config, dict):
            user_scheduler = user_config.get("scheduler")
            if isinstance(user_scheduler, dict):
                overrides.update(user_scheduler)

        interval = int(overrides.get("interval_seconds", global_interval))
        start_hour = int(overrides.get("start_hour", global_hour))
        scheduler.add_job(
            run_import_job,
            trigger="interval",
            seconds=interval,
            start_date=_next_occurrence(start_hour, timezone_name),
            id=f"import:{username}",
            replace_existing=True,
            name=f"daily import for {username}",
            kwargs={
                "db_path": str(store.db_path),
                "username": username,
                "import_dir": str(import_dir),
                "config": config,
            },
        )
        registered += 1
        log.info(
            "scheduled import for user=%r folder=%s interval=%ss start_hour=%s",
            username,
            import_dir,
            interval,
            start_hour,
        )

    log.info("APScheduler jobs registered: %d (folder imports)", registered)
    return scheduler


def run_import_job(*, db_path: str, username: str, import_dir: str, config: AppConfig) -> None:
    """Run one scheduled folder import in the APScheduler worker thread.

    Opens a dedicated ``SqliteStore`` (the main store connection is bound to
    the caller thread) and an ``InfluxStore`` from the config, then delegates
    the full scan+audit+file-move to ``import_flow.import_folder`` with
    ``source="daily"``. Errors are logged and swallowed — a failed scan must
    not kill the scheduler.
    """
    store = SqliteStore(db_path)
    influx: InfluxStore | None = None
    try:
        influx_config = resolve_influx_config(config)
        if influx_config is not None:
            influx = InfluxStore(influx_config)
        conn = store.connect()
        import_folder(conn, influx, config, username, import_dir, source="daily")
    except Exception:  # noqa: BLE001 — a job failure must never crash APScheduler
        log.exception(
            "scheduled import failed user=%r folder=%s", username, import_dir
        )
    finally:
        store.close()
