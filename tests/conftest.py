"""Shared pytest fixtures (Fase 0, Task 0.5).

MAINTENANCE NOTES:
    - ``tmp_sqlite``  : temp SQLite path usable by ``SqliteStore`` tests.
    - ``sample_csv`` / ``sample_daylio`` : paths to the real ``sample-data/``
      files (kept for later phases) and to the mini fixtures under
      ``tests/fixtures/`` (versioned, small, derived from the real ones).
    - ``config_sample`` : loads the example configuration (schema reference).
    - ``captured_logs`` : installs the logger capture handler for the duration
      of a test and always restores it afterwards.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from daylio_api import config as config_module
from daylio_api import logger as logger_module

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
