"""Structured, level-aware application logging.

=============================== MAINTENANCE NOTES ===============================
Module   : src/daylio_api/logger.py
Scope    : Fase 0 (Task 0.4) — level-aware logger with a ``LOG_LEVEL``
           threshold (default ``INFO``) and a capture/release mechanism used by
           the test-suite. Modeled after PublishArticleLogger.php / WORKPLAN R12.

Format   : ``[daylio_api] <LEVEL> <message> [ctx k=v]``
           Context keys are rendered for every ``extra`` key passed to the log
           call, e.g. ``logger.info("job done", extra={"job": "csv", "n": 3})``
           produces ``[daylio_api] INFO job done [ctx job=csv n=3]``.

Levels   : DEBUG < INFO < WARN < ERROR < FATAL. ``WARN/ERROR/FATAL`` are ALWAYS
           active: the effective threshold is at most ``INFO`` even when the
           environment/config sets a higher value, so operational problems are
           never silently swallowed (risk R8/R15) — while DEBUG stays opt-in.

Capture  : ``capture()`` installs an in-memory handler and returns the list of
            formatted messages; ``release()`` removes it. Used by tests
            (conftest fixture ``captured_logs``). Capture is thread-unsafe by
            design (single GIL) and no-op when already captured.

FASE 7 (Task 7.2, D9) — the WARN/ERROR/FATAL guarantee is enforced in ONE place,
``_threshold_level()``: it is the only function that maps a requested level name
to a numeric threshold, and it always returns DEBUG or INFO. ``_threshold_from_env``
(boot) and ``configure`` (runtime) both delegate to it, so the two paths cannot
drift apart and a production ``LOG_LEVEL`` can never silence a warning.
The regression guard lives in ``tests/unit/test_logger.py``
(``test_warn_error_fatal_survive_every_requested_level``,
``test_effective_threshold_is_capped_at_info``,
``test_log_level_env_var_drives_the_threshold``): a change that lets
``configure("ERROR")`` mute ``logger.warning`` fails the suite.

Changing the log format? Update ``DaylioFormatter`` and
``test_format_prefix_and_context`` together — the ``[ctx k=v]`` tail is a
public contract (R12, it mirrors PublishArticleLogger.php).
================================================================================
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

#: Public level names accepted by the WORKPLAN (WARN/FATAL aliases mapped to
#: the standard Python logging level names WARNING/CRITICAL).
_LEVEL_ALIASES = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARN": logging.WARNING,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "FATAL": logging.CRITICAL,
    "CRITICAL": logging.CRITICAL,
}

#: Package name shown in every log line.
_PREFIX = "daylio_api"

#: Root logger name owned by this module.
_ROOT_NAME = "daylio_api"


class DaylioFormatter(logging.Formatter):
    """Formatter that appends ``[ctx k=v]`` built from the record's ``extra``."""

    def __init__(self) -> None:
        super().__init__(fmt="")

    def format(self, record: logging.LogRecord) -> str:
        base = f"[{_PREFIX}] {record.levelname} {record.getMessage()}"
        ctx_keys = [
            key
            for key in record.__dict__
            if key
            not in logging.LogRecord("", 0, "", 0, "", (), None).__dict__
            and key != "message"
        ]
        if ctx_keys:
            ctx = " ".join(f"{key}={record.__dict__[key]}" for key in sorted(ctx_keys))
            return f"{base} [ctx {ctx}]"
        return base


class _CaptureHandler(logging.Handler):
    """Handler that stores formatted messages instead of printing them."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.setFormatter(DaylioFormatter())
        self.records: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.records.append(self.format(record))
        except Exception:  # pragma: no cover - defensive, capture must not crash
            self.records.append(f"[{_PREFIX}] {record.levelname} {record.getMessage()}")


def _root_logger() -> logging.Logger:
    """Return the module's root logger, lazily configured."""
    logger = logging.getLogger(_ROOT_NAME)
    logger.propagate = False
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(DaylioFormatter())
        logger.addHandler(handler)
        logger.setLevel(_threshold_from_env())
    return logger


def _threshold_level(level_name: str | None) -> int:
    """Map a requested level name to the effective threshold (never above INFO).

    This is the SINGLE place implementing the "WARN/ERROR/FATAL are always
    active" contract (module docstring, WORKPLAN D9/R12): only ``DEBUG`` can
    open the gate, every other value — including a bogus one — resolves to
    ``INFO``. Both :func:`_threshold_from_env` (boot) and :func:`configure`
    (runtime) delegate here so they cannot drift apart.

    Args:
        level_name: case-insensitive level name, or None/empty for the default.

    Returns:
        Either ``logging.DEBUG`` or ``logging.INFO``.
    """
    requested = _LEVEL_ALIASES.get(str(level_name or "").strip().upper(), logging.INFO)
    return logging.DEBUG if requested == logging.DEBUG else logging.INFO


def _threshold_from_env() -> int:
    """Compute the boot-time logging threshold from the ``LOG_LEVEL`` env var.

    MAINTENANCE NOTES:
        - Reads ``LOG_LEVEL`` (default ``INFO``) and delegates the mapping to
          :func:`_threshold_level`, which caps the result at INFO so that
          WARN/ERROR/FATAL are always emitted.
    """
    return _threshold_level(os.environ.get("LOG_LEVEL", "INFO"))


def configure(level: str | None = None) -> None:
    """Set the logging threshold, overriding the current root handler.

    Args:
        level: one of DEBUG/INFO/WARN/WARNING/ERROR/FATAL/CRITICAL. When None
            the ``LOG_LEVEL`` environment variable is used (default INFO).
            WARN/ERROR/FATAL remain always-active regardless of this value:
            the effective threshold is capped at INFO by
            :func:`_threshold_level`.
    """
    if level is None:
        level = os.environ.get("LOG_LEVEL", "INFO")
    _root_logger().setLevel(_threshold_level(level))


def get_logger(name: str | None = None) -> logging.Logger:
    """Return a (child) logger under the ``daylio_api`` namespace.

    Args:
        name: child name, e.g. ``"config"`` -> ``daylio_api.config``.
            Defaults to the root ``daylio_api`` logger.
    """
    root = _root_logger()
    if name is None or name == _ROOT_NAME:
        return root
    child = root.getChild(name.lstrip("."))
    # Children inherit the effective level/handlers from the root configure().
    return child


def capture() -> list[str]:
    """Install an in-memory capture handler and return its message list.

    After ``capture()`` every formatted message emitted by ``daylio_api``
    loggers at or above the effective threshold is appended in order. Call
    ``release()`` to restore the normal stderr handler. Calling capture twice
    is safe: the first capture handler wins and the same list is returned.
    """
    root = _root_logger()
    for handler in list(root.handlers):
        if isinstance(handler, _CaptureHandler):
            return handler.records
    root.addHandler(_CaptureHandler())
    return _last_capture()

def _last_capture() -> list[str]:
    """Return the records of the current capture handler, if any."""
    root = _root_logger()
    for handler in list(root.handlers):
        if isinstance(handler, _CaptureHandler):
            return handler.records
    return []


def release() -> list[str]:
    """Remove the capture handler (if any) and return the captured messages."""
    root = _root_logger()
    records = _last_capture()
    for handler in list(root.handlers):
        if isinstance(handler, _CaptureHandler):
            root.removeHandler(handler)
    return records


def log_context(**kwargs: Any) -> dict[str, Any]:
    """Convenience builder for ``extra`` context dictionaries."""
    return kwargs
