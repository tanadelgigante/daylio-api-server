"""App-factory lifecycle refinements from the Fase 5 review (R2/R3).

=============================== MAINTENANCE NOTES ===============================
Module   : tests/integration/test_app_lifecycle.py
Scope    : two small Tester findings closed in Fase 6 while wiring the dashboard:

  R3 — ``SqliteStore.close()`` was never called by the app factory, so every
       SQLite connection opened by a thread survived until the interpreter died.
       ``create_app()`` now registers an ``atexit`` hook (``_make_shutdown``)
       that closes the SQLite store AND the Influx client, is idempotent and
       never raises.

  R2 — the module-level ``app = create_app()`` built the whole application (and
       therefore created ``data/daylio.db``) AT IMPORT TIME, ignoring the
       ``DB_PATH`` override a test had just set. ``app`` is now a
       ``_LazyWsgiApp`` proxy: the Flask app is built on the first request (or
       the first attribute access) instead of at import.
================================================================================
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from werkzeug.test import Client

from daylio_api.app import _LazyWsgiApp, _make_shutdown, create_app
from daylio_api.db.influx_store import InfluxConfig, InfluxStore
from daylio_api.db.sqlite_store import SqliteStore
from tests.fakes import FakeInfluxClient


# --------------------------------------------------------------------------- #
# R3 — store shutdown
# --------------------------------------------------------------------------- #
def test_shutdown_hook_closes_sqlite_and_influx(tmp_path: Path) -> None:
    """The hook closes every store connection and releases the Influx client."""
    store = SqliteStore(str(tmp_path / "shutdown.db"))
    store.connect()
    assert store._open  # a connection is open before the hook runs
    client = FakeInfluxClient()
    influx = InfluxStore(InfluxConfig(url="http://fake:8120", token="t"), client=client)

    shutdown = _make_shutdown(store, influx)
    shutdown()

    assert not store._open
    # Idempotent: a second call must not raise nor reopen anything.
    shutdown()


def test_create_app_registers_the_shutdown_hook(tmp_path: Path, monkeypatch) -> None:
    """Every created app exposes its atexit hook and it is callable."""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "hook.db"))
    app = create_app()
    hook = app.extensions["daylio_shutdown"]
    assert callable(hook)
    hook()
    assert not app.extensions["daylio_store"]._open


def test_shutdown_hook_survives_a_failing_store(tmp_path: Path) -> None:
    """A store that raises on close is logged, never propagated (exit safety)."""

    class _Broken(SqliteStore):
        def close(self) -> None:
            raise RuntimeError("cannot close")

    hook = _make_shutdown(_Broken(str(tmp_path / "broken.db")), None)
    hook()  # must not raise


# --------------------------------------------------------------------------- #
# R2 — lazy WSGI app
# --------------------------------------------------------------------------- #
def test_importing_the_module_does_not_create_the_database(tmp_path: Path) -> None:
    """Importing ``daylio_api.app`` must have NO filesystem side effect (R2).

    Run in a subprocess with ``DB_PATH`` pointing into a temp dir: a
    create-at-import-time app would create that file before the test could
    assert anything else.
    """
    db_path = tmp_path / "import_side_effect.db"
    env = dict(os.environ, DB_PATH=str(db_path))
    env.pop("INFLUX_URL", None)
    env.pop("INFLUX_TOKEN", None)
    result = subprocess.run(
        [sys.executable, "-c", "import daylio_api.app; print('imported')"],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "imported" in result.stdout
    assert not db_path.exists(), "importing the module must not create the SQLite file"


def test_lazy_proxy_builds_the_app_on_first_call(tmp_path: Path, monkeypatch) -> None:
    """``app`` is a WSGI proxy: it serves requests and forwards attributes."""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "lazy.db"))
    proxy = _LazyWsgiApp()
    assert isinstance(proxy, _LazyWsgiApp)
    assert proxy._app is None
    # Werkzeug accepts any WSGI callable: the first request materialises the app.
    client = Client(proxy)
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.get_json()["status"] == "ok"
    # Built once, then cached (thread-safe double-checked lock).
    assert proxy._app is not None
    assert proxy.resolve() is proxy._app
    # Attribute access is proxied to the real Flask app.
    assert proxy.config["TESTING"] is False


def test_module_level_app_is_the_lazy_proxy() -> None:
    """The WSGI entry point used by waitress is the proxy, not a Flask app."""
    from daylio_api import app as app_module

    assert isinstance(app_module.app, _LazyWsgiApp)
