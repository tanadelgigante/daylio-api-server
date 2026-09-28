"""Shared API test helpers (Fase 5, D6).

Every ``create_app()`` bootstraps the admin user from the environment
(``ADMIN_USERNAME``/``ADMIN_PASSWORD``; forced by the autouse ``_phase5_admin_env``
fixture in conftest). These helpers make the test flows readable across the
integration suites.
"""

from __future__ import annotations

from typing import Any


def login(
    client: Any, username: str = "admin", password: str = "admin-pass"
) -> Any:
    """Log the test client in and return the ``/api/auth/login`` response."""
    return client.post(
        "/api/auth/login",
        json={"username": username, "password": password},
    )


def login_assert_ok(client: Any, username: str = "admin", password: str = "admin-pass") -> dict:
    """Log in and assert success; return the ``user`` payload."""
    resp = login(client, username=username, password=password)
    assert resp.status_code == 200, f"login failed: {resp.get_json()}"
    data = resp.get_json()
    assert data["user"]["username"] == username
    return data["user"]
