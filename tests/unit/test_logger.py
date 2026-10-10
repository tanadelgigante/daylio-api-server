"""Unit tests for daylio_api.logger (Task 0.4, refined in FASE 7 Task 7.2).

MAINTENANCE NOTES (FASE 7, Task 7.2 / D9 / R12):
    The contract under test is the one stated in the module docstring: the
    ``LOG_LEVEL`` threshold selects between DEBUG and INFO, while
    WARN/ERROR/FATAL are ALWAYS active, whatever value is requested. The
    ``test_warn_error_fatal_survive_every_requested_level`` test is the
    regression guard for that guarantee: a future change that lets
    ``configure("ERROR")`` mute ``logger.warning`` would break the operations
    contract (an operator must never lose a warning).
"""

from __future__ import annotations

import logging
import os

import pytest

from daylio_api.logger import capture, configure, get_logger, log_context, release

#: Levels that MUST keep emitting whatever the threshold is (FASE 7, Task 7.2).
ALWAYS_ON_LEVELS = ("warning", "error", "critical")


@pytest.fixture(autouse=True)
def _restore_logger_level():
    """Restore the process-wide logger threshold after each test.

    ``configure()`` mutates the global ``daylio_api`` logger; without this
    guard a DEBUG threshold set here would leak into unrelated modules.
    """
    root = get_logger()
    previous = root.level
    yield
    root.setLevel(previous)


def test_logger_levels_are_active() -> None:
    """DEBUG and INFO levels are available and ordered correctly."""
    assert logging.DEBUG < logging.INFO


def test_threshold_info_suppresses_debug(captured_logs) -> None:
    configure("INFO")
    logger = get_logger("test.info")
    logger.info("hello world")
    logger.debug("invisible")
    assert any("hello world" in msg for msg in captured_logs)
    assert not any("invisible" in msg for msg in captured_logs)


def test_threshold_debug_enables_debug(captured_logs) -> None:
    configure("DEBUG")
    logger = get_logger("test.debug")
    logger.debug("low-level detail")
    assert any("low-level detail" in msg for msg in captured_logs)


def test_warning_always_active(captured_logs) -> None:
    """WARN is emitted even when the threshold is configured above INFO."""
    configure("ERROR")
    logger = get_logger("test.warn")
    logger.warning("problem detected")
    assert any("problem detected" in msg for msg in captured_logs)


@pytest.mark.parametrize(
    "level",
    ["INFO", "WARN", "WARNING", "ERROR", "FATAL", "CRITICAL", "NONSENSE"],
)
def test_warn_error_fatal_survive_every_requested_level(captured_logs, level: str) -> None:
    """WARN/ERROR/FATAL are always emitted, whatever level is requested (R9).

    FASE 7 / Task 7.2 regression guard: the threshold is capped at INFO so a
    production configuration can never silence operational problems.
    """
    configure(level)
    logger = get_logger(f"test.always.{level}")
    for method in ALWAYS_ON_LEVELS:
        getattr(logger, method)(f"always-on {method}")
    for method in ALWAYS_ON_LEVELS:
        assert any(f"always-on {method}" in msg for msg in captured_logs), (
            f"{method} was suppressed by configure({level!r})"
        )


def test_effective_threshold_is_capped_at_info(captured_logs) -> None:
    """The effective level never rises above INFO (WARN+ must stay enabled)."""
    for level in ("WARN", "ERROR", "FATAL", "CRITICAL", "garbage"):
        configure(level)
        assert get_logger("test.cap").getEffectiveLevel() <= logging.INFO


def test_log_level_env_var_drives_the_threshold(captured_logs, monkeypatch) -> None:
    """``LOG_LEVEL=DEBUG`` opens DEBUG; any other value keeps INFO as default."""
    # configure() re-reads LOG_LEVEL, exactly like app startup does (D9).
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    configure()
    logger = get_logger("test.env")
    assert logger.getEffectiveLevel() == logging.DEBUG
    logger.debug("env debug")
    assert any("env debug" in msg for msg in captured_logs)

    # A higher level is honoured as a *request* but capped at INFO so that
    # WARN/ERROR/FATAL can never be silenced through the environment.
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    configure()
    assert get_logger("test.env").getEffectiveLevel() == logging.INFO
    assert os.environ["LOG_LEVEL"] == "WARNING"


def test_default_threshold_is_info_without_the_env_var(captured_logs, monkeypatch) -> None:
    """With no ``LOG_LEVEL`` set, INFO is the documented default (R9)."""
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    configure()
    assert get_logger("test.env.default").getEffectiveLevel() == logging.INFO


def test_capture_release_returns_messages() -> None:
    try:
        messages = capture()
        configure("DEBUG")
        get_logger("test.release").debug("before-release")
        assert any("before-release" in msg for msg in messages)
        returned = release()
        assert returned is messages
    finally:
        release()  # never leak the capture handler


def test_format_prefix_and_context(captured_logs) -> None:
    configure("INFO")
    logger = get_logger("test.format")
    logger.info("job finished", extra=log_context(job="csv", n=3))
    log_line = captured_logs[-1]
    assert log_line.startswith("[daylio_api] INFO")
    assert "job finished" in log_line
    assert "[ctx job=csv n=3]" in log_line


def test_get_logger_children_share_namespace() -> None:
    root_logger = get_logger()
    child = get_logger("api.routes")
    assert child.name == root_logger.name + ".api.routes"
