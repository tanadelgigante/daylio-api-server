"""Flask application factory.

=============================== MAINTENANCE NOTES ===============================
Module   : src/daylio_api/app.py
Scope    : Fase 0 (Task 0.2/0.4) + Fase 1 (Tasks 1.1/1.2/1.3) — creates the Flask
           app: loads and validates the YAML configuration (config.load),
           initialises the level-aware logger, boots the SQLite schema with the
           versioned idempotent migrations (db.migrations) and reconciles the
           columns/scales/mood configuration with the YAML (config.sync_schema),
           then registers the ``/api`` blueprint (version + config reads).
           Records/import/auth/dashboard blueprints arrive in Phases 4-6.

Lifecycle: the SqliteStore is stored in ``app.extensions["daylio_store"]`` and
           lives for the whole app lifetime (call ``.close()`` on shutdown, as
           the tests do). The module-level ``app`` WSGI object boots the schema
           at import time — the normal behaviour under Waitress
           (``waitress-serve --port=5000 daylio_api.app:app``).

Fail-fast : invalid configuration raises ``ConfigError`` from ``create_app``,
           so a misconfigured service never starts (risk R15).
==================================================================================
"""

from __future__ import annotations

import os
from typing import Any

from flask import Flask, jsonify

from daylio_api import __version__
from daylio_api.api.routes import bp as api_bp
from daylio_api.config import AppConfig, sync_schema
from daylio_api.config import load as load_config
from daylio_api.db.migrations import migrate
from daylio_api.db.sqlite_store import SqliteStore
from daylio_api.logger import configure as configure_logger
from daylio_api.logger import get_logger

log = get_logger("app")


def create_app(
    config_file: str | os.PathLike[str] | None = None,
    users_file: str | os.PathLike[str] | None = None,
) -> Flask:
    """Create and configure a Flask application.

    Args:
        config_file: optional explicit base configuration file (default: the
            ``CONFIG_FILE`` env var or ``config/config.example.yml``).
        users_file: optional explicit per-user overrides file (default: the
            ``USERS_CONFIG_FILE`` env var, or the ``users`` section of the base
            file).

    Returns:
        The configured Flask application (the WSGI ``app`` object).
    """
    app = Flask(__name__)
    app.config["DAYLIO_VERSION"] = __version__

    cfg: AppConfig = load_config(config_file, users_file)
    configure_logger(cfg.effective_log_level)
    app.config["DAYLIO"] = cfg
    users_file_resolved = users_file or os.environ.get("USERS_CONFIG_FILE")
    app.config["DAYLIO_USER_CONFIG_FILE"] = (
        str(users_file_resolved) if users_file_resolved else None
    )

    # --- SQLite bootstrap (Fase 1, Tasks 1.1/1.2/1.4) ---------------------- #
    # Open the store, apply versioned idempotent migrations, then reconcile
    # the YAML configuration with the config tables (seed on first boot).
    db_path = cfg.storage.get("sqlite_path") or "data.db"
    store = SqliteStore(db_path)
    conn = store.connect()
    schema_version = migrate(conn)
    sync_stats = sync_schema(conn, cfg)
    app.extensions["daylio_store"] = store
    app.config["DAYLIO_SCHEMA_VERSION"] = schema_version
    app.config["DAYLIO_SYNC_STATS"] = sync_stats
    log.info(
        "SQLite schema ready (version=%s, db=%s, sync=%s)",
        schema_version,
        db_path,
        sync_stats,
    )

    # REST API blueprint (Fase 1: version + configuration reads; more in Phases 4-5).
    app.register_blueprint(api_bp)

    # Minimal liveness endpoint for smoke tests / orchestration.
    @app.get("/healthz")
    def healthz() -> Any:
        payload = {"status": "ok", "version": __version__}
        return jsonify(payload), 200

    log.info("Flask app created (version=%s)", __version__)
    return app


#: WSGI entry point used by Waitress (and docker CMD). Boots the schema and
#: seeds the config tables at import time (see MAINTENANCE NOTES above).
app = create_app()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
