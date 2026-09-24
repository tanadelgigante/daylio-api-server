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
"""

from __future__ import annotations

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
