"""Unit tests for ``scripts/bump_version.py`` (FASE 7, Task 7.3, D9/D15b).

The version bump is executed by the CI ``release`` job, so the parsing and
increment rules are covered here with pure functions plus a CLI round-trip on a
temporary ``version.ini``. The real repository ``version.ini`` is never touched.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "bump_version.py"


def _load_module():
    """Import ``scripts/bump_version.py`` as a standalone module (no package)."""
    spec = importlib.util.spec_from_file_location("bump_version", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["bump_version"] = module
    spec.loader.exec_module(module)
    return module


bump_version = _load_module()


# --------------------------------------------------------------------------- #
# parse_version
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2.0.0-SNAPSHOT", (2, 0, 0, "SNAPSHOT")),
        ("2.1.0", (2, 1, 0, "")),
        ("v3.4.5", (3, 4, 5, "")),
        ("  2.0.0-SNAPSHOT\n", (2, 0, 0, "SNAPSHOT")),
        ("1.2.3-rc1", (1, 2, 3, "rc1")),
    ],
)
def test_parse_version_accepts_semantic_values(raw: str, expected) -> None:
    assert bump_version.parse_version(raw) == expected


@pytest.mark.parametrize("raw", ["", "nope", "2.0", "2.0.0.0", "x.y.z", "2.0.0-"])
def test_parse_version_rejects_malformed_values(raw: str) -> None:
    with pytest.raises(ValueError):
        bump_version.parse_version(raw)


# --------------------------------------------------------------------------- #
# bump
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("current", "part", "expected"),
    [
        # WORKPLAN/D9 example: 2.0.0-SNAPSHOT -> 2.0.1-SNAPSHOT
        ("2.0.0-SNAPSHOT", "patch", "2.0.1-SNAPSHOT"),
        ("2.0.9-SNAPSHOT", "patch", "2.0.10-SNAPSHOT"),
        ("2.0.0-SNAPSHOT", "minor", "2.1.0-SNAPSHOT"),
        ("2.0.0-SNAPSHOT", "major", "3.0.0-SNAPSHOT"),
        # A released (non -SNAPSHOT) file still yields the next -SNAPSHOT.
        ("2.1.0", "patch", "2.1.1-SNAPSHOT"),
        ("v1.9.9", "patch", "1.9.10-SNAPSHOT"),
    ],
)
def test_bump_increments_the_requested_component(current: str, part: str, expected: str) -> None:
    assert bump_version.bump(current, part) == expected


def test_bump_rejects_an_unknown_part() -> None:
    with pytest.raises(ValueError):
        bump_version.bump("2.0.0-SNAPSHOT", "epoch")


# --------------------------------------------------------------------------- #
# read/write round-trip and the CLI contract
# --------------------------------------------------------------------------- #
def test_read_version_rejects_a_malformed_file(tmp_path: Path) -> None:
    bad = tmp_path / "version.ini"
    bad.write_text("not-a-version\n", encoding="utf-8")
    with pytest.raises(ValueError):
        bump_version.read_version(bad)


def test_read_version_reports_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        bump_version.read_version(tmp_path / "absent.ini")


def test_dry_run_does_not_touch_the_file(tmp_path: Path, capsys) -> None:
    version_file = tmp_path / "version.ini"
    version_file.write_text("2.0.0-SNAPSHOT\n", encoding="utf-8")

    assert bump_version.main(["--file", str(version_file), "--dry-run"]) == 0

    assert version_file.read_text(encoding="utf-8") == "2.0.0-SNAPSHOT\n"
    out = capsys.readouterr().out
    assert "dry-run" in out
    assert "bump_version=2.0.1-SNAPSHOT" in out


def test_bare_invocation_is_read_only_by_default(tmp_path: Path) -> None:
    """No mutation flag -> nothing is written (safe default for local runs)."""
    version_file = tmp_path / "version.ini"
    version_file.write_text("2.0.0-SNAPSHOT\n", encoding="utf-8")

    assert bump_version.main(["--file", str(version_file)]) == 0
    assert version_file.read_text(encoding="utf-8") == "2.0.0-SNAPSHOT\n"


def test_write_flag_rewrites_the_file_with_a_trailing_newline(tmp_path: Path) -> None:
    version_file = tmp_path / "version.ini"
    version_file.write_text("2.0.0-SNAPSHOT", encoding="utf-8")  # no newline

    assert bump_version.main(["--file", str(version_file), "--write"]) == 0
    assert version_file.read_text(encoding="utf-8") == "2.0.1-SNAPSHOT\n"


def test_dry_run_writes_the_ci_output_when_requested(tmp_path: Path, monkeypatch) -> None:
    """In a runner the new version is also exported to ``$GITHUB_OUTPUT``."""
    version_file = tmp_path / "version.ini"
    version_file.write_text("2.0.0-SNAPSHOT\n", encoding="utf-8")
    github_output = tmp_path / "github_output"
    github_output.write_text("", encoding="utf-8")
    monkeypatch.setenv("GITHUB_OUTPUT", str(github_output))

    assert bump_version.main(["--file", str(version_file), "--dry-run"]) == 0
    assert "bump_version=2.0.1-SNAPSHOT" in github_output.read_text(encoding="utf-8")


def test_main_reports_a_malformed_file_with_exit_code_1(tmp_path: Path, capsys) -> None:
    version_file = tmp_path / "version.ini"
    version_file.write_text("oops\n", encoding="utf-8")

    assert bump_version.main(["--file", str(version_file), "--dry-run"]) == 1
    assert "ERROR" in capsys.readouterr().err


def test_repository_version_file_is_a_supported_version() -> None:
    """Guard: the shipped version.ini must parse (CI reads it for the tag)."""
    current = bump_version.read_version(PROJECT_ROOT / "version.ini")
    assert bump_version.bump(current, "patch").endswith("-SNAPSHOT")
