"""Authentication: PBKDF2 hashing, signed sessions, API keys, admin decorators.

=============================== MAINTENANCE NOTES ===============================
Module   : src/daylio_api/api/auth.py
Scope    : Fase 5 (R6/D6, WORKPLAN §7 Task 5.1) — replaces the Fase 0
           placeholder. Provides:
             - PBKDF2 password hashing via Werkzeug (``hash_password`` /
               ``verify_password``);
             - signed session cookies (Flask + itsdangerous) for the Web UI,
               with the TTL enforced by ``PERMANENT_SESSION_LIFETIME``;
             - API keys for scripts/automation (R6): only the SHA-256 hash is
               stored, every key is shown in plaintext exactly once at creation;
             - identity resolution (``current_user``), the ``@login_required`` /
               ``@admin_required`` decorators and the two API blueprints
               ``/api/auth/*`` and ``/api/admin/*``;
             - ``init_admin``: first-boot admin bootstrap from the
               ``ADMIN_USERNAME``/``ADMIN_PASSWORD`` env vars (D6). Missing
               credentials only log a clear warning and NEVER raise — the
               module-level ``app = create_app()`` runs at import time.

Auth order : a request is authenticated by the signed session cookie first,
             then by an API key (``Authorization: Bearer <key>`` or
             ``X-API-Key``). Views decorated with ``@login_required`` /
             ``@admin_required`` abort with the uniform 401/403 JSON handled
             at app level (app.py).

API keys   : keys look like ``daylio_<64 hex>``; ``key_prefix`` is the first
             10 hex characters (display/revoke handle). ``api_keys`` rows are
             stored in ``db.migrations`` v3 and managed through the
             ``sqlite_store`` accessors. Key auth respects
             ``auth.api_keys.enabled`` (config.example.yml default true).

Threading  : request resolvers open a fresh connection through the shared
             ``SqliteStore`` so a request never shares a sqlite connection
             across threads. Scheduler jobs open their own store
             (``scheduler.run_import_job``), keeping ``check_same_thread``
             satisfied.
==================================================================================
"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable
from functools import wraps
from typing import Any, cast

from flask import Blueprint, abort, current_app, g, jsonify, request, session
from werkzeug.security import check_password_hash, generate_password_hash

from daylio_api.config import AppConfig
from daylio_api.db.sqlite_store import (
    SqliteStore,
    create_api_key,
    create_user,
    delete_user,
    list_api_keys,
    list_users_with_counts,
    revoke_api_key,
    user_by_username,
    user_for_api_key,
)
from daylio_api.logger import get_logger

log = get_logger("api.auth")

#: Flask blueprints registered by the app factory (app.py).
auth_bp = Blueprint("auth", __name__, url_prefix="/api/auth")
admin_bp = Blueprint("admin", __name__, url_prefix="/api/admin")

#: Prefix of every generated API key (display/revocation convenience).
_API_KEY_PREFIX = "daylio_"


# --------------------------------------------------------------------------- #
# Password hashing (D6): PBKDF2 via Werkzeug
# --------------------------------------------------------------------------- #
def hash_password(password: str) -> str:
    """Hash a plaintext password with Werkzeug's PBKDF2 (method=D6)."""
    return generate_password_hash(password, method="pbkdf2:sha256")


def verify_password(password: str, password_hash: str) -> bool:
    """Verify ``password`` against a Werkzeug PBKDF2 hash (never raises)."""
    if not password_hash:
        return False
    try:
        return check_password_hash(password_hash, password)
    except ValueError:
        return False


# --------------------------------------------------------------------------- #
# Identity resolution
# --------------------------------------------------------------------------- #
def _store() -> SqliteStore:
    """Return the bootstrapped SqliteStore (registered by the app factory)."""
    store = current_app.extensions.get("daylio_store")
    if store is None:
        abort(503, description="SQLite store not initialised")
    return store


def _key_from_request() -> str | None:
    """Return the API key from ``Authorization: Bearer`` or ``X-API-Key``."""
    header = request.headers.get("Authorization") or ""
    if header.startswith("Bearer "):
        candidate = header[len("Bearer ") :].strip()
        if candidate:
            return candidate
    candidate = (request.headers.get("X-API-Key") or "").strip()
    return candidate or None


def _user_from_session() -> dict[str, Any] | None:
    """Resolve the identity from the signed session cookie (Web UI)."""
    username = session.get("username")
    if not username:
        return None
    return user_by_username(_store().connect(), str(username))


