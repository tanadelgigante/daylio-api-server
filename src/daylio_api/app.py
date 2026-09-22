"""Flask application factory.

=============================== MAINTENANCE NOTES ===============================
Module   : src/daylio_api/app.py
Scope    : Fase 0 (Task 0.2/0.4) — minimal Flask app factory. Loads the YAML
           configuration (config.load), initialises the level-aware logger,
           and stores the configuration on ``app.config["DAYLIO"]`` for later
           phases. Blueprints are registered in later phases (api, dashboard);
           bundle the WSGI ``app`` variable expected by Waitress:
               waitress-serve --port=5000 daylio_api.app:app
================================================================================
"""

from __future__ import annotations

import os
from typing import Any

from flask import Flask, jsonify

from daylio_api import __version__
from daylio_api.config import AppConfig
from daylio_api.config import load as load_config
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

    # Minimal liveness endpoint for smoke tests / orchestration. Real API
    # blueprints are registered in later phases (WORKPLAN §7).
    @app.get("/healthz")
    def healthz() -> Any:
        payload = {"status": "ok", "version": __version__}
        return jsonify(payload), 200

    log.info("Flask app created (version=%s)", __version__)
    return app


#: WSGI entry point used by Waitress (and docker CMD).
app = create_app()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
