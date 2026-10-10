# Daylio API Server — dev/ops scripts

This directory contains standalone dev/ops utilities that are **not** part of
the runtime package (`src/daylio_api/`) and are **not** copied into the Docker
image (see `.dockerignore`). Every script is a plain-stdlib CLI with a
`MAINTENANCE NOTES` docblock, runnable both locally and inside the CI job
containers.

## `bump_version.py` — automatic `version.ini` bump (FASE 7, Task 7.3)

Increments `version.ini` to the next development version. Called by the
`release` job of `.gitea/workflows/ci.yml` right after a Gitea release is
published, so the next cycle always starts from a fresh `-SNAPSHOT` version
(flatpress pattern, WORKPLAN D9/D15b).

| Invocation | Effect |
| --- | --- |
| `python scripts/bump_version.py --dry-run` | **Read-only** (default): prints the next version, touches nothing |
| `python scripts/bump_version.py` | Same as `--dry-run` (safe default when no mutation flag is given) |
| `python scripts/bump_version.py --write` | Rewrites `version.ini` with a trailing newline |
| `python scripts/bump_version.py --commit --push --branch main` | Full CI action: write + `git add/commit` + `git push` |

Options:

- `--file PATH` — version file to bump (default `<repo>/version.ini`);
- `--part patch|minor|major` — component to increment (default `patch`:
  `2.0.0-SNAPSHOT` → `2.0.1-SNAPSHOT`, the WORKPLAN example);
- `--message TEXT` — custom bump commit message;
- `--branch NAME` — branch used by `--push` (default: the current branch).

Contract details:

- the `-SNAPSHOT` qualifier is always re-applied, so releasing a plain
  `2.1.0` still yields `2.1.1-SNAPSHOT`;
- a leading `v` is tolerated on read and never written back;
- a malformed `version.ini` is a hard error (exit code 1) with a clear message,
  so a broken version file can never silently produce a wrong tag;
- the new version is printed as `bump_version=<value>` and, when
  `GITHUB_OUTPUT` is set, appended to it for reuse by the workflow steps;
- the bump commit produced in CI carries the `[skip-ci]` token, so the bump
  push does not re-trigger lint/test (and cannot start a new build).

## Planned scripts (phases 3-5)

- `seed_admin.py` — create the initial admin user (Phase 5, delivered as an
  API route; a CLI wrapper is optional).
- `dump.py` / `import_cli.py` — operational DB/import helpers (Phase 3-4).
