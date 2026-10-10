"""Shared pytest fixtures (Fase 0, Task 0.5).

MAINTENANCE NOTES:
    - ``tmp_sqlite``  : temp SQLite path usable by ``SqliteStore`` tests.
    - ``sample_csv`` / ``sample_daylio`` : paths to the real ``sample-data/``
      files (kept for later phases) and to the mini fixtures under
      ``tests/fixtures/`` (versioned, small, derived from the real ones).
    - ``config_sample`` : loads the example configuration (schema reference).
    - ``fake_influx_client`` / ``influx_store`` : injectable InfluxDB fake
      (Fase 3) so store/import introspection never touches the network.
    - ``captured_logs`` : installs the logger capture handler for the duration
      of a test and always restores it afterwards.
    - ``live_influx`` (FASE 7, D18) : REAL ``InfluxStore`` built from the CI
      environment; skips when the endpoint/token is missing. Only used by
      ``@pytest.mark.influx`` tests.
    - ``_phase5_admin_env`` (autouse) : guarantees every ``create_app()`` can
      bootstrap the admin user (D6) — ``ADMIN_USERNAME``/``ADMIN_PASSWORD``
      come from the environment. Tests that need other users create them via
      the admin API.

MAINTENANCE NOTES (FASE 7, Task 7.1 — D18/R16, live InfluxDB marker):
    Tests marked with ``@pytest.mark.influx`` need a REAL InfluxDB 2.x. They
    are skipped unless the endpoint is configured through ``INFLUX_URL`` (or
    ``INFLUX_INSTANCE``, which ``config.load`` expands to ``http://<ip>:<port>``)
    together with ``INFLUX_TOKEN``. ``CI_SKIP_INFLUX=1`` forces the skip and is
    exported by the CI ``test`` job when ``vars.INFLUX_INSTANCE`` is unset
    (see .gitea/workflows/ci.yml). Tests bound to
    :class:`tests.fakes.FakeInfluxClient` are deliberately NOT marked and always
    run, so the bulk of the suite stays hermetic.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from daylio_api import config as config_module
from daylio_api import logger as logger_module
from daylio_api.db.influx_store import InfluxConfig, InfluxStore
from tests.fakes import FakeInfluxClient

TESTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = TESTS_DIR.parent
FIXTURES_DIR = TESTS_DIR / "fixtures"
SAMPLE_DATA_DIR = PROJECT_ROOT / "sample-data"

#: Env var exported by the CI job `test` to disable the live InfluxDB tests.
CI_SKIP_INFLUX_ENV = "CI_SKIP_INFLUX"


def live_influx_requested() -> bool:
    """Return True when a live InfluxDB integration is expected (D18).

    Conditions (all must hold):
      - ``CI_SKIP_INFLUX`` must NOT be set to a truthy value;
      - an endpoint must be reachable through the config: ``INFLUX_URL`` or the
        CI shortcut ``INFLUX_INSTANCE`` (``"<ip>:<port>"``, D18);
      - an API token must be present in ``INFLUX_TOKEN``.

    Returns:
        False (tests marked ``influx`` are skipped) in every other case, so a
        developer running the suite offline is never blocked (R16).
    """
    if os.environ.get(CI_SKIP_INFLUX_ENV, "").strip().lower() in {"1", "true", "yes"}:
        return False
    url = os.environ.get("INFLUX_URL", "").strip()
    instance = os.environ.get("INFLUX_INSTANCE", "").strip()
    if not url and instance:
        url = f"http://{instance}"
    if not url or url.startswith("env:"):
        return False
    return bool(os.environ.get("INFLUX_TOKEN", "").strip())


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip every ``@pytest.mark.influx`` test when no live InfluxDB is available.

    MAINTENANCE NOTES:
        This is the single gate used by CI: with ``vars.INFLUX_INSTANCE``
        configured the tests run against the real server, otherwise the job
        also passes ``-m "not influx"`` and this hook is a second, local-safe
        net (e.g. a developer with a stale INFLUX_URL in their shell).
    """
    if live_influx_requested():
        return
    skip_influx = pytest.mark.skip(
        reason=(
            "live InfluxDB integration not available: set INFLUX_URL (or "
            "INFLUX_INSTANCE '<ip>:<port>') + INFLUX_TOKEN, and do not set "
            f"{CI_SKIP_INFLUX_ENV}"
        )
    )
    for item in items:
        if "influx" in item.keywords:
            item.add_marker(skip_influx)


