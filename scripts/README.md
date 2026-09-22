# Daylio API Server — dev/ops scripts

This directory contains standalone dev/ops utilities that are **not** part of
the runtime package (`src/daylio_api/`). Scripts are added in the relevant
phases, e.g.:

- `seed_admin.py` — create the initial admin user (Phase 5).
- `dump.py` / `import_cli.py` — operational DB/import helpers (Phase 3-4).

Phase 0 ships the directory as a placeholder only.