def _user_from_api_key() -> dict[str, Any] | None:
    """Resolve the identity from an API key (scripts/R6).

    MAINTENANCE NOTES (FASE 7 bug fix):
        :func:`user_for_api_key` returns the join ``api_keys`` x ``users`` with
        the columns ``key_id``/``user_id`` and deliberately no ``id``, while the
        session lookup (:func:`user_by_username`) returns the users row WITH
        ``id``. Views such as ``/api/auth/api-keys`` read ``current_user()["id"]``,
        so the raw API-key dict raised ``KeyError`` -> HTTP 500 whenever those
        routes were authenticated with an API key. We normalise the API-key
        branch here to the same shape as the session branch by adding ``id``
        (aliased from ``user_id``), keeping ``current_user()`` a single,
        consumer-agnostic contract regardless of how the request authenticated.
    """
    key = _key_from_request()
    if not key:
        return None
    if not current_app.config.get("DAYLIO_API_KEYS_ENABLED", True):
        return None
    key_hash = hashlib.sha256(key.encode("utf-8")).hexdigest()
    user = user_for_api_key(_store().connect(), key_hash)
    if user is not None:
        # Alias the API-key user_id onto id so callers can use user["id"].
        user.setdefault("id", user.get("user_id"))
    return user


def resolve_user() -> dict[str, Any] | None:
    """Return the authenticated ``users`` row, or None (anonymous).

    Resolution order: signed session cookie first, then API key.
    """
    return _user_from_session() or _user_from_api_key()


def current_user() -> dict[str, Any] | None:
    """Return the authenticated user for the request (cached in ``g``)."""
    cached = getattr(g, "auth_user", None)
    if cached is not None:
        return cached
    user = resolve_user()
    if user is not None:
        g.auth_user = user
    return user


def public_user(user: dict[str, Any]) -> dict[str, Any]:
    """Strip secret/internal fields from a ``users`` row for API responses."""
    return {
        "username": str(user.get("username") or ""),
        "is_admin": bool(user.get("is_admin")),
        "import_dir": user.get("import_dir"),
        "created_at": user.get("created_at"),
        "updated_at": user.get("updated_at"),
    }


def public_user_with_records(user: dict[str, Any]) -> dict[str, Any]:
    """Public user shape plus the day_entries count (users listings).

    Used by ``GET /api/users`` (routes.py) and ``GET /api/admin/users`` so both
    listings expose the same wire contract: booleans/normalized fields, no
    internal id, no password data, and the record count from ``day_entries``.
    """
    shape = public_user(user)
    shape["records"] = int(user.get("records") or 0)
    return shape


# --------------------------------------------------------------------------- #
# Decorators
# --------------------------------------------------------------------------- #
def login_required[F: Callable[..., Any]](view: F) -> F:
    """Reject unauthenticated requests with the uniform 401 (app-level)."""

    @wraps(view)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        if current_user() is None:
            abort(401, description="authentication required")
        return view(*args, **kwargs)

    return cast(F, wrapped)


def admin_required[F: Callable[..., Any]](view: F) -> F:
    """Reject non-admin and unauthenticated requests (401/403)."""

    @wraps(view)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        user = current_user()
        if user is None:
            abort(401, description="authentication required")
        if not bool(user.get("is_admin")):
            abort(403, description="admin privileges required")
        return view(*args, **kwargs)

    return cast(F, wrapped)


# --------------------------------------------------------------------------- #
# API key generation
# --------------------------------------------------------------------------- #
def _generate_api_key(conn: Any, user_id: int, name: str = "") -> dict[str, str]:
    """Create an API key row; return ``{"prefix", "key"}`` (key shown once)."""
    key = _API_KEY_PREFIX + secrets.token_hex(32)
    key_hash = hashlib.sha256(key.encode("utf-8")).hexdigest()
    # Keep enough of the plaintext for a human to recognize the key.
    prefix = key[: len(_API_KEY_PREFIX) + 10]
    create_api_key(conn, user_id, key_prefix=prefix, key_hash=key_hash, name=name)
    return {"prefix": prefix, "key": key}


# --------------------------------------------------------------------------- #
# /api/auth/* blueprint (Web UI session + personal API keys)
# --------------------------------------------------------------------------- #
@auth_bp.post("/login")
def auth_login() -> Any:
    """Authenticate ``{username, password}`` and set the signed session cookie."""
    payload = request.get_json(silent=True) or {}
    username = str(payload.get("username") or "").strip()
    password = str(payload.get("password") or "")
    if not username or not password:
        return (
            jsonify({"error": "username and password are required", "type": "bad_request"}),
            400,
        )
    user = user_by_username(_store().connect(), username)
    if user is None or not verify_password(password, str(user.get("password_hash") or "")):
        log.info("failed login attempt for username=%r", username)
        return jsonify({"error": "invalid credentials", "type": "unauthorized"}), 401
    session.permanent = True
    session["username"] = username
    log.info("user %r logged in", username)
    return jsonify({"user": public_user(user)}), 200


