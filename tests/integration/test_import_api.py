"""Fase 5 (R7a) integration tests: upload + per-user folder import + scheduler.

Covers Task 5.3 (upload end-to-end through the pipeline), Task 5.4/R7a (the
per-user folder trigger with done//failed/ file moves) and the APScheduler job
registration for users that have an ``import_dir`` (R7/D6). The autouse
``_phase5_admin_env`` fixture guarantees the admin credentials for ``create_app``.
"""

from __future__ import annotations

import io
import shutil
from pathlib import Path

import pytest

from daylio_api.app import create_app
from daylio_api.config import load as load_config
from daylio_api.db.sqlite_store import SqliteStore, create_user
from daylio_api.scheduler import create_scheduler
from tests.helpers import login

#: Binary (non-CSV, non-ZIP) content used to trigger "unrecognized format".
_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


@pytest.fixture
def api_client(tmp_path: Path, monkeypatch):
    """Flask test client backed by a fresh temp SQLite database."""
    tmp_db = tmp_path / "import_test.db"
    monkeypatch.setenv("DB_PATH", str(tmp_db))
    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()
    resp = login(client)
    assert resp.status_code == 200, resp.get_json()
    return client


def _create_user(client, username: str, password: str = "pass-123") -> None:
    """Create a user via the admin API (logs the admin back out afterwards)."""
    resp = login(client)
    assert resp.status_code == 200, resp.get_json()
    resp = client.post(
        "/api/admin/users", json={"username": username, "password": password}
    )
    assert resp.status_code == 201, resp.get_json()
    client.post("/api/auth/logout")


# --------------------------------------------------------------------------- #
# Upload (Task 5.3)
# --------------------------------------------------------------------------- #
def test_upload_csv_creates_records_and_job(api_client, mini_csv) -> None:
    with mini_csv.open("rb") as fh:
        resp = api_client.post(
            "/api/import",
            data={"file": (fh, mini_csv.name)},
            content_type="multipart/form-data",
        )
    assert resp.status_code == 200, resp.get_json()
    job = resp.get_json()["job"]
    assert job["status"] == "completed"
    assert job["imported"] == 5
    assert job["errors"] == 0
    assert job["source"] == "upload"
    assert job["user"] == "admin"
    assert job["job_id"] is not None
    assert isinstance(job["started_at"], str)

    # The uploaded records are queryable through the canonical API.
    payload = api_client.get("/api/records").get_json()
    assert payload["total"] == 5
    dates = [record["full_date"] for record in payload["records"]]
    assert len(set(dates)) == 5


def test_upload_daylio_creates_records(api_client, mini_daylio) -> None:
    with mini_daylio.open("rb") as fh:
        resp = api_client.post(
            "/api/import",
            data={"file": (fh, mini_daylio.name)},
            content_type="multipart/form-data",
        )
    assert resp.status_code == 200, resp.get_json()
    job = resp.get_json()["job"]
    assert job["status"] == "completed"
    assert job["imported"] == 3
    assert job["user"] == "admin"

    payload = api_client.get("/api/records").get_json()
    assert payload["total"] == 3


def test_upload_admin_to_other_user(api_client, mini_csv) -> None:
    """An admin can upload into another user's account (?user=<name>)."""
    with mini_csv.open("rb") as fh:
        resp = api_client.post(
            "/api/import",
            data={"user": "alice", "file": (fh, mini_csv.name)},
            content_type="multipart/form-data",
        )
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["job"]["user"] == "alice"
    payload = api_client.get("/api/records?user=alice").get_json()
    assert payload["total"] == 5


