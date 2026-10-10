#!/usr/bin/env python3
"""Bump ``version.ini`` to the next development version (WORKPLAN FASE 7 / Task 7.3).

============================== MAINTENANCE NOTES ===============================
Module   : scripts/bump_version.py
Scope    : FASE 7 (Task 7.3, D9/D15b) — post-release automatic version bump of
            ``version.ini``, following the flatpress-publish-article-plugin
            release pattern (its "Bump version to next SNAPSHOT" step). The
            workflow job ``release`` (.gitea/workflows/ci.yml) calls this script
            right after the Gitea release is created, so the next development
            cycle always starts from a fresh ``-SNAPSHOT`` version.

Contract : ``version.ini`` holds a single line, e.g. ``2.0.0-SNAPSHOT``.
           - default bump is ``patch`` (flatpress pattern, and the WORKPLAN
             example ``2.0.0-SNAPSHOT`` -> ``2.0.1-SNAPSHOT``);
           - ``--part minor``/``--part major`` are available for a deliberate
             minor/major release;
           - the ``-SNAPSHOT`` qualifier is re-applied to the bumped version,
             so releasing a non-snapshot ``version.ini`` (e.g. ``2.1.0``)
             yields ``2.1.1-SNAPSHOT``;
           - a leading ``v`` is tolerated on read and never written back.

Usage    : ``python scripts/bump_version.py --dry-run``            (safe, no writes)
           ``python scripts/bump_version.py``                      (rewrite the file)
           ``python scripts/bump_version.py --commit --push``       (CI post-release)

Exit codes: ``0`` success (including "nothing to bump"), ``1`` usage/IO error.
================================================================================
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

#: ``version.ini`` relative to the repository root (script lives in ``scripts/``).
DEFAULT_VERSION_FILE = Path(__file__).resolve().parent.parent / "version.ini"

#: ``MAJOR.MINOR.PATCH`` with an optional ``-SNAPSHOT``/``-rc1`` qualifier.
#: The qualifier must start with a letter so ``2.0.0.0`` is rejected as invalid.
_VERSION_RE = re.compile(
    r"^v?(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)"
    r"(?P<qualifier>-[0-9A-Za-z][0-9A-Za-z.\-]*)?$"
)

#: Qualifier used for unreleased development versions (flatpress pattern).
SNAPSHOT = "SNAPSHOT"

#: Default bump: the next patch release, matching the WORKPLAN example.
DEFAULT_PART = "patch"


def parse_version(raw: str) -> tuple[int, int, int, str]:
    """Parse a ``version.ini`` value into ``(major, minor, patch, qualifier)``.

    Args:
        raw: the trimmed file content, optionally prefixed with ``v``.

    Returns:
        The three numeric components plus the qualifier WITHOUT its separator
        (e.g. ``2.0.0-SNAPSHOT`` -> ``(2, 0, 0, "SNAPSHOT")``, ``2.1.0`` ->
        ``(2, 1, 0, "")``).

    Raises:
        ValueError: when the value is not a semantic ``X.Y.Z[-qualifier]``.
    """
    match = _VERSION_RE.match(raw.strip())
    if match is None:
        raise ValueError(
            f"invalid version {raw!r}: expected MAJOR.MINOR.PATCH[-QUALIFIER] (e.g. 2.0.0-SNAPSHOT)"
        )
    return (
        int(match.group("major")),
        int(match.group("minor")),
        int(match.group("patch")),
        (match.group("qualifier") or "")[1:],
    )

def bump(raw: str, part: str = DEFAULT_PART) -> str:
    """Return the next ``-SNAPSHOT`` development version after ``raw``.

    Args:
        raw: current ``version.ini`` content (trimmed by the caller is fine).
        part: ``patch`` (default), ``minor`` or ``major``.

    Raises:
        ValueError: on a malformed version or an unknown ``part``.
    """
    major, minor, patch, _qualifier = parse_version(raw)
    if part == "patch":
        return f"{major}.{minor}.{patch + 1}-{SNAPSHOT}"
    if part == "minor":
        return f"{major}.{minor + 1}.0-{SNAPSHOT}"
    if part == "major":
        return f"{major + 1}.0.0-{SNAPSHOT}"
    raise ValueError(f"unknown part {part!r}: expected one of patch|minor|major")


def read_version(path: Path) -> str:
    """Read and validate ``version.ini`` (returns the trimmed content)."""
    if not path.is_file():
        raise FileNotFoundError(f"version file not found: {path}")
    content = path.read_text(encoding="utf-8").strip()
    parse_version(content)  # fail fast on a malformed file
    return content


def write_version(path: Path, version: str) -> None:
    """Write ``version`` (with a trailing newline) to ``path``."""
    path.write_text(f"{version}\n", encoding="utf-8")


def _run_git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run a git command with a fixed argv (never through a shell).

    Args:
        *args: git sub-command and arguments.
        check: when True (default) a non-zero exit raises ``RuntimeError`` with
            the captured stderr; when False the result is returned as-is (used
            for ``git diff --quiet``, whose exit code IS the answer).
    """
    result = subprocess.run(
        ["git", *args], capture_output=True, text=True, check=False
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed ({result.returncode}): "
            f"{(result.stderr or result.stdout).strip()}"
        )
    return result


