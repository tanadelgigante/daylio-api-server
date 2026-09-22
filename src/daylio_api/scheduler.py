"""Scheduler placeholder.

=============================== MAINTENANCE NOTES ===============================
Module   : src/daylio_api/scheduler.py
Scope    : Fase 0 placeholder (Task 0.2). Configures (but does NOT start) an
           APScheduler ``BackgroundScheduler`` from the validated YAML config.
           Per-user periodic import jobs (interval + start hour, D6) are
           registered and started in Phase 5 (WORKPLAN §5.2/§7).
================================================================================
"""

from __future__ import annotations

from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler

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
