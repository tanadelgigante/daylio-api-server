"""Unit tests for the FASE 7 live-InfluxDB gate in ``tests/conftest.py`` (D18/R16).

MAINTENANCE NOTES (FASE 7, Task 7.1 — D18/R16):
    ``tests.conftest.live_influx_requested`` is the single function that decides
    whether the ``@pytest.mark.influx`` tests talk to a REAL InfluxDB 2.x or are
    skipped. The CI ``test`` job and the ``live_influx`` fixture both delegate to
    it, so its branch logic is configuration that deserves its own guard:

      - ``CI_SKIP_INFLUX=1`` forces the skip (exported by CI when the repo var
        ``vars.INFLUX_INSTANCE`` is unset);
      - an endpoint must be reachable via ``INFLUX_URL`` or the CI-shortcut
        ``INFLUX_INSTANCE`` (``"<ip>:<port>"`` -> ``http://<ip>:<port>``, D18);
      - the ``env:INFLUX_URL`` YAML placeholder is NOT a real endpoint;
      - an API token must be present in ``INFLUX_TOKEN``.

    Anything that changes these rules must update this module in the same
    commit (and the WORKPLAN §8.4 matrix row 8).
"""

from __future__ import annotations

import pytest

from tests.conftest import live_influx_requested

URL = "http://192.168.188.120:8120"
TOKEN = "a-valid-token"


@pytest.fixture
def clean_env(monkeypatch):
    """Remove every Influx env var so each test starts from a blank slate."""
    for name in ("INFLUX_URL", "INFLUX_INSTANCE", "INFLUX_TOKEN", "CI_SKIP_INFLUX"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_requires_both_url_and_token(clean_env) -> None:
    # Nothing configured -> skipped.
    assert live_influx_requested() is False
    # A URL alone is not enough.
    clean_env.setenv("INFLUX_URL", URL)
    assert live_influx_requested() is False
    # A token alone is not enough.
    clean_env.delenv("INFLUX_URL")
    clean_env.setenv("INFLUX_TOKEN", TOKEN)
    assert live_influx_requested() is False
    # Both together enable the live suite.
    clean_env.setenv("INFLUX_URL", URL)
    assert live_influx_requested() is True


def test_instance_shortcut_expands_to_an_http_url(clean_env) -> None:
    # D18: the repo var holds "<ip>:<port>" and the gate builds the URL.
    monkeypatch_set(clean_env, {"INFLUX_INSTANCE": "192.168.188.120:8120", "INFLUX_TOKEN": TOKEN})
    assert live_influx_requested() is True


def test_env_placeholder_does_not_count_as_an_endpoint(clean_env) -> None:
    # A YAML `env:INFLUX_URL` that was never substituted is not a live URL.
    monkeypatch_set(clean_env, {"INFLUX_URL": "env:INFLUX_URL", "INFLUX_TOKEN": TOKEN})
    assert live_influx_requested() is False


def test_ci_skip_influx_forces_the_skip_even_with_a_valid_endpoint(clean_env) -> None:
    # Exporting CI_SKIP_INFLUX=1 (CI job with vars.INFLUX_INSTANCE unset) must
    # win over a stale INFLUX_URL/INFLUX_TOKEN left in the runner shell.
    monkeypatch_set(clean_env, {"INFLUX_URL": URL, "INFLUX_TOKEN": TOKEN, "CI_SKIP_INFLUX": "1"})
    assert live_influx_requested() is False
    for truthy in ("true", "yes"):
        monkeypatch_set(clean_env, {"CI_SKIP_INFLUX": truthy})
        assert live_influx_requested() is False


def monkeypatch_set(clean_env, values: dict[str, str]) -> None:
    """Helper: set env vars through the same monkeypatch fixture."""
    for name, value in values.items():
        clean_env.setenv(name, value)
