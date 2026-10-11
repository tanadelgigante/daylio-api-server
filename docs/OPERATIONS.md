# Operations

> **Scope.** Deploying, configuring, running and recovering version 2.0.0 of the
> Daylio API Server.

## Requirements

| Target | Needs |
| --- | --- |
| Docker deployment | Docker + Compose plugin |
| Bare metal / dev | Python ≥ 3.12, a writable data directory, and an InfluxDB 2.x instance for the dashboard |

SQLite needs nothing else: the whole record store is a single file.

## Deployment

### Docker Compose (recommended)

`docker-compose.yml` runs two services:

| Service | Image / build | Host port | Notes |
| --- | --- | --- | --- |
| `daylio_api` | built from this repository | `3177` → container `5000` | Restart `unless-stopped` |
| `influxdb` | `influxdb:latest` | `8120` → container `8086` | Volumes under `/var/influx/`; needs a one-time onboarding |

```sh
# 1. configuration: create .env (see the table below)
# 2. start
docker compose up -d --build
# 3. follow the boot log
docker compose logs -f daylio_api
```

The image is a two-stage build: locked dependencies from `requirements.lock`
installed into a virtualenv, then a slim `python:3.12-slim` runtime that starts
Waitress on the WSGI entry point `daylio_api.app:app`. Only `src/`,
`version.ini` and `config/config.example.yml` are copied in; the configuration
directory is mounted read-only at `/app/config` and the data directory at
`/app/data`.

> **Mind the mount.** Compose mounts `./config` over `/app/config`, so the copy
> of `config.example.yml` baked into the image becomes invisible. Either keep
> `config.example.yml` in the mounted directory, or point `CONFIG_FILE` at the
> file you actually provide (for example
> `CONFIG_FILE=/app/config/config.yml`). A configuration file that is missing or
> invalid stops the service at boot on purpose (fail-fast, R15) — the log names
> the file.

### What Compose forwards

The `environment:` list of `docker-compose.yml` is explicit: only
`CONFIG_FILE`, `LOG_LEVEL`, `ADMIN_USERNAME`, `ADMIN_PASSWORD`, `INFLUX_URL`,
`INFLUX_TOKEN`, `INFLUX_ORG`, `INFLUX_BUCKET`, `DATA_DIR` and `DB_PATH` are
interpolated from `.env` into the container. Two consequences:

- **`SESSION_SECRET` is not forwarded.** A value in `.env` alone changes
  nothing: the app falls back to a random per-boot secret and logs
  `auth.session_secret is not configured; …`, so dashboard sessions end at every
  restart. Add `- SESSION_SECRET=${SESSION_SECRET}` to the `environment:` list
  to fix it, or run the app outside Compose with the variable exported.
- **`USERS_CONFIG_FILE` is not forwarded either.** Put per-user overrides in the
  `users:` section of the mounted configuration file, which needs no extra
  variable.

### Without Docker

```sh
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements.lock          # or: pip install -e ".[dev]"
CONFIG_FILE=config/config.yml \
ADMIN_USERNAME=admin ADMIN_PASSWORD='…' SESSION_SECRET='…' \
  waitress-serve --port=5000 daylio_api.app:app
```

For development, `python -m daylio_api.app` starts the Flask development server
on `0.0.0.0:${PORT:-5000}`.

## Configuration

### Precedence

From lowest to highest:

1. `config/config.example.yml` — the reference schema (used directly when no
   other file is given).
2. The file pointed to by `CONFIG_FILE` (a deployment copy of the same schema).
3. The per-user file pointed to by `USERS_CONFIG_FILE` (same schema, top-level
   usernames).
4. Environment variables.
5. Values written as `"env:VAR_NAME"` in the YAML are replaced by the
   environment variable; if it is missing the literal placeholder is kept (and
   treated as unconfigured, which is what makes a missing `SESSION_SECRET`
   detectable).

The base file is validated at boot: invalid YAML or an invalid schema stops the
startup with a clear message instead of starting with wrong values.

### Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `CONFIG_FILE` | `config/config.example.yml` | Base configuration file |
| `USERS_CONFIG_FILE` | – | Per-user override file |
| `DB_PATH` | `storage.sqlite_path` | SQLite file (source of truth) |
| `DATA_DIR` | `data` | Data directory |
| `INFLUX_URL` | from YAML | InfluxDB URL, e.g. `http://influxdb:8086` |
| `INFLUX_TOKEN` | from YAML | InfluxDB API token — **secret** |
| `INFLUX_ORG` | `tanadelgigante` | InfluxDB organization |
| `INFLUX_BUCKET` | `daylio` | InfluxDB bucket |
| `INFLUX_INSTANCE` | – | `<host>:<port>`, expanded to `http://<host>:<port>` (used by CI) |
| `ADMIN_USERNAME` | – | Bootstrap admin username |
| `ADMIN_PASSWORD` | – | Bootstrap admin password — **secret** |
| `SESSION_SECRET` | random per boot | Secret for the signed session cookie — **secret** |
| `LOG_LEVEL` | `INFO` | `DEBUG`/`INFO`; `WARNING`+ are always active |
| `PORT` | `5000` | Port of the development server only |

