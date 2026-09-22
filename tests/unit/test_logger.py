"""Unit tests for daylio_api.logger (Task 0.4)."""

from __future__ import annotations

import logging

from daylio_api.logger import capture, configure, get_logger, log_context, release


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
