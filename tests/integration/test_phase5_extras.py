"""Fase 5 QA extras: independent acceptance coverage + documented bugs (SOLO test).

This suite pins acceptance criteria of WORKPLAN §7/Phase 5 (R6, R7, D6) that the
existing suites (test_auth_api.py / test_import_api.py) do not exercise end-to-end:

  - real 630-row sample CSV and .daylio upload through ``POST /api/import``
    (acceptance: "sample 630 -> 628 imported");
  - folder import idempotency ("no double import of an already processed file");
  - scheduler gating OFF when ``scheduler.enabled=false`` (default config);
  - per-user YAML ``users.<name>.scheduler`` interval/hour override;
  - the APScheduler worker ``run_import_job`` executing ``import_folder`` in a
    worker thread without touching the network;
  - signed-session TTL cookie on login.

It also pins FOUR regressions reported by the Tester, now fixed by the Coder
(B1/B2/R1 in ``api_import_upload``, B3 in ``SqliteStore``) and enforced as real
(no longer ``xfail``) tests:

  B1. upload of an unrecognized-format file must return 400 AND write a failed
      ``import_jobs`` audit row ("formato sconosciuto -> 400 + audit failed").
  B2. upload of a *text* CSV whose mandatory ``full_date`` column is missing
      must be a clean 400 + failed audit row (never a 500 ``internal_error``).
  R1. upload of a CSV with a field over ``csv.field_size_limit`` must be a
      clean 400 + failed audit row (``csv.Error`` is not a ``ValueError``).
  B3. ``SqliteStore.connect`` returns ONE connection PER THREAD; with the
      mandated multi-threaded Waitress WSGI server concurrent requests must not
      raise ``sqlite3.ProgrammingError`` (check_same_thread). The scheduler
      opens a per-thread store; the request path must do the same.
"""

from __future__ import annotations

import io
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from daylio_api.app import create_app
from daylio_api.config import load as load_config
from daylio_api.db.sqlite_store import SqliteStore, create_user
from daylio_api.scheduler import create_scheduler, run_import_job
from tests.helpers import login

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


@pytest.fixture
def api_client(tmp_path: Path, monkeypatch):
    """Flask test client backed by a fresh temp SQLite database (as admin)."""
    tmp_db = tmp_path / "phase5_extra.db"
    monkeypatch.setenv("DB_PATH", str(tmp_db))
    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()
    resp = login(client)
    assert resp.status_code == 200, resp.get_json()
    return client


# --------------------------------------------------------------------------- #
# Criterion 4: real samples through the upload endpoint (630 -> 628)
# --------------------------------------------------------------------------- #
def test_upload_real_csv_sample_imports_628(api_client, sample_csv) -> None:
    """POST /api/import with the real 630-row CSV -> 628 unique days + audit."""
    with sample_csv.open("rb") as fh:
        resp = api_client.post(
            "/api/import",
            data={"file": (fh, sample_csv.name)},
            content_type="multipart/form-data",
        )
    assert resp.status_code == 200, resp.get_json()
    job = resp.get_json()["job"]
    assert job["status"] == "completed"
    assert job["total_rows"] == 630
    assert job["imported"] == 628
    assert job["skipped"] == 2
    assert job["errors"] == 0
    assert job["job_id"] is not None
    # audit row is queryable
    jobs = api_client.get("/api/import/jobs").get_json()["jobs"]
    assert jobs[0]["imported"] == 628


def test_upload_real_daylio_sample_imports_628(api_client, sample_daylio) -> None:
    """POST /api/import with the real .daylio archive -> 628 unique days."""
    with sample_daylio.open("rb") as fh:
        resp = api_client.post(
            "/api/import",
            data={"file": (fh, sample_daylio.name)},
            content_type="multipart/form-data",
        )
    assert resp.status_code == 200, resp.get_json()
    job = resp.get_json()["job"]
    assert job["status"] == "completed"
    assert job["imported"] == 628


