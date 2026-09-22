"""API blueprint placeholder.

=============================== MAINTENANCE NOTES ===============================
Module   : src/daylio_api/api/routes.py
Scope    : Fase 0 placeholder (Task 0.2). Creates the (empty) ``/api``
           blueprint that the app factory will register in Phase 4. Only a
           version endpoint is exposed now so ``create_app`` can be smoke-tested.
================================================================================
"""

from __future__ import annotations

from flask import Blueprint, jsonify

from daylio_api import __version__

bp = Blueprint("api", __name__, url_prefix="/api")


@bp.get("/version")
def api_version() -> object:
    """Return the running package version (used by tests/health checks)."""
    return jsonify({"name": "daylio-api-server", "version": __version__})
