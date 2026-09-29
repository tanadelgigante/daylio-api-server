"""Flask application factory.

=============================== MAINTENANCE NOTES ===============================
Module   : src/daylio_api/app.py
Scope    : Fase 0 (Task 0.2/0.4) + Fase 1 (Tasks 1.1/1.2/1.3) — creates the Flask
           app: loads and validates the YAML configuration (config.load),
           initialises the level-aware logger, boots the SQLite schema with the
           versioned idempotent migrations (db.migrations) and reconciles the
           columns/scales/mood configuration with the YAML (config.sync_schema),
           then registers the ``/api`` blueprint (version + config reads).
           Fase 4/5 add the records/import/auth blueprints, Fase 6 the
           ``dashboard`` blueprint (``/dashboard`` SPA + ``/api/analytics/*``,
           Influx-only, R4/D4).

Lifecycle: the SqliteStore is stored in ``app.extensions["daylio_store"]`` and
           lives for the whole app lifetime. Every ``create_app()`` registers an
           ``atexit`` hook that closes BOTH stores (R3: the SQLite connections
           were previously never released) — see :func:`_make_shutdown`.

WSGI      : the module-level ``app`` is a :class:`_LazyWsgiApp` proxy that calls
           ``create_app()`` on the FIRST request instead of at import time
           (R2: importing the module used to create ``data/daylio.db`` as a side
           effect, ignoring the ``DB_PATH`` override a test had just set). It is
           a plain WSGI callable, so ``waitress-serve daylio_api.app:app`` (the
           Docker CMD) is unaffected; attribute access is proxied to the real
           Flask app, so ``app.test_client()``/``app.config`` keep working.

Fail-fast : invalid configuration raises ``ConfigError`` from ``create_app``,
           so a misconfigured service never starts (risk R15).
================================================================================
"""

from __future__ import annotations

import atexit
import os
import secrets
import threading
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from flask import Flask, jsonify, request

from daylio_api import __version__
from daylio_api.api.auth import admin_bp, auth_bp, init_admin
from daylio_api.api.routes import bp as api_bp
from daylio_api.api.routes import legacy_bp
from daylio_api.config import AppConfig, sync_schema
from daylio_api.config import load as load_config
from daylio_api.dashboard.views import bp as dashboard_bp
from daylio_api.db.influx_store import InfluxStore, resolve_influx_config
from daylio_api.db.migrations import migrate
from daylio_api.db.sqlite_store import SqliteStore
from daylio_api.logger import configure as configure_logger
from daylio_api.logger import get_logger
from daylio_api.scheduler import create_scheduler

log = get_logger("app")