def test_upload_unsupported_extension_400(api_client) -> None:
    resp = api_client.post(
        "/api/import",
        data={"file": (io.BytesIO(b"hello"), "notes.txt")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["type"] == "bad_request"
    assert ".csv" in body["error"]


def test_upload_missing_file_400(api_client) -> None:
    resp = api_client.post("/api/import", data={}, content_type="multipart/form-data")
    assert resp.status_code == 400
    assert resp.get_json()["type"] == "bad_request"


def test_upload_unrecognized_binary_400(api_client) -> None:
    """A .csv file whose magic bytes are neither ZIP nor ASCII -> 400."""
    resp = api_client.post(
        "/api/import",
        data={"file": (io.BytesIO(_PNG), "garbage.csv")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    assert "format" in resp.get_json()["error"]


# --------------------------------------------------------------------------- #
# Per-user folder import (Task 5.4 / R7a)
# --------------------------------------------------------------------------- #
def test_folder_import_completed_moves_done(
    api_client, tmp_path: Path, mini_csv
) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    shutil.copy(mini_csv, inbox / "export.csv")

    resp = api_client.post(
        "/api/import/folder",
        data={"import_dir": str(inbox)},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200, resp.get_json()
    imports = resp.get_json()["imports"]
    assert len(imports) == 1
    summary = imports[0]
    assert summary["status"] == "completed"
    assert summary["imported"] == 5
    assert summary["user"] == "admin"
    assert summary["source"] == "folder"
    assert summary["moved_to"] == str(inbox / "done")
    assert (inbox / "done" / "export.csv").exists()
    assert not (inbox / "export.csv").exists()

    # Records are queryable (admin defaults to their own username).
    assert api_client.get("/api/records").get_json()["total"] == 5


def test_folder_import_failed_file_moves_failed(
    api_client, tmp_path: Path
) -> None:
    inbox = tmp_path / "inbox-failed"
    inbox.mkdir()
    (inbox / "broken.csv").write_bytes(_PNG)

    resp = api_client.post(
        "/api/import/folder",
        data={"import_dir": str(inbox)},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200, resp.get_json()
    imports = resp.get_json()["imports"]
    assert len(imports) == 1
    summary = imports[0]
    assert summary["status"] == "failed"
    assert summary["errors"] == 1
    assert summary["moved_to"] == str(inbox / "failed")
    assert (inbox / "failed" / "broken.csv").exists()

    # A failed scan still leaves an audit trail.
    jobs = api_client.get("/api/import/jobs").get_json()["jobs"]
    assert jobs[0]["status"] == "failed"


def test_folder_missing_400(api_client) -> None:
    resp = api_client.post(
        "/api/import/folder",
        data={"import_dir": "/definitely/not/here"},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    assert "not found" in resp.get_json()["error"]


def test_folder_without_configuration_400(api_client) -> None:
    """No import_dir anywhere for the viewer -> clear 400."""
    resp = api_client.post(
        "/api/import/folder", data={}, content_type="multipart/form-data"
    )
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["type"] == "bad_request"
    assert "import_dir" in body["error"]


def test_folder_import_requires_auth(tmp_path: Path, monkeypatch) -> None:
    tmp_db = tmp_path / "anon.db"
    monkeypatch.setenv("DB_PATH", str(tmp_db))
    app = create_app()
    app.config["TESTING"] = True
    anon = app.test_client()
    resp = anon.post(
        "/api/import/folder", data={}, content_type="multipart/form-data"
    )
    assert resp.status_code == 401


def test_folder_import_other_user_forbidden(
    api_client, tmp_path: Path
) -> None:
    _create_user(api_client, "bob")
    api_client.post("/api/auth/logout")
    resp = login(api_client, username="bob", password="pass-123")
    assert resp.status_code == 200

    resp = api_client.post(
        "/api/import/folder",
        data={"user": "alice", "import_dir": str(tmp_path / "alice-inbox")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 403
    assert resp.get_json()["type"] == "forbidden"


# --------------------------------------------------------------------------- #
# Import jobs isolation (D6)
# --------------------------------------------------------------------------- #
def test_import_jobs_isolation_per_user(api_client, mini_csv) -> None:
    # Admin imports into alice, then a second non-admin cannot see those jobs.
    with mini_csv.open("rb") as fh:
        resp = api_client.post(
            "/api/import",
            data={"user": "alice", "file": (fh, mini_csv.name)},
            content_type="multipart/form-data",
        )
    assert resp.status_code == 200
    api_client.post("/api/auth/logout")

    _create_user(api_client, "bob")
    login(api_client, username="bob", password="pass-123")

    resp = api_client.get("/api/import/jobs?user=alice")
    assert resp.status_code == 403

    resp = api_client.get("/api/import/jobs")
    assert resp.status_code == 200
    assert resp.get_json()["jobs"] == []  # bob has no jobs


# --------------------------------------------------------------------------- #
# Scheduler job registration (R7/D6)
# --------------------------------------------------------------------------- #
def test_create_scheduler_registers_only_import_dir_users(tmp_path: Path) -> None:
    db_path = tmp_path / "sched_test.db"
    store = SqliteStore(str(db_path))
    conn = store.connect()
    create_user(conn, "carol", password_hash="x", import_dir=str(tmp_path / "carol"))
    create_user(conn, "nobody", password_hash="x")  # no folder -> skipped
    conn.commit()

    cfg = load_config()
    sched = create_scheduler(store, None, cfg)
    job_ids = [job.id for job in sched.get_jobs()]
    assert "import:carol" in job_ids
    assert "import:nobody" not in job_ids

    job = sched.get_job("import:carol")
    assert job is not None
    assert job.kwargs["username"] == "carol"
    assert job.kwargs["import_dir"] == str(tmp_path / "carol")
    assert job.kwargs["db_path"] == str(db_path)
    # The scheduler is registered but NOT started (app factory starts it).
    assert sched.state == 0  # STATE_STOPPED
    store.close()


def test_create_scheduler_respects_per_user_interval(tmp_path: Path) -> None:
    db_path = tmp_path / "sched_test2.db"
    store = SqliteStore(str(db_path))
    conn = store.connect()
    create_user(conn, "carol", password_hash="x", import_dir=str(tmp_path / "carol"))
    conn.commit()

    cfg = load_config()
    # Override carol's interval through the per-user scheduler section.
    cfg.scheduler.setdefault("per_user", {})["carol"] = {"interval_seconds": 3600}
    sched = create_scheduler(store, None, cfg)
    job = sched.get_job("import:carol")
    assert job.trigger.interval.total_seconds() == 3600
    store.close()