def test_upload_parse_warning_still_audited(api_client) -> None:
    """A CSV with an undeclared column warns but still imports + audits."""
    csv_bytes = b"full_date,mood,bogus_col\n2026-01-01,bene,x\n2026-01-02,bene,y\n"
    resp = api_client.post(
        "/api/import",
        data={"file": (io.BytesIO(csv_bytes), "warn.csv")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200, resp.get_json()
    job = resp.get_json()["job"]
    assert job["imported"] == 2
    assert job["warnings"] >= 1


# --------------------------------------------------------------------------- #
# Criterion 5: folder import idempotency / "no double import"
# --------------------------------------------------------------------------- #
def test_folder_import_repeats_do_not_reprocess(
    api_client, tmp_path: Path, sample_csv
) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    shutil.copy(sample_csv, inbox / "export.csv")

    resp1 = api_client.post(
        "/api/import/folder",
        data={"import_dir": str(inbox)},
        content_type="multipart/form-data",
    )
    assert resp1.status_code == 200, resp1.get_json()
    assert len(resp1.get_json()["imports"]) == 1
    assert (inbox / "done" / "export.csv").exists()

    # A second scan finds nothing new (file already moved out of the folder).
    resp2 = api_client.post(
        "/api/import/folder",
        data={"import_dir": str(inbox)},
        content_type="multipart/form-data",
    )
    assert resp2.status_code == 200, resp2.get_json()
    assert resp2.get_json()["imports"] == []

    total = api_client.get("/api/records").get_json()["total"]
    assert total == 628  # never duplicated


# --------------------------------------------------------------------------- #
# Criterion 6: scheduler gating + per-user overrides + worker thread
# --------------------------------------------------------------------------- #
def test_scheduler_is_off_by_default(tmp_path: Path, monkeypatch) -> None:
    """scheduler.enabled=false (shipped default) -> no APScheduler instance."""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "off.db"))
    app = create_app()
    assert app.extensions.get("daylio_scheduler") is None


def test_scheduler_user_yaml_override_interval_and_hour(tmp_path: Path) -> None:
    """users.<name>.scheduler overrides interval_seconds and start_hour."""
    db_path = tmp_path / "sched_yaml.db"
    store = SqliteStore(str(db_path))
    conn = store.connect()
    create_user(conn, "dave", password_hash="x", import_dir=str(tmp_path / "dave"))
    conn.commit()

    cfg = load_config()
    cfg.scheduler["interval_seconds"] = 86400
    cfg.scheduler["start_hour"] = 0
    cfg.users["dave"] = {"scheduler": {"interval_seconds": 7777, "start_hour": 9}}
    sched = create_scheduler(store, None, cfg)
    job = sched.get_job("import:dave")
    assert job is not None
    assert job.trigger.interval.total_seconds() == 7777
    assert job.trigger.start_date.hour == 9
    assert sched.state == 0  # registered but NOT started
    store.close()


def test_run_import_job_imports_folder_in_worker_thread(
    tmp_path: Path, monkeypatch
) -> None:
    """The APScheduler worker runs import_folder(source='daily') w/o network."""
    monkeypatch.delenv("INFLUX_URL", raising=False)
    monkeypatch.delenv("INFLUX_TOKEN", raising=False)
    db_path = tmp_path / "job.db"
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    csv_bytes = b"full_date,mood\n2026-03-01,bene\n2026-03-02,male\n"
    (inbox / "export.csv").write_bytes(csv_bytes)

    cfg = load_config()
    run_import_job(db_path=str(db_path), username="alice", import_dir=str(inbox), config=cfg)

    assert (inbox / "done" / "export.csv").exists()
    assert not (inbox / "export.csv").exists()
    store = SqliteStore(str(db_path))
    conn = store.connect()
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM day_entries WHERE user = 'alice'"
    ).fetchone()["n"] == 2
    job = conn.execute(
        "SELECT source, status, imported FROM import_jobs ORDER BY id"
    ).fetchone()
    assert job["source"] == "daily"
    assert job["status"] == "completed"
    assert job["imported"] == 2
    store.close()


