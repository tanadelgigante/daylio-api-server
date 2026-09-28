"""Integration smoke tests (Fase 0 baseline, Task 0.5)."""

from __future__ import annotations

import importlib

import pytest

from daylio_api import config as config_module
from daylio_api.app import create_app
from daylio_api.db.sqlite_store import SqliteStore
from daylio_api.logger import configure, get_logger
from tests.helpers import login_assert_ok


def test_import_daylio_api_and_version() -> None:
    """`import daylio_api` works and __version__ comes from version.ini."""
    package = importlib.import_module("daylio_api")
    assert package.__version__ == "2.0.0-SNAPSHOT"
    assert package.VERSION == "2.0.0-SNAPSHOT"


def test_config_sample_load_is_valid(config_sample: config_module.AppConfig) -> None:
    cfg = config_sample
    assert cfg.mood["values"]["alla grande"] == 1
    assert cfg.mood["values"]["malissimo"] == 5
    assert cfg.storage["influx_org"] == "tanadelgigante"
    assert cfg.storage["influx_bucket"] == "daylio"
    assert cfg.effective_log_level in {"INFO", "DEBUG"}


def test_logger_respects_threshold(captured_logs) -> None:
    configure("INFO")
    logger = get_logger("baseline")
    logger.info("info-line")
    logger.debug("debug-line")
    assert any("info-line" in msg for msg in captured_logs)
    assert not any("debug-line" in msg for msg in captured_logs)
    logger.warning("warn-line")
    assert any("warn-line" in msg for msg in captured_logs)


def test_app_factory_and_healthz() -> None:
    app = create_app()
    client = app.test_client()
    resp = client.get("/healthz")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["status"] == "ok"
    assert payload["version"] == "2.0.0-SNAPSHOT"
    # Fase 1 registers the /api blueprint: version endpoint is reachable.
    resp = client.get("/api/version")
    assert resp.status_code == 200
    assert resp.get_json()["name"] == "daylio-api-server"
    # Fase 4 registers the parametric records endpoints (empty DB -> 200).
    # Fase 5 protects the data routes: log the admin in first (D6).
    login_assert_ok(client)
    resp = client.get("/api/records?user=default")
    assert resp.status_code == 200
    assert resp.get_json()["records"] == []


# --- Fase 0 fixture sanity: the mini fixtures are loadable and non-empty ------
def test_mini_csv_fixture_has_bom_and_five_rows(mini_csv) -> None:
    raw = mini_csv.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")  # UTF-8 BOM
    text = raw.decode("utf-8-sig")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    assert len(lines) == 6  # header + 5 data rows
    assert lines[0].startswith("full_date")


def test_mini_daylio_fixture_is_zip_with_backup(mini_daylio) -> None:
    import zipfile

    with zipfile.ZipFile(mini_daylio) as zf:
        assert "backup.daylio" in zf.namelist()
        data = zf.read("backup.daylio")
    import base64
    import json

    doc = json.loads(base64.b64decode(data).decode("utf-8"))
    assert doc["version"] == 15
    assert len(doc["dayEntries"]) == 3


def test_sqlite_store_connect(tmp_sqlite) -> None:
    with SqliteStore(tmp_sqlite) as store:
        conn = store.connect()
        row = conn.execute("SELECT 1 AS one").fetchone()
        assert row["one"] == 1
        assert tmp_sqlite.exists()


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
