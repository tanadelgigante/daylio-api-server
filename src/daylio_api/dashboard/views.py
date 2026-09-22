"""Dashboard views placeholder.

=============================== MAINTENANCE NOTES ===============================
Module   : src/daylio_api/dashboard/views.py
Scope    : Fase 0 placeholder (Task 0.2). Registers the ``/dashboard`` route
           serving the minimal static placeholder page. The real SPA plus the
           ``/api/analytics/*`` Influx-only endpoints are implemented in
           Phase 6 (WORKPLAN §5.7).
================================================================================
"""

from __future__ import annotations

from flask import Blueprint

bp = Blueprint(
    "dashboard",
    __name__,
    url_prefix="/dashboard",
    static_folder="static",
    static_url_path="/dashboard/static",
)


@bp.get("/")
def index():
    """Serve the minimal placeholder dashboard page (static/index.html)."""
    return bp.send_static_file("index.html")