**Never** write secrets into a YAML file: reference them as `env:VAR_NAME` and
provide the value through the environment (or a `.env` file for Compose, which
is git-ignored).

## First run

1. Start the stack and open InfluxDB on <http://localhost:8120>.
2. Complete the onboarding: create the organization (`tanadelgigante`) and the
   bucket (`daylio`), then generate an **API token** (All-access is enough) and
   put it in `INFLUX_TOKEN`; restart the app service.
3. The bootstrap admin is created from `ADMIN_USERNAME`/`ADMIN_PASSWORD` on the
   first boot. An existing account with that username is promoted to admin; an
   existing password is **not** overwritten.
4. Create the real users:

   ```sh
   curl -c c.txt -X POST http://localhost:3177/api/auth/login \
     -H 'Content-Type: application/json' \
     -d '{"username":"admin","password":"…"}'

   curl -b c.txt -X POST http://localhost:3177/api/admin/users \
     -H 'Content-Type: application/json' \
     -d '{"username":"alice","password":"…","import_dir":"/srv/daylio/alice"}'
   ```

5. Create an API key for automation (the plaintext is shown once):

   ```sh
   curl -b c.txt -X POST http://localhost:3177/api/auth/api-keys \
     -H 'Content-Type: application/json' -d '{"name":"automation"}'
   ```

6. Import the sample data to check the pipeline end to end:

   ```sh
   curl -b c.txt -X POST http://localhost:3177/api/import \
     -F 'file=@sample-data/daylio_export_2026_09_22.csv' -F 'user=alice'
   ```

## Monitoring and health checks

| Check | Command | Expected |
| --- | --- | --- |
| Service alive | `curl -s localhost:3177/api/version` | `{"name": "daylio-api-server", "version": "2.0.0"}` |
| Analytics reachable | `curl -s -H "X-API-Key: $KEY" 'localhost:3177/api/analytics/mood-over-time?limit=1'` | JSON, or `503` when InfluxDB is down |
| Records reachable | `curl -s -H "X-API-Key: $KEY" 'localhost:3177/api/records?limit=1'` | JSON |
| Last imports | `curl -s -H "X-API-Key: $KEY" 'localhost:3177/api/import/jobs?limit=5'` | JSON with `status` and counters |

Logs are written to stdout in the format
`[daylio_api] <LEVEL> <message> [key=value …]`. Useful markers at `INFO`:
migration results, config sync, admin bootstrap, every import summary (with
`imported`/`updated`/`skipped`/`errors`/`influx_errors`), scheduler scans, and
`Influx resync done`.

Set `LOG_LEVEL=DEBUG` while diagnosing an import: it adds the per-file parse and
per-request details.

## Automated imports (scheduler)

Periodic imports are **opt-in**: `scheduler.enabled` ships `false`, so a fresh
install never runs a background job until it is configured explicitly.

```yaml
scheduler:
  backend: "apscheduler"
  enabled: true
  interval_seconds: 86400
  start_hour: 0
  per_user:
    alice:
      interval_seconds: 3600
      start_hour: 6
```

A per-user override in `config/users.example.yml` (`users.<name>.scheduler`)
takes precedence over `scheduler.per_user`, which takes precedence over the
global values. One job is created per user that has an `import_dir`; the first
run happens at the next occurrence of `start_hour` in `app.timezone`, then every
`interval_seconds`.

Jobs call the same `import_folder` used by `POST /api/import/folder`, with
`source: "daily"`, so scheduled runs are audited in `import_jobs` and their
files are moved to `done/` or `failed/`. Because a job opens its own database
connections inside the worker thread, it is safe with a multi-threaded Waitress.

## Backup and restore

### SQLite (records, users, configuration, audit)

The database is a single file whose path is `DB_PATH` (in Compose:
`./data/daylio.db` on the host). The connection uses SQLite's default rollback
journal, so copy it while the service is **stopped**, or take a consistent
snapshot with the SQLite CLI:

```sh
# consistent online backup (Python's sqlite3 backup API)
docker compose exec daylio_api python -c \
  "import sqlite3; s=sqlite3.connect('/app/data/daylio.db'); d=sqlite3.connect('/app/data/backup.db'); s.backup(d)"
# or, with the service stopped
docker compose stop daylio_api
cp ./data/daylio.db ./backups/daylio-$(date +%F).db
docker compose start daylio_api
```

Restoring is a file copy back to `DB_PATH` followed by a restart; the migrations
run automatically on the next connection and are no-ops on an up-to-date file.

### InfluxDB

InfluxDB is a derived store, so losing it costs only the dashboard until a
resync. If you do keep backups, the standard Influx mechanisms apply (bucket
snapshot, `influx backup`, or the `/var/influx/` volumes mounted by the Compose
service).

### Import folders

`import_dir` contents are inputs, not data: processed files are moved to
`done/`, failures to `failed/`. Back them up only if you need the raw exports.

