# Daylio API Server

Multi-user REST server for [Daylio](https://daylio.net/) journal data: it imports
CSV and `.daylio` exports, stores **one record per user per day**, and serves the
data through a parametric JSON API plus an analytics dashboard.

- **Version:** 2.0.0 (see `version.ini`)
- **Stack:** Python 3.12+, Flask 3, Waitress, SQLite (source of truth), InfluxDB 2.x (analytics), APScheduler
- **License:** MIT

## Features

- **Unified import** of two Daylio formats — CSV export and `.daylio` backup — through one normalizer.
- **One record per day:** duplicates are resolved by keeping the most recent entry, and re-imports update the existing row instead of inserting a second one.
- **Dual storage:** SQLite is the source of truth (records, configuration, users, import audit); InfluxDB 2.x stores one point per `(user, date)` and powers the dashboard.
- **Configurable model:** the mood scale, custom scales (text levels or numeric ranges) and record columns are configuration, not code. YAML files, never hardcoded.
- **Multi-user with isolation:** users are created by an admin; a user only ever sees their own data. Admin users can read and import on behalf of anyone.
- **Two authentication modes:** signed session cookie for the dashboard, API keys (`daylio_…`) for scripts and automation. Only the SHA-256 hash of a key is stored.
- **Analytics API:** five endpoints (mood over time, distribution, weekday × hour heatmap, scale trends, top activities) reading **only** from InfluxDB.
- **Automated imports:** optional APScheduler jobs scan a per-user folder on an interval and move processed files to `done/` or `failed/`.
- **Fully audited:** every import run (manual, folder scan, scheduled) leaves a row in the import history.

## Quick start (Docker Compose)

**Requirements:** Docker with the Compose plugin (this path needs nothing else).
Without Docker you need Python ≥ 3.12, a writable data directory, and an
InfluxDB 2.x instance for the dashboard — see
[docs/OPERATIONS.md](docs/OPERATIONS.md#requirements).

1. **Create the environment file** `.env` with the admin credentials and the
   InfluxDB connection. `ADMIN_PASSWORD` is mandatory; `SESSION_SECRET` must be
   set to a long random value in production.

   ```dotenv
   ADMIN_USERNAME=admin
   ADMIN_PASSWORD=change-me
   SESSION_SECRET=change-me-to-a-long-random-string
   INFLUX_URL=http://influxdb:8086
   INFLUX_TOKEN=                 # see step 2
   INFLUX_ORG=tanadelgigante
   INFLUX_BUCKET=daylio
   ```

   `docker-compose.yml` forwards a fixed set of variables to the container, and
   `SESSION_SECRET` is not one of them: to keep the dashboard session valid
   across restarts, add `- SESSION_SECRET=${SESSION_SECRET}` to its
   `environment:` list. Without it the app generates a random secret at every
   boot (logged as a warning) and only the login survives, not the session.

2. **Start the stack.** It runs the API on host port `3177` (container `5000`)
   and InfluxDB on host port `8120`.

   ```sh
   docker compose up -d --build
   docker compose logs -f daylio_api
   ```

   InfluxDB needs a **one-time onboarding** (create the org and the bucket named
   above), then an API token: open <http://localhost:8120> or use the `influx`
   CLI, generate a token and put it in `INFLUX_TOKEN` in `.env`. Until the
   token is set the API and imports keep working, but the dashboard and the
   `/api/analytics/*` endpoints answer `503` (InfluxDB is not available).

3. **Log in** and create a user (the bootstrap admin comes from
   `ADMIN_USERNAME`/`ADMIN_PASSWORD` and exists only on first boot).

   ```sh
   curl -c cookies.txt -X POST http://localhost:3177/api/auth/login \
     -H 'Content-Type: application/json' \
     -d '{"username": "admin", "password": "change-me"}'

   curl -b cookies.txt -X POST http://localhost:3177/api/admin/users \
     -H 'Content-Type: application/json' \
     -d '{"username": "alice", "password": "alice-pw", "import_dir": "/srv/daylio/alice"}'
   ```

4. **Import a sample export** shipped in `sample-data/`.

   ```sh
   curl -b cookies.txt -X POST http://localhost:3177/api/import \
     -F 'file=@sample-data/daylio_export_2026_09_22.csv' \
     -F 'user=alice'
   ```

   The response is an audit summary: `{"job": {"imported": 628, "skipped": 2, "errors": 0, "status": "completed", ...}}`.

5. **Read the data back** (admin session, or an API key — see
   [docs/API.md](docs/API.md)).

   ```sh
   curl -b cookies.txt 'http://localhost:3177/api/records?user=alice&start_date=2026-09-21&end_date=2026-09-21'
   ```

6. **Open the dashboard** at <http://localhost:3177/dashboard>.

## Configuration

Configuration is layered: the YAML file first, then environment variables on top.
Never put secrets in YAML — reference them as `env:VAR_NAME`.

| Variable | Default | Purpose |
| --- | --- | --- |
| `CONFIG_FILE` | `config/config.example.yml` | Path of the base configuration file |
| `USERS_CONFIG_FILE` | — | Optional per-user override file (see `config/users.example.yml`) |
| `DB_PATH` | `storage.sqlite_path` (`data/daylio.db`) | SQLite database file (source of truth) |
| `DATA_DIR` | `data` | Data directory |
| `INFLUX_URL` | from YAML | InfluxDB 2.x URL |
| `INFLUX_TOKEN` | from YAML | InfluxDB API token (secret) |
| `INFLUX_ORG` | `tanadelgigante` | InfluxDB organization |
| `INFLUX_BUCKET` | `daylio` | InfluxDB bucket |
| `INFLUX_INSTANCE` | — | `<host>:<port>` shorthand, expands to `http://<host>:<port>` (used by CI) |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | — | Bootstrap admin, created on first boot |
| `SESSION_SECRET` | random per boot | Secret for signed session cookies |
| `LOG_LEVEL` | `INFO` | `DEBUG`/`INFO` are optional; `WARNING`+ are always active |

The reference schema, fully commented, is
[`config/config.example.yml`](config/config.example.yml); per-user overrides are
in [`config/users.example.yml`](config/users.example.yml).

## API at a glance

| Endpoint | Auth | Purpose |
| --- | --- | --- |
| `POST /api/auth/login`, `POST /api/auth/logout`, `GET /api/auth/me` | – / session | Session login for the dashboard |
| `GET/POST /api/auth/api-keys`, `DELETE /api/auth/api-keys/<prefix>` | session | Create, list and revoke API keys |
| `GET /api/version` | none | Service name and version (health check) |
| `GET /api/columns`, `GET /api/scales` | optional | Configured columns, scales and mood map |
| `GET /api/records`, `GET /api/records/<full_date>` | session or API key | Daily records with filters, column selection, pagination |
| `POST /api/import`, `POST /api/import/folder` | session or API key | Upload a file, or scan the per-user import folder |
| `GET /api/import/jobs` | session or API key | Import history (audit trail) |
| `GET /api/users` | session or API key | Visible users (all for an admin, self otherwise) |
| `GET /api/analytics/*` (5 endpoints) | session or API key | Mood over time, distribution, heatmap, scale trends, top activities — **InfluxDB only** |
| `GET /dashboard` | session | HTML dashboard (reads analytics from InfluxDB) |
| `GET /users`, `GET /moods` | session or API key | Legacy v1 endpoints, kept for compatibility |

Authenticate every call with either the session cookie or an API key:

```sh
curl -H 'X-API-Key: daylio_<64 hex chars>' 'http://localhost:3177/api/records?limit=5'
# or
curl -H 'Authorization: Bearer daylio_<64 hex chars>' 'http://localhost:3177/api/records?limit=5'
```

A canonical record looks like this:

```json
{
  "mood": 2,
  "mood_label": "bene",
  "scales": {
    "Stress": { "value": 4, "label": "Alto", "levels_count": 5 },
    "Scala salute": { "value": 6, "label": "insufficiente", "levels_count": 8 }
  }
}
```

Errors are uniform: `{"error": "...", "type": "bad_request"}` with HTTP `400`
(invalid parameter), `401` (not authenticated), `403` (another user's data or
admin required), `404` (no such record), `409` (conflict) and `503` (InfluxDB
unavailable for analytics). All dates are `YYYY-MM-DD`.

Full reference, with request parameters and real response bodies:
**[docs/API.md](docs/API.md)**.

## Data model in one minute

- `mood` is an integer from **1 (best) to 5 (worst)**, with Italian labels by default (`alla grande`, `bene`, `così così`, `male`, `malissimo`); `0` means "label not resolved".
- `scala_salute` is an **ordered 8-level health scale** (`ottimo` → `terribile`), not a list of tags.
- Custom scales are either `text` (ordered named levels) or `number` (`min`/`max`).
- `full_date` is the only mandatory input column; `date` and `weekday` (Italian names) are derived.
- Uniqueness is **`(user, full_date)`**: one row per user per day, updated in place on re-import.
- InfluxDB holds one point per `(user, date)` in measurement `daylio_entry`, timestamped at `00:00:00Z`, written with a delete-then-write so stale fields never survive.

Details and full schemas: **[docs/DATA_MODEL.md](docs/DATA_MODEL.md)**.

## Project layout

```text
daylio-api-server/
├── src/daylio_api/          # runtime package (app factory, API, DB, importers, dashboard)
│   ├── app.py               # create_app() + lazy WSGI app (daylio_api.app:app)
│   ├── config.py            # YAML + env layered configuration and validation
│   ├── api/                 # records, config, import, legacy, auth, admin blueprints
│   ├── db/                  # sqlite_store, influx_store, migrations
│   ├── importers/           # CSV and .daylio parsers + shared normalizer
│   ├── dashboard/           # HTML dashboard + /api/analytics blueprints
│   ├── import_flow.py       # import/resync orchestration and audit
│   └── scheduler.py         # per-user periodic imports (APScheduler)
├── config/                  # config.example.yml, users.example.yml
├── sample-data/             # real CSV and .daylio exports used as fixtures
├── tests/                   # pytest suite (unit + integration)
├── docs/                    # technical documentation (see docs/README.md)
├── Dockerfile               # multi-stage build, Waitress runtime
├── docker-compose.yml       # app + influxdb
├── pyproject.toml           # metadata, dependencies, ruff/pytest config
└── version.ini              # single source of the version
```

## Development

```sh
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"

pytest                  # unit + integration suite
ruff check .            # lint
python -m daylio_api.app  # dev server on http://0.0.0.0:5000 (PORT to change)
```

Dependencies are locked in `requirements.lock` (pip-tools); the Docker build
installs only that file, and CI verifies it is still in sync with
`requirements.in`.

## Documentation

| Document | Contents |
| --- | --- |
| [docs/README.md](docs/README.md) | Documentation index (also the Gitea wiki fallback) |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Components, storage strategy, import pipeline, auth, scheduler |
| [docs/API.md](docs/API.md) | Complete endpoint reference with examples |
| [docs/DATA_MODEL.md](docs/DATA_MODEL.md) | Input contract, SQLite schema, InfluxDB point, serialization |
| [docs/OPERATIONS.md](docs/OPERATIONS.md) | Deployment, configuration, backup/restore, troubleshooting |
| [docs/WORKPLAN.md](docs/WORKPLAN.md) | Project work plan (Italian, historical reference) |