def _make_shutdown(store: SqliteStore, influx: InfluxStore | None) -> Callable[[], None]:
    """Return the idempotent shutdown hook closing the SQLite+Influx stores.

    R3: ``SqliteStore`` keeps one connection per thread and no caller closed
    them, so the interpreter exit leaked every connection. Failures are logged
    and never raised: a shutdown hook must not break the exit path. The hook is
    safe to call twice and never while a request is in flight (atexit only runs
    after the WSGI server has stopped).
    """
    closed = False

    def _shutdown() -> None:
        nonlocal closed
        if closed:
            return
        closed = True
        if influx is not None:
            try:
                influx.close()
            except Exception as exc:  # noqa: BLE001 — shutdown must not raise
                log.warning("Influx client close failed: %s", exc)
        try:
            store.close()
        except Exception as exc:  # noqa: BLE001 — shutdown must not raise
            log.warning("SQLite store close failed: %s", exc)
        # DEBUG on purpose: at interpreter exit the stdio streams may already be
        # closed (a StreamHandler write would then emit a logging error), so the
        # hook only reports itself when LOG_LEVEL=DEBUG.
        log.debug("Stores closed on shutdown (R3)")

    return _shutdown



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

    # --- Auth (Fase 5, D6): signed sessions + API keys --------------------- #
    # SECRET_KEY drives the itsdangerous session signature. A missing
    # auth.session_secret (or an unresolved ``env:SESSION_SECRET`` placeholder)
    # falls back to a random per-boot secret with a loud warning: sessions
    # will not survive a restart, but the service stays bootable.
    auth_cfg = cfg.auth if isinstance(cfg.auth, dict) else {}
    session_secret = str(auth_cfg.get("session_secret") or "").strip()
    if not session_secret or session_secret.startswith("env:"):
        log.warning(
            "auth.session_secret is not configured; using a random per-boot "
            "secret (sessions will not survive a restart)"
        )
        session_secret = secrets.token_hex(32)
    app.config["SECRET_KEY"] = session_secret
    session_ttl = auth_cfg.get("session_ttl")
    session_ttl = int(session_ttl) if isinstance(session_ttl, int) else 86400
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(seconds=session_ttl)
    app.config["SESSION_COOKIE_NAME"] = str(
        auth_cfg.get("session_cookie_name") or "daylio_session"
    )
    api_keys_cfg = auth_cfg.get("api_keys")
    app.config["DAYLIO_API_KEYS_ENABLED"] = bool(
        api_keys_cfg.get("enabled", True)
        if isinstance(api_keys_cfg, dict)
        else True
    )

    # First-boot admin bootstrap (D6): ADMIN_USERNAME/ADMIN_PASSWORD env.
    try:
        init_admin(store, cfg)
    except Exception:  # noqa: BLE001 — admin bootstrap must never block boot
        log.exception("admin bootstrap failed; continuing without an admin user")

    # --- InfluxDB store (lazy client; R8: unavailable is not fatal) --------- #
    influx: InfluxStore | None = None
    influx_cfg = resolve_influx_config(cfg)
    if influx_cfg is not None:
        influx = InfluxStore(influx_cfg)
    app.extensions["daylio_influx"] = influx

    # R3: release both stores (SQLite connections + Influx client) at exit.
    app.extensions["daylio_shutdown"] = _make_shutdown(store, influx)
    atexit.register(app.extensions["daylio_shutdown"])

    # REST API blueprint (Fase 1: version + configuration reads; more in Phases 4-5).
    app.register_blueprint(api_bp)
    # Root-level legacy endpoints (old server.py wire contract, read from
    # day_entries/users). Registered behind the API blueprint on purpose.
    app.register_blueprint(legacy_bp)
    # Fase 5: authentication + admin-only user management (D6).
    app.register_blueprint(auth_bp)
    app.register_blueprint(admin_bp)
    # Fase 6: dashboard SPA + /api/analytics/* (Influx-only, R4/D4).
    app.register_blueprint(dashboard_bp)

    # --- Per-user folder import scheduler (Fase 5, R7/D6) ------------------- #
    # Registered ONLY when scheduler.enabled is true (opt-in). A scheduler
    # failure is logged and never prevents the app from serving.
    scheduler_enabled = bool((cfg.scheduler or {}).get("enabled", False))
    if scheduler_enabled:
        try:
            scheduler = create_scheduler(store, influx, cfg)
            scheduler.start()
            app.extensions["daylio_scheduler"] = scheduler
            log.info("APScheduler started (%d jobs)", len(scheduler.get_jobs()))
        except Exception:  # noqa: BLE001 — scheduler must never block boot
            log.exception("APScheduler failed to start; continuing without it")
            app.extensions["daylio_scheduler"] = None
    else:
        app.extensions["daylio_scheduler"] = None
        log.info("scheduler.enabled=false: periodic imports are OFF (Fase 5)")

    # Minimal liveness endpoint for smoke tests / orchestration.
    @app.get("/healthz")
    def healthz() -> Any:
        payload = {"status": "ok", "version": __version__}
        return jsonify(payload), 200

    # Uniform 404 for UNMATCHED routes (no blueprint context -> app-level).
    # Blueprint-raised 404s keep the blueprint handler in api/routes.py (the
    # wire contract is the same); this fallback guarantees that even unknown
    # /api/* paths answer JSON. Non-API requests without JSON in Accept (the
    # dashboard SPA shell and its static assets) fall back to a minimal HTML
    # page.
    @app.errorhandler(404)
    def _app_not_found(exc: Any) -> Any:
        message = str(getattr(exc, "description", None) or "not found")
        wants_json = request.path.startswith("/api/") or bool(
            request.accept_mimetypes and request.accept_mimetypes.accept_json
        )
        if wants_json:
            return jsonify({"error": message, "type": "not_found"}), 404
        body = (
            "<!doctype html><html><head><title>404 Not Found</title></head>"
            f"<body><h1>Not Found</h1><p>{message}</p></body></html>"
        )
        return body, 404, {"Content-Type": "text/html"}

    # Fase 5 auth errors use the uniform JSON shape. They are registered
    # app-wide so decorators in any blueprint (api, legacy, auth, admin)
    # produce the same 401/403 wire contract.
    @app.errorhandler(401)
    def _unauthorized(exc: Any) -> Any:
        message = str(getattr(exc, "description", None) or "authentication required")
        return jsonify({"error": message, "type": "unauthorized"}), 401

    @app.errorhandler(403)
    def _forbidden(exc: Any) -> Any:
        message = str(getattr(exc, "description", None) or "forbidden")
        return jsonify({"error": message, "type": "forbidden"}), 403

    log.info("Flask app created (version=%s)", __version__)
    return app


#: WSGI entry point used by Waitress (and docker CMD). It is a LAZY proxy: the
#: Flask app (and therefore the SQLite schema + config seeding) is built on the
#: first request instead of at import time, so importing this module has no side
#: effect on the filesystem (R2). See the module MAINTENANCE NOTES above.
class _LazyWsgiApp:
    """WSGI callable that builds the Flask app on first use (R2).

    Thread-safe (double-checked lock: waitress serves concurrent requests) and
    attribute-transparent: ``proxy.test_client()``, ``proxy.config`` and friends
    are forwarded to the real :class:`flask.Flask` app once it exists.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._args = args
        self._kwargs = kwargs
        self._app: Flask | None = None
        self._lock = threading.Lock()

    def resolve(self) -> Flask:
        """Return the Flask app, creating it on the first call."""
        app = self._app
        if app is None:
            with self._lock:
                if self._app is None:
                    self._app = create_app(*self._args, **self._kwargs)
                    log.info("Lazy WSGI app materialised on first use (R2)")
                app = self._app
        return app

    def __call__(self, environ: Any, start_response: Any) -> Any:
        """WSGI entry point (called by waitress with the usual 2 arguments)."""
        return self.resolve()(environ, start_response)

    def __getattr__(self, name: str) -> Any:
        """Proxy unknown attributes to the real Flask app (tooling sugar)."""
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self.resolve(), name)


#: WSGI app for Waitress/Docker CMD (``daylio_api.app:app``).
app = _LazyWsgiApp()


if __name__ == "__main__":
    # Development server: the development app is built eagerly on purpose.
    create_app().run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