# --------------------------------------------------------------------------- #
# Criterion 3: signed session cookie carries the TTL
# --------------------------------------------------------------------------- #
def test_login_sets_signed_cookie_with_expiry(api_client) -> None:
    resp = login(api_client)
    set_cookie = resp.headers.get("Set-Cookie") or ""
    assert "daylio_session=" in set_cookie
    assert "Expires=" in set_cookie  # permanent session lifetime (TTL)


# --------------------------------------------------------------------------- #
# Admin list needs authentication (401) — minor gap fill
# --------------------------------------------------------------------------- #
def test_admin_users_requires_auth(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("DB_PATH", str(tmp_path / "anon.db"))
    app = create_app()
    app.config["TESTING"] = True
    anon = app.test_client()
    assert anon.get("/api/admin/users").status_code == 401


# --------------------------------------------------------------------------- #
# Regressions found by the Tester (B1/B2/B3) — must pass as REAL tests
# --------------------------------------------------------------------------- #
def test_upload_unknown_format_should_write_failed_audit(api_client) -> None:
    """Acceptance: 'formato sconosciuto -> 400 + audit failed' (criterion 4)."""
    resp = api_client.post(
        "/api/import",
        data={"file": (io.BytesIO(_PNG), "garbage.csv")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    jobs = api_client.get("/api/import/jobs").get_json()["jobs"]
    assert any(job["status"] == "failed" for job in jobs)


def test_upload_malformed_csv_is_400_and_audited_failed(api_client) -> None:
    """A recognized-text CSV missing 'full_date' must be a clean 400 + failed audit."""
    bad_csv = b"foo,bar\n1,2\n"
    resp = api_client.post(
        "/api/import",
        data={"file": (io.BytesIO(bad_csv), "bad.csv")},
        content_type="multipart/form-data",
    )
    assert resp.status_code in (400, 422), resp.get_json()
    jobs = api_client.get("/api/import/jobs").get_json()["jobs"]
    assert any(job["status"] == "failed" for job in jobs)


def test_upload_huge_field_is_400_and_audited_failed(api_client) -> None:
    """A CSV field over csv.field_size_limit (131072 B) is a clean 400 (R1).

    The stdlib ``csv`` module raises ``csv.Error`` (not ``ValueError``) for an
    oversized field; the upload handler must not 500 and must still audit.
    """
    huge_field = b"x" * (150 * 1024)  # single ~150 KiB field
    resp = api_client.post(
        "/api/import",
        data={"file": (io.BytesIO(huge_field), "huge.csv")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400, resp.get_json()
    jobs = api_client.get("/api/import/jobs").get_json()["jobs"]
    assert any(job["status"] == "failed" for job in jobs)


def test_api_requests_are_thread_safe(tmp_path: Path, monkeypatch) -> None:
    """Concurrent requests (Waitress worker threads) must not crash."""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "threads.db"))
    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()
    assert login(client).status_code == 200

    def _hit(_: int) -> int:
        return client.get("/api/records").status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = list(pool.map(_hit, range(16)))
    assert all(status == 200 for status in statuses)


def test_store_connect_is_thread_safe(tmp_path: Path) -> None:
    """Direct store use: each thread gets its own sqlite3 connection (B3)."""
    db_path = tmp_path / "tls.db"
    store = SqliteStore(str(db_path))
    conn = store.connect()
    create_user(conn, "multi", password_hash="x")
    conn.commit()

    def _read(_: int) -> int:
        local_conn = store.connect()
        return int(
            local_conn.execute(
                "SELECT COUNT(*) AS n FROM users WHERE username = 'multi'"
            ).fetchone()["n"]
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        counts = list(pool.map(_read, range(16)))
    assert all(count == 1 for count in counts)
    # Same-thread reuse: the main thread keeps the connection it created.
    assert store.connect() is conn
    store.close()
