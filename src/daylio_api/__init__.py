"""Daylio API Server — multi-user Daylio import server.

Package version is sourced from the repository ``version.ini`` file at the
project root (e.g. ``2.0.0-SNAPSHOT``), with a static fallback so that the
package is importable even when the file is missing (e.g. installed wheels).
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["__version__", "VERSION"]

#: Static fallback used only when ``version.ini`` cannot be read.
_DEFAULT_VERSION = "2.0.0"


def _read_version() -> str:
    """Read version.ini from the repository root (two levels above this file).

    MAINTENANCE NOTES:
        - ``src/daylio_api/__init__.py`` -> parents[0]  = src/daylio_api
        - parent of that -> parents[1] = src, parents[2] = project root.
    """
    version_ini = Path(__file__).resolve().parents[2] / "version.ini"
    try:
        raw = version_ini.read_text(encoding="utf-8").strip()
    except OSError:
        return _DEFAULT_VERSION
    return raw if raw else _DEFAULT_VERSION


__version__ = _read_version()
VERSION = __version__