## Resync (InfluxDB from SQLite)

After restoring SQLite, replacing the InfluxDB bucket, or fixing an Influx
outage, replay every stored day into InfluxDB. There is no HTTP endpoint for
this; run it once with the application importable:

```sh
python - <<'PY'
from daylio_api.app import create_app
from daylio_api.import_flow import resync_all

app = create_app()
store = app.extensions["daylio_store"]
influx = app.extensions.get("daylio_influx")
print(resync_all(store.connect(), influx))
PY
```

```json
{ "users": 1, "days": 628, "influx_errors": 0, "detail": [] }
```

`resync_all(conn, influx, user="alice")` restricts the replay to one user.
SQLite is never modified; each day is replaced with the same delete-and-write
used by the import path, so the resync is idempotent. If InfluxDB is not
configured or not reachable, the call returns immediately with
`{"users": 0, "days": 0, "influx_errors": 1, "detail": [...]}` — fix the
connection and run it again.

## Upgrades

- `version.ini` is the single source of the version; the release job bumps it.
- The database schema is migrated automatically on the first connection after
  the upgrade: migrations are idempotent and their version is stored in
  `PRAGMA user_version`. Taking a SQLite backup before upgrading is still
  recommended.
- `requirements.lock` is the dependency set used by the image; CI checks that it
  is in sync with `requirements.in`. Rebuild the image after every upgrade
  (`docker compose up -d --build`).

## CI and image registries

The Gitea Actions workflow (`ci.yml`) builds and publishes a multi-arch image
(`linux/amd64,linux/arm64`) with this **two-registry flow**:

1. **Build -> Gitea container registry.** The `build` job pushes the image to
   `$GITHUB_SERVER_URL` host / `<owner>/<repo>` (login with the `DUNGEONPAT`
   secret via stdin). It runs automatically on `push` when the previous commit
   touched the `Dockerfile`, and can always be forced with a manual
   `workflow_dispatch job=build` dispatch. Pull and smoke-test this image
   before releasing:
   ```sh
   docker pull <gitea-host>/tanadelgigante/daylio-api-server:v2.0.0
   docker run --rm -p 3177:5000 \
     -e ADMIN_USERNAME=admin -e ADMIN_PASSWORD=<pwd> \
     -e INFLUX_URL=http://192.168.188.120:8120 -e INFLUX_TOKEN=<token> \
     -e INFLUX_ORG=tanadelgigante -e INFLUX_BUCKET=daylio \
     <gitea-host>/tanadelgigante/daylio-api-server:v2.0.0
   ```
2. **Release -> Docker Hub (manual only).** The `release` job (reachable ONLY
   through `workflow_dispatch job=release`) pulls that tag from the Gitea
   registry, re-tags it to the Docker Hub image (`tanadelgigante/daylio-api-server`,
   override `vars.IMAGE_REPO`) and pushes it with the release-only secrets
   `DOCKER_USERNAME`/`DOCKER_PASSWORD` (registry `vars.DOCKER_REGISTRY`,
   default `docker.io`). It then creates the Gitea release and bumps
   `version.ini`. A push or tag push never triggers a release.

## Troubleshooting

| Symptom | Cause | Action |
| --- | --- | --- |
| Service exits at boot with a configuration error | The mounted `./config` hides the baked-in `config.example.yml`, or the YAML is invalid | Keep `config.example.yml` in the mounted directory or set `CONFIG_FILE` to your file; the log names the offending file |
| `503 {"type": "service_unavailable"}` on `/api/analytics/*` | InfluxDB unreachable or `INFLUX_TOKEN` missing/expired | Check the container/`INFLUX_URL`, the token, and the org/bucket names; then [resync](#resync-influxdb-from-sqlite) |
| `401 {"error": "authentication required"}` | No session cookie and no API key | Send `X-API-Key` or log in |
| Sessions lost after every restart | `SESSION_SECRET` unset inside the container (Compose does not forward it) | Add `- SESSION_SECRET=${SESSION_SECRET}` to the Compose `environment:` list, or export it when running outside Compose |
| `403 cannot access another user's data` | Non-admin asking for another user | Use your own account, or an admin token |
| `403 admin privileges required` | Non-admin on `/api/admin/*` | Use the admin account |
| `409 cannot delete the last admin user` | Deleting the only admin | Create another admin first |
| `400 import_dir is not configured for user 'x'` | Folder import without a folder | Set `import_dir` on the user (`POST /api/admin/users`) or pass `import_dir` in the request |
| `400 unsupported file type` / `400 unrecognized file format` | The upload is not a `.csv`/`.daylio` export | Re-export from Daylio (`Export → CSV` or a `.daylio` backup) |
| `500` on `/api/auth/api-keys` while using an API key | Known issue in 2.0.0 (the caller id is missing for API-key auth) | Manage keys with a session cookie |
| Import says `influx_errors > 0` | InfluxDB was down during the run | Check the logs, then resync |
| `imported=0, updated=N` | The same days were imported again | Expected: the import is idempotent, days are updated in place |
