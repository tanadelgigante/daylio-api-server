"""Fase 5 (R6/D6) integration tests: authentication, API keys, admin API.

The autouse ``_phase5_admin_env`` fixture boots ``ADMIN_USERNAME=admin`` /
``ADMIN_PASSWORD=admin-pass`` into every ``create_app``. This suite pins the
wire contract:

  - session login: 200 + signed cookie; 401 for a wrong/unknown credential;
    400 for missing fields; ``/api/auth/me`` reflects the identity; logout
    clears the session;
  - API keys (R6): create 201 (plaintext once), list, authenticate via
    ``Authorization: Bearer`` and ``X-API-Key``, revoke by prefix;
  - admin API: create/list/delete users, 409 on duplicates, 404 delete of an
    unknown user, 403 for non-admins;
  - D6 isolation: data routes require auth (401), a non-admin cannot read
    another user (403), an admin can; ``/api/columns`` + ``/api/scales`` stay
    public WITHOUT ``?user=``.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from daylio_api.app import create_app
from tests.helpers import login


@pytest.fixture
def api_client(tmp_path: Path, monkeypatch):
    """Flask test client backed by a fresh temp SQLite database."""
    tmp_db = tmp_path / "auth_test.db"
    monkeypatch.setenv("DB_PATH", str(tmp_db))
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


def _admin(client) -> None:
    """Log the client in as the bootstrapped admin and assert success."""
    resp = login(client)
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["user"]["is_admin"] is True


def _create_user(client, username: str, password: str = "pass-123") -> None:
    """Create a non-admin user via the admin API (and log the admin back out)."""
    _admin(client)
    resp = client.post(
        "/api/admin/users", json={"username": username, "password": password}
    )
    assert resp.status_code == 201, resp.get_json()
    assert resp.get_json()["user"]["username"] == username
    client.post("/api/auth/logout")


# --------------------------------------------------------------------------- #
# Session login / logout / me
# --------------------------------------------------------------------------- #
def test_login_success_sets_session_and_me(api_client) -> None:
    resp = login(api_client)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["user"]["username"] == "admin"
    assert body["user"]["is_admin"] is True

    me = api_client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.get_json()["user"]["username"] == "admin"


def test_login_wrong_password_401(api_client) -> None:
    resp = api_client.post(
        "/api/auth/login", json={"username": "admin", "password": "wrong"}
    )
    assert resp.status_code == 401
    assert resp.get_json()["type"] == "unauthorized"
    # No session is established.
    assert api_client.get("/api/auth/me").status_code == 401


def test_login_unknown_user_401(api_client) -> None:
    resp = api_client.post(
        "/api/auth/login", json={"username": "ghost", "password": "x"}
    )
    assert resp.status_code == 401


def test_login_missing_fields_400(api_client) -> None:
    resp = api_client.post("/api/auth/login", json={"username": "admin"})
    assert resp.status_code == 400
    assert resp.get_json()["type"] == "bad_request"

    resp = api_client.post("/api/auth/login", json={"password": "admin-pass"})
    assert resp.status_code == 400


def test_me_anonymous_401(api_client) -> None:
    resp = api_client.get("/api/auth/me")
    assert resp.status_code == 401
    assert resp.get_json()["type"] == "unauthorized"


def test_logout_invalidates_session(api_client) -> None:
    _admin(api_client)
    resp = api_client.post("/api/auth/logout")
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True}
    assert api_client.get("/api/auth/me").status_code == 401


# --------------------------------------------------------------------------- #
# API keys (R6)
# --------------------------------------------------------------------------- #
def test_api_key_create_list_use_revoke(api_client) -> None:
    _admin(api_client)

    # Initially empty.
    resp = api_client.get("/api/auth/api-keys")
    assert resp.status_code == 200
    assert resp.get_json()["api_keys"] == []

    # Create: the plaintext key is shown exactly once (201).
    resp = api_client.post("/api/auth/api-keys", json={"name": "ci"})
    assert resp.status_code == 201
    created = resp.get_json()
    assert created["key"].startswith("daylio_") and len(created["key"]) == len("daylio_") + 64
    prefix = created["prefix"]
    assert created["prefix"].startswith("daylio_")
    key = created["key"]

    # The stored list exposes only the prefix + metadata, never the secret.
    resp = api_client.get("/api/auth/api-keys")
    assert resp.status_code == 200
    listed = resp.get_json()["api_keys"]
    assert len(listed) == 1
    assert listed[0]["key_prefix"] == prefix
    assert "key" not in listed[0]

    # Authenticate with Bearer after dropping the session.
    api_client.post("/api/auth/logout")
    me = api_client.get("/api/auth/me", headers={"Authorization": f"Bearer {key}"})
    assert me.status_code == 200
    assert me.get_json()["user"]["username"] == "admin"

    # X-API-Key header works too.
    me2 = api_client.get("/api/auth/me", headers={"X-API-Key": key})
    assert me2.status_code == 200

    # Revoke by prefix -> the key no longer authenticates. DELETE is a
    # session-protected route, so log the admin back in first.
    _admin(api_client)
    resp = api_client.delete(f"/api/auth/api-keys/{prefix}")
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True}
    api_client.post("/api/auth/logout")
    me3 = api_client.get("/api/auth/me", headers={"Authorization": f"Bearer {key}"})
    assert me3.status_code == 401


def test_api_key_is_per_user(api_client) -> None:
    _admin(api_client)
    resp = api_client.post("/api/auth/api-keys", json={"name": "admin-key"})
    admin_key = resp.get_json()["key"]
    api_client.post("/api/auth/logout")

    _create_user(api_client, "bob")

    # bob's key is valid only for bob, not for the admin account.
    resp = login(api_client, username="bob", password="pass-123")
    assert resp.status_code == 200
    resp = api_client.post("/api/auth/api-keys", json={"name": "bob-key"})
    bob_key = resp.get_json()["key"]
    api_client.post("/api/auth/logout")

    assert api_client.get("/api/auth/me", headers={"X-API-Key": bob_key}).get_json()[
        "user"
    ]["username"] == "bob"
    # The admin key still maps to the admin account (keys are owner-bound).
    admin_me = api_client.get("/api/auth/me", headers={"X-API-Key": admin_key})
    assert admin_me.status_code == 200
    assert admin_me.get_json()["user"]["username"] == "admin"


# --------------------------------------------------------------------------- #
# Admin user management (D6)
# --------------------------------------------------------------------------- #
def test_admin_create_list_login_delete(api_client) -> None:
    _admin(api_client)

    resp = api_client.post(
        "/api/admin/users",
        json={"username": "alice", "password": "alice-pass", "import_dir": "/tmp/alice"},
    )
    assert resp.status_code == 201
    body = resp.get_json()["user"]
    assert body["username"] == "alice"
    assert body["is_admin"] is False
    assert body["import_dir"] == "/tmp/alice"

    resp = api_client.get("/api/admin/users")
    assert resp.status_code == 200
    users = {user["username"]: user for user in resp.get_json()["users"]}
    assert set(users) == {"admin", "alice"}
    assert users["admin"]["is_admin"] is True

    # The new user can log in.
    api_client.post("/api/auth/logout")
    resp = login(api_client, username="alice", password="alice-pass")
    assert resp.status_code == 200
    assert resp.get_json()["user"]["is_admin"] is False

    # Admin deletes the user again (owned rows cascade, FK-safe order).
    _admin(api_client)
    resp = api_client.delete("/api/admin/users/alice")
    assert resp.status_code == 200
    resp = api_client.delete("/api/admin/users/alice")
    assert resp.status_code == 404
    assert resp.get_json()["type"] == "not_found"


def test_admin_delete_last_admin_is_rejected(api_client) -> None:
    """The last admin cannot be deleted (would lock out user management)."""
    _admin(api_client)
    resp = api_client.delete("/api/admin/users/admin")
    assert resp.status_code == 409
    assert resp.get_json()["type"] == "conflict"

    # A second admin unblocks deletion while keeping one admin.
    resp = api_client.post(
        "/api/admin/users",
        json={"username": "second", "password": "x", "is_admin": True},
    )
    assert resp.status_code == 201
    resp = api_client.delete("/api/admin/users/admin")
    assert resp.status_code == 200


def test_admin_create_duplicate_409(api_client) -> None:
    _admin(api_client)
    resp = api_client.post(
        "/api/admin/users", json={"username": "dup", "password": "x"}
    )
    assert resp.status_code == 201
    resp = api_client.post(
        "/api/admin/users", json={"username": "dup", "password": "y"}
    )
    assert resp.status_code == 409
    assert resp.get_json()["type"] == "conflict"


def test_admin_api_forbidden_for_non_admin(api_client) -> None:
    _create_user(api_client, "bob")
    resp = login(api_client, username="bob", password="pass-123")
    assert resp.status_code == 200

    resp = api_client.get("/api/admin/users")
    assert resp.status_code == 403
    assert resp.get_json()["type"] == "forbidden"

    resp = api_client.post(
        "/api/admin/users", json={"username": "hax", "password": "x"}
    )
    assert resp.status_code == 403


# --------------------------------------------------------------------------- #
# D6 isolation on the data routes
# --------------------------------------------------------------------------- #
def test_data_routes_require_auth(api_client) -> None:
    assert api_client.get("/api/records").status_code == 401
    assert api_client.get("/api/records/2026-08-04").status_code == 401
    assert api_client.get("/api/records?user=admin").status_code == 401
    assert api_client.get("/api/import/jobs").status_code == 401
    assert api_client.get("/api/users").status_code == 401
    assert api_client.get("/users").status_code == 401  # legacy
    assert (
        api_client.get("/moods?user=default").status_code == 401
    )  # legacy
    resp = api_client.post("/api/import", data={"file": (io.BytesIO(b"x"), "x.csv")})
    assert resp.status_code == 401


def test_columns_scales_public_without_user(api_client) -> None:
    """Columns/scales remain public WITHOUT ?user= (Fase 1 contract)."""
    assert api_client.get("/api/columns").status_code == 200
    assert api_client.get("/api/scales").status_code == 200


def test_columns_scales_user_requires_auth(api_client) -> None:
    """A ?user= override is a per-user read => authenticated (401 anonymous)."""
    resp = api_client.get("/api/columns?user=alice")
    assert resp.status_code == 401
    resp = api_client.get("/api/scales?user=alice")
    assert resp.status_code == 401


def test_non_admin_cannot_read_other_user_records(
    api_client, mini_csv, config_sample
) -> None:
    _admin(api_client)
    # Seed 5 records into "alice" as admin (multipart upload also covers R7a).
    with mini_csv.open("rb") as fh:
        resp = api_client.post(
            "/api/import",
            data={"user": "alice", "file": (fh, mini_csv.name)},
            content_type="multipart/form-data",
        )
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["job"]["user"] == "alice"
    api_client.post("/api/auth/logout")

    _create_user(api_client, "bob")
    resp = login(api_client, username="bob", password="pass-123")
    assert resp.status_code == 200

    # bob cannot read alice's record.
    resp = api_client.get("/api/records?user=alice")
    assert resp.status_code == 403
    assert resp.get_json()["type"] == "forbidden"
    # bob sees only his own (empty) space by default.
    resp = api_client.get("/api/records")
    assert resp.status_code == 200
    assert resp.get_json()["records"] == []

    # bob's own ?user=bob is allowed.
    resp = api_client.get("/api/records?user=bob")
    assert resp.status_code == 200


def test_admin_can_read_any_user_records(api_client, mini_csv) -> None:
    _admin(api_client)
    with mini_csv.open("rb") as fh:
        resp = api_client.post(
            "/api/import",
            data={"user": "alice", "file": (fh, mini_csv.name)},
            content_type="multipart/form-data",
        )
    assert resp.status_code == 200

    resp = api_client.get("/api/records?user=alice")
    assert resp.status_code == 200
    assert len(resp.get_json()["records"]) == 5


def test_users_endpoint_isolation(api_client) -> None:
    _create_user(api_client, "bob")

    # Admin sees everyone.
    _admin(api_client)
    resp = api_client.get("/api/users")
    assert resp.status_code == 200
    users = {user["username"]: user for user in resp.get_json()["users"]}
    assert set(users) == {"admin", "bob"}

    # Non-admin sees only their own identity.
    api_client.post("/api/auth/logout")
    login(api_client, username="bob", password="pass-123")
    resp = api_client.get("/api/users")
    assert resp.status_code == 200
    users = resp.get_json()["users"]
    assert len(users) == 1 and users[0]["username"] == "bob"


def test_legacy_users_listed_for_admin(api_client) -> None:
    """Legacy /users honours D6: admin sees the grouped record counts."""
    _admin(api_client)
    resp = api_client.get("/users")
    assert resp.status_code == 200
    assert resp.get_json() == [{"user": "admin", "records": 0}]