def _emit(version: str, current: str, part: str) -> None:
    """Print a human line plus the machine-readable ``bump_version=`` output.

    The ``<name>=<value>`` line is appended to ``$GITHUB_OUTPUT`` when running
    inside a CI runner, so the workflow can reuse the new version.
    """
    print(f"version.ini: {current} -> {version} (bump={part})")
    print(f"bump_version={version}")

    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as handle:
            handle.write(f"bump_version={version}\n")


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: compute, optionally write, commit and push the bump."""
    parser = argparse.ArgumentParser(
        description="Bump version.ini to the next -SNAPSHOT version (CI post-release).",
    )
    parser.add_argument(
        "--file",
        type=Path,
        default=DEFAULT_VERSION_FILE,
        help="version file to bump (default: <repo>/version.ini)",
    )
    parser.add_argument(
        "--part",
        choices=("patch", "minor", "major"),
        default=DEFAULT_PART,
        help="component to increment (default: patch)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the new version without touching the file (safe default)",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="write the new version into the version file",
    )
    parser.add_argument(
        "--commit",
        action="store_true",
        help="git add + commit the version file (implies --write)",
    )
    parser.add_argument(
        "--push",
        action="store_true",
        help="push the bump commit to --branch (implies --commit)",
    )
    parser.add_argument(
        "--branch",
        default="",
        help="branch to push the bump commit to (default: current branch)",
    )
    parser.add_argument(
        "--message",
        default="",
        help="bump commit message (default: 'Bump version to <new> after release <ref> [skip-ci]')",
    )
    args = parser.parse_args(argv)

    # --dry-run (or no mutation flag at all) is the safe default: it only prints.
    mutate = args.write or args.commit or args.push

    try:
        current = read_version(args.file)
        new_version = bump(current, args.part)
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if args.dry_run or not mutate:
        if args.dry_run:
            print(f"dry-run: would set {args.file} to {new_version} (current {current})")
        _emit(new_version, current, args.part)
        return 0

    try:
        write_version(args.file, new_version)
        _emit(new_version, current, args.part)

        if not (args.commit or args.push):
            return 0

        message = args.message or (
            f"Bump version to {new_version} after release [skip-ci]"
        )
        _run_git("add", str(args.file))
        # Nothing to do when version.ini already holds the target value.
        if _run_git("diff", "--cached", "--quiet", check=False).returncode == 0:
            print(f"version.ini already at {new_version}; nothing to commit")
            return 0
        _run_git("config", "user.name", "CI bot")
        _run_git("config", "user.email", "ci-bot@localhost")
        _run_git("commit", "-m", message)
        if args.push:
            branch = args.branch or _run_git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
            _run_git("push", "origin", branch)
            print(f"pushed bump commit to {branch}")
    except (OSError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