@pytest.fixture(autouse=True)
def _phase5_admin_env(monkeypatch) -> None:
    """(Fase 5, D6) Set the boot-time admin credentials for every test.

    Runs before every test (autouse): ``create_app()`` calls ``init_admin``
    with these credentials, so the test client can always log in as ``admin``.
    """
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "admin-pass")
    yield


@pytest.fixture
def tmp_sqlite(tmp_path: Path) -> Path:
    """Absolute path to a temp SQLite database file for a single test."""
    return tmp_path / "test_daylio.db"


@pytest.fixture
def sample_csv() -> Path:
    """Path to the real sample CSV export (large fixture, not versioned here)."""
    return SAMPLE_DATA_DIR / "daylio_export_2026_09_22.csv"


@pytest.fixture
def sample_daylio() -> Path:
    """Path to the real sample .daylio backup (large fixture)."""
    return SAMPLE_DATA_DIR / "backup_2026_09_22.daylio"


@pytest.fixture
def mini_csv() -> Path:
    """Path to the small versioned CSV fixture (5 clean rows, UTF-8 BOM)."""
    return FIXTURES_DIR / "mini_daylio_export.csv"


@pytest.fixture
def mini_daylio() -> Path:
    """Path to the small versioned .daylio fixture (ZIP/Base64/JSON, 3 entries)."""
    return FIXTURES_DIR / "mini_backup.daylio"


@pytest.fixture
def config_sample() -> config_module.AppConfig:
    """Loads the example application config (schema reference, §6)."""
    return config_module.load()


@pytest.fixture
def captured_logs() -> list[str]:
    """Capture all daylio_api log messages during the test.

    The fixture restores the normal stderr handler on teardown even if the
    test fails, so capture state never leaks between tests.
    """
    started = logger_module.capture()
    yield started
    logger_module.release()


@pytest.fixture
def fake_influx_client() -> FakeInfluxClient:
    """Fresh in-memory InfluxDB fake (Fase 3, delete-and-write semantics)."""
    return FakeInfluxClient()


@pytest.fixture
def influx_store(fake_influx_client: FakeInfluxClient) -> InfluxStore:
    """Configured InfluxStore bound to the fake client (no real endpoint).

    The url/token are dummy values that only make ``is_configured()`` True;
    all wire traffic is intercepted by the fake (R8 CI-safe).
    """
    return InfluxStore(
        InfluxConfig(
            url="http://fake-influx:8120",
            token="fake-token",
            org="tanadelgigante",
            bucket="daylio",
            measurement="daylio_entry",
        ),
        client=fake_influx_client,
    )


@pytest.fixture
def live_influx() -> InfluxStore:
    """REAL InfluxDB store for tests marked ``@pytest.mark.influx`` (D18).

    Builds the store from the environment exactly like the CI ``test`` job:
    ``INFLUX_URL`` (or ``INFLUX_INSTANCE="<ip>:<port>"`` -> ``http://...``),
    ``INFLUX_TOKEN``, ``INFLUX_ORG`` (default ``tanadelgigante``) and
    ``INFLUX_BUCKET`` (default ``daylio``). Skips when the endpoint is not
    configured or ``CI_SKIP_INFLUX=1``; the real client is closed on teardown.
    """
    if not live_influx_requested():
        pytest.skip(
            "live InfluxDB integration not available "
            f"(INFLUX_URL/INFLUX_INSTANCE + INFLUX_TOKEN, {CI_SKIP_INFLUX_ENV} unset)"
        )
    url = os.environ.get("INFLUX_URL", "").strip()
    instance = os.environ.get("INFLUX_INSTANCE", "").strip()
    if not url and instance:
        url = f"http://{instance}"
    store = InfluxStore(
        InfluxConfig(
            url=url,
            token=os.environ.get("INFLUX_TOKEN", "").strip(),
            org=os.environ.get("INFLUX_ORG", "").strip() or "tanadelgigante",
            bucket=os.environ.get("INFLUX_BUCKET", "").strip() or "daylio",
            measurement="daylio_entry",
        )
    )
    try:
        yield store
    finally:
        store.close()