@auth_bp.post("/logout")
def auth_logout() -> Any:
    """Clear the signed session cookie."""
    session.clear()
    return jsonify({"ok": True}), 200


@auth_bp.get("/me")
def auth_me() -> Any:
    """Return the authenticated identity (``{"user": {...}}``) or 401."""
    user = current_user()
    if user is None:
        return jsonify({"error": "authentication required", "type": "unauthorized"}), 401
    return jsonify({"user": public_user(user)}), 200


@auth_bp.route("/api-keys", methods=["GET", "POST"])
@login_required
def auth_api_keys() -> Any:
    """Manage the authenticated user's API keys (list/create)."""
    view: dict[str, Any] = current_user()
    conn = _store().connect()
    if request.method == "GET":
        keys = list_api_keys(conn, int(view["id"]))
        return jsonify({"api_keys": keys}), 200
    payload = request.get_json(silent=True) or {}
    name = str(payload.get("name") or "").strip()
    generated = _generate_api_key(conn, int(view["id"]), name=name)
    conn.commit()
    return jsonify(generated), 201


@auth_bp.delete("/api-keys/<prefix>")
@login_required
def auth_delete_api_key(prefix: str) -> Any:
    """Revoke one of the authenticated user's API keys (by display prefix)."""
    conn = _store().connect()
    affected = revoke_api_key(conn, int(current_user()["id"]), prefix)
    conn.commit()
    if affected == 0:
        abort(404, description="api key not found")
    return jsonify({"ok": True}), 200


# --------------------------------------------------------------------------- #
# /api/admin/* blueprint (user management is admin-only, D6)
# --------------------------------------------------------------------------- #
@admin_bp.get("/users")
@admin_required
def admin_list_users() -> Any:
    """List every user with record counts (admin-only)."""
    conn = _store().connect()
    users = [public_user_with_records(row) for row in list_users_with_counts(conn)]
    return jsonify({"users": users}), 200


@admin_bp.get("/settings")
@admin_required
def admin_get_settings() -> Any:
    import json
    import os

    from flask import current_app

    cfg = current_app.config.get("DAYLIO")
    backend = "influx"
    if cfg:
        if isinstance(getattr(cfg, "storage", None), dict):
            backend = cfg.storage.get("analytics_backend", "influx")
        elif hasattr(cfg, "_raw"):
            backend = (cfg._raw.get("storage") or {}).get("analytics_backend", "influx")
    data_dir = os.environ.get("DATA_DIR") or "/data"
    path = os.path.join(data_dir, "settings.json")
    if os.path.exists(path):
        try:
            with open(path) as f:
                s2 = json.load(f)
            if s2.get("analytics_backend"):
                backend = s2["analytics_backend"]
        except Exception:
            pass
    return jsonify({"analytics_backend": backend}), 200


@admin_bp.post("/settings")
@admin_required
def admin_set_settings() -> Any:
    import json
    import os

    from flask import current_app, request

    data = request.get_json(silent=True) or {}
    backend = str(data.get("analytics_backend") or "").strip().lower()
    if backend not in {"influx", "sqlite"}:
        return jsonify({"error": "analytics_backend must be 'influx' or 'sqlite'", "type": "bad_request"}), 400

    data_dir = os.environ.get("DATA_DIR") or "/data"
    try:
        os.makedirs(data_dir, exist_ok=True)
        path = os.path.join(data_dir, "settings.json")
        cur = {}
        if os.path.exists(path):
            try:
                with open(path) as f:
                    cur = json.load(f)
            except Exception:
                cur = {}
        old = str(cur.get("analytics_backend") or "influx").strip().lower()
        cur["analytics_backend"] = backend
        with open(path, "w") as f:
            json.dump(cur, f, indent=2)
        cfg = current_app.config.get("DAYLIO")
        if cfg:
            if isinstance(getattr(cfg, "storage", None), dict):
                cfg.storage["analytics_backend"] = backend
            elif hasattr(cfg, "_raw"):
                cfg._raw.setdefault("storage", {})["analytics_backend"] = backend

        result = {"ok": True, "analytics_backend": backend, "old": old}
        if old == "sqlite" and backend == "influx":
            result["sync_needed"] = True
        return jsonify(result), 200
    except Exception as e:
        return jsonify({"error": str(e), "type": "server_error"}), 500


@admin_bp.post("/sync-influx")
@admin_required
def admin_sync_influx() -> Any:
    from daylio_api.api.routes import _influx, _store
    try:
        conn = _store().connect()
        influx = _influx()
        stats = resync_all(conn, influx, user=None)
        return jsonify({"ok": True, "stats": stats}), 200
    except Exception as e:
        return jsonify({"error": str(e), "type": "server_error"}), 500


@admin_bp.post("/users")
@admin_required
def admin_create_user() -> Any:
    """Create a user from ``{username, password, is_admin?, import_dir?}``."""
    payload = request.get_json(silent=True) or {}
    username = str(payload.get("username") or "").strip()
    password = str(payload.get("password") or "")
    if not username or not password:
        return (
            jsonify({"error": "username and password are required", "type": "bad_request"}),
            400,
        )
    conn = _store().connect()
    if user_by_username(conn, username) is not None:
        return jsonify({"error": "user already exists", "type": "conflict"}), 409
    record = {
        "username": username,
        "is_admin": bool(payload.get("is_admin")),
        "import_dir": str(payload.get("import_dir") or "").strip() or None,
    }
    create_user(
        conn,
        username,
        password_hash=hash_password(password),
        is_admin=record["is_admin"],
        import_dir=record["import_dir"],
    )
    conn.commit()
    log.info("admin %r created user %r", current_user().get("username"), username)
    return jsonify({"user": record}), 201


@admin_bp.delete("/users/<username>")
@admin_required
def admin_delete_user(username: str) -> Any:
    """Delete a user and every owned row (records/audit/keys/config).

    The last remaining admin account cannot be deleted (it would lock the
    deployment out of user management); other users, including secondary
    admins, may be removed freely.
    """
    conn = _store().connect()
    target = user_by_username(conn, username)
    if target is None:
        abort(404, description="user not found")
    if bool(target.get("is_admin")):
        admins = conn.execute(
            "SELECT COUNT(*) AS n FROM users WHERE is_admin = 1"
        ).fetchone()["n"]
        if int(admins) <= 1:
            return (
                jsonify(
                    {
                        "error": "cannot delete the last admin user",
                        "type": "conflict",
                    }
                ),
                409,
            )
    affected = delete_user(conn, username)
    conn.commit()
    if affected == 0:
        abort(404, description="user not found")
    log.info("admin %r deleted user %r", current_user().get("username"), username)
    return jsonify({"ok": True}), 200


# --------------------------------------------------------------------------- #
# First-boot admin bootstrap (D6)
# --------------------------------------------------------------------------- #
def init_admin(store: SqliteStore, cfg: AppConfig) -> dict[str, Any] | None:
    """Create/promote the admin user from the validated configuration.

    Reads ``auth.admin_username``/``auth.admin_password`` (env overrides are
    applied by ``config.load``). Runs on every boot inside the app factory.
    Missing credentials log a clear warning and return None — the module-level
    ``app = create_app()`` must never fail for an unconfigured initial admin.

    Promotions: an existing user with the admin username is promoted to
    ``is_admin = 1``; the password is only written when the user has no hash
    yet (existing accounts keep their password).
    """
    auth = cfg.auth if isinstance(cfg.auth, dict) else {}
    username = str(auth.get("admin_username") or "").strip()
    password = str(auth.get("admin_password") or "")
    if not username or not password:
        log.warning(
            "ADMIN_USERNAME/ADMIN_PASSWORD not configured; no admin user "
            "created (D6)"
        )
        return None

    conn = store.connect()
    existing = user_by_username(conn, username)
    password_hash = hash_password(password)
    if existing is None:
        create_user(conn, username, password_hash=password_hash, is_admin=True)
        log.info("created admin user %r from ADMIN_USERNAME (D6)", username)
    else:
        if not bool(existing.get("is_admin")):
            conn.execute(
                "UPDATE users SET is_admin = 1, updated_at = datetime('now') "
                "WHERE username = ?",
                (username,),
            )
            log.info("promoted existing user %r to admin (D6)", username)
        if not str(existing.get("password_hash") or ""):
            conn.execute(
                "UPDATE users SET password_hash = ?, updated_at = datetime('now') "
                "WHERE username = ?",
                (password_hash, username),
            )
            log.info("set the admin password for %r from ADMIN_PASSWORD (D6)", username)
    conn.commit()
    return {"username": username, "is_admin": True}
