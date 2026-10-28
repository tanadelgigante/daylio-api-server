# Architecture

> **Scope.** How the service is put together: application factory and
> lifecycle, the two stores, the import pipeline, normalization, authentication,
> the dashboard, the scheduler and logging. For endpoint-level details see
> [API.md](API.md); for schemas see [DATA_MODEL.md](DATA_MODEL.md).
>
> **Status.** Describes version 2.0.0.

## Big picture

```text
                 CSV export / .daylio backup
                              │
                    ┌─────────▼─────────┐
                    │  importer/parser  │  csv_importer · daylio_importer
                    └─────────┬─────────┘
                              │  ImportResult (raw rows + errors)
                    ┌─────────▼─────────┐
                    │    normalizer     │  mood, activities, scales,
                    └─────────┬─────────┘  daily dedup (latest wins)
                              │  NormalizedEntry
                    ┌─────────▼─────────┐
                    │   import_flow     │  audit row + dual write
                    └───┬──────────┬────┘
                        │          │
        ┌───────────────▼──┐   ┌───▼──────────────────┐
        │ SQLite (truth)   │   │ InfluxDB 2.x (write) │  delete day → write point
        │ day_entries,     │   │ daylio_entry,        │
        │ users, audit,    │   │ 1 point / (user,date)│
        │ config, api_keys │   └──────────┬───────────┘
        └───────┬──────────┘              │
                │                         │
    ┌───────────▼──────────┐   ┌──────────▼───────────┐
    │  /api/records, ...  │   │  /api/analytics/*    │
    │  (reads SQLite)     │   │  /dashboard          │
    └─────────────────────┘   │  (reads Influx only) │
                              └──────────────────────┘
```

Two rules explain most of the design:

1. **SQLite is the source of truth.** Every read of record data, every
   configuration value and every audit trail comes from SQLite. InfluxDB is a
   derived store optimized for one thing: aggregating daily points over time.
2. **The dashboard never reads SQLite.** Analytics endpoints and the HTML
   dashboard query InfluxDB exclusively, and answer `503` when it is
   unreachable instead of silently falling back (requirement R4 / decision D4).
   This keeps the aggregation path in one place and guarantees that the numbers
   on screen are exactly the numbers in the TSDB.

## Stack

| Layer | Choice | Why |
| --- | --- | --- |
| Language | Python ≥ 3.14 | `zoneinfo`, modern typing, slim images |
| Web framework | Flask 3 | Small, blueprint-oriented, WSGI |
| WSGI server | Waitress | Pure-Python, no C extensions to compile in the image (R10) |
| Truth store | SQLite (`sqlite3` stdlib) | Zero-dependency, transactional, one file to back up |
| Analytics store | InfluxDB 2.x (`influxdb-client`) | Time-series queries, one point per day, cheap range scans |
| Configuration | PyYAML | Human-editable, commented, supports per-user overrides (D17) |
| Sessions | Flask + `itsdangerous` | Signed cookie, no server-side session store |
| Passwords | Werkzeug `generate_password_hash` | PBKDF2 by default |
| Scheduling | APScheduler 3 | Background jobs, interval + first-run hour |
| Tests / lint | pytest, ruff | Fast, no plugins needed |
| Packaging | setuptools (`src/` layout), pip-tools | Reproducible, locked Docker builds |

**No pandas.** The CSV parser uses the standard library `csv` module, which
keeps the image lean and avoids a ~50 MB dependency for a flat, well-defined
file format (D5).

## Source layout

```text
src/daylio_api/
├── app.py              create_app() factory, error handlers, extensions,
│                       lifecycle, lazy WSGI object
├── config.py           YAML + env layered load, validation, `env:VAR` resolution
├── logger.py           get_logger() with LOG_LEVEL threshold
├── audit.py            import audit helpers (counters, detail, job rows)
├── db/
│   ├── sqlite_store.py per-thread connections, record/config/user accessors
│   ├── influx_store.py client, daily point builder, delete-and-write, Flux reads
│   └── migrations.py   versioned, idempotent DDL (PRAGMA user_version)
├── importers/
│   ├── base.py         ImportResult / NormalizedEntry contracts
│   ├── csv_importer.py stdlib CSV parser (BOM tolerant, `Name: Level k/N`)
│   ├── daylio_importer.py ZIP/JSON `.daylio` parser (tags → activities/scales)
│   └── normalizer.py   shared normalization + daily dedup
├── api/
│   ├── routes.py       /api records, config, users, import, legacy
│   ├── auth.py         login/logout/me, API keys, /api/admin/*
│   └── serializers.py  canonical record JSON
├── dashboard/
│   ├── views.py        /dashboard HTML + /api/analytics/*
│   └── static/         dashboard assets
├── import_flow.py      import_data(), import_folder(), resync_all()
└── scheduler.py        per-user periodic folder scans
```

The repository root also contains `server.py`, the **legacy v1 single-file
server** (Flask + pandas). It is kept for reference and migration purposes; the
v2 image, the tests and the documentation do not use it.

## Application factory and lifecycle

`create_app(config_file=None, users_file=None)` does, in order:

1. **Load the configuration** (`config.load`): base YAML file (default
   `config/config.example.yml`, override with `CONFIG_FILE`), then the optional
   per-user file (`USERS_CONFIG_FILE`), then environment variables on top. Any
   `"env:VAR_NAME"` value is resolved from the environment. Validation is
   fail-fast: a malformed YAML or an invalid schema aborts the boot with a clear
   message (R15).
2. **Install the logger** with the `LOG_LEVEL` threshold.
3. **Open the SQLite store** and run the versioned migrations on first connect.
4. **Resolve the InfluxDB configuration** and build the store, but do **not**
   block the boot if InfluxDB is down.
5. **Bootstrap the admin** from `ADMIN_USERNAME`/`ADMIN_PASSWORD` if the user
   table is empty (decision D6: users exist only if an admin creates them, and
   the very first one comes from the environment).
6. **Set the session secret**: `auth.session_secret` (normally
   `env:SESSION_SECRET`); if unresolved a random per-boot secret is used and a
   warning is logged — sessions then do not survive a restart.
7. **Register the blueprints**: `api`, `legacy`, `auth`, `admin`, `dashboard`.
8. **Build the scheduler** and start it only when `scheduler.enabled` is true.

The module exposes a lazy WSGI proxy:

```python
app = _LazyWsgiApp()      # daylio_api.app:app
```

It materialises the real Flask application on the first call, so importing the
module is cheap and the container can start Waitress before any configuration
has been read. `python -m daylio_api.app` starts the development server on
`0.0.0.0:$PORT` (default `5000`).

## Storage strategy

### SQLite (source of truth)

`SqliteStore` keeps **one connection per thread** (`threading.local`) plus a
registry of open connections closed on shutdown. This is required because
Waitress serves requests from a thread pool and SQLite connections are bound to
the thread that created them (`check_same_thread`); sharing one connection
raised `sqlite3.ProgrammingError` under load.

`connect()` creates the parent directory, enables `PRAGMA foreign_keys = ON`
and applies the migrations, so a freshly opened store is always schema-ready.
Migrations are idempotent (`CREATE TABLE IF NOT EXISTS` plus a version counter in
`PRAGMA user_version`) and are safe when two threads race on first connect.

SQLite is written **once per import**, inside the import transaction; the
analytics path never touches it.

### InfluxDB (analytics)

`InfluxStore` writes **one point per `(user, date)`** in the measurement
`daylio_entry`, timestamped at the start of the day (`YYYY-MM-DDT00:00:00Z`).

**Delete-and-write.** Overwriting a day means deleting the day's range for that
user (`_measurement == "daylio_entry" AND user == "<name>"`) and then writing
the new point. Deleting first is what guarantees that a field present in a
previous version of the record (for example a scale the user removed) does not
survive in InfluxDB (R8). The cost is one extra call per day, which is
irrelevant at the volumes this service handles.

Reads use a single Flux query over the range:

```flux
from(bucket: "daylio")
  |> range(start: 2026-09-20T00:00:00Z, stop: 2026-09-22T00:00:00Z)
  |> filter(fn: (r) => r._measurement == "daylio_entry")
  |> filter(fn: (r) => r.user == "alice")
```

The returned frames are pivoted in Python (`read_daily_points`) into the same
per-day dictionaries the importer produces, so analytics and import share one
representation.

## Import pipeline

```text
file (CSV | .daylio)
  → parse            raw rows + per-row errors      importers/*
  → normalize        mood, activities, scales,
                     scala_salute, dedup by day    importers/normalizer.py
  → audit row        status=running                 import_jobs
  → SQLite upsert    INSERT … ON CONFLICT(user, full_date) DO UPDATE
  → Influx write     delete day → write point (only if Influx is available)
  → audit row        status=completed | failed, counters, detail[]
```

- **Dedup.** Within a file, two entries for the same day collapse into one:
  the most recent (later time, then later position in the file) wins. This is
  what makes a CSV export and a `.daylio` backup of the same data produce the
  same 628 daily records out of 630 rows.
- **Upsert.** Re-importing the same day updates the existing row, so
  `created_at` is preserved and `updated_at` moves.
- **Influx is best effort.** If InfluxDB is unavailable the import still
  completes and the summary reports `influx_errors`; a later
  [`resync`](OPERATIONS.md#resync-influxdb-from-sqlite) replays SQLite into
  InfluxDB.
- **Every run is audited.** Successes, parse failures and unrecognized formats
  all leave an `import_jobs` row, readable through `GET /api/import/jobs`.

Folder imports (`import_folder`) scan a directory, import each `.csv`/`.daylio`
file and move it to `done/` or `failed/`, so the same file is never processed
twice and failures can be inspected.

## Normalization and configurability

Nothing about the data model is hardcoded. `config/config.example.yml` defines:

- the **mood map** (label → 1..5, `1` = best);
- any number of **scales**, each `text` (ordered levels) or `number`
  (`min`/`max`), with a `source` telling the importer where to find the value
  (CSV `scales` column, or a `.daylio` tag group);
- the **columns** of a record: `full_date` is the only required one, `date` and
  `weekday` are derived, everything else is optional.

Per-user overrides (`config/users.example.yml`, or a `users:` section) can
replace the mood map, redefine a scale, restrict the exposed columns and set the
import folder and schedule. The API endpoints `/api/columns` and `/api/scales`
expose the effective configuration, with the per-user override applied when
`?user=` is passed.

See [DATA_MODEL.md](DATA_MODEL.md) for the exact contracts.

## Authentication and authorization

Two credential types, resolved by the `login_required` decorator in this order:

1. **Session cookie** — `POST /api/auth/login` verifies the password and writes
   a signed cookie (`daylio_session`, `itsdangerous`, TTL from
   `auth.session_ttl`). Used by the dashboard.
2. **API key** — `X-API-Key: daylio_…` or `Authorization: Bearer daylio_…`.
   Only the SHA-256 hash is stored, together with a display prefix and a
   `last_used_at` stamp; the plaintext is returned exactly once, at creation.

Passwords are stored as Werkzeug PBKDF2 hashes; comparison is constant-time.
Every user is a normal user until an admin promotes them; the
`admin_required` decorator guards `/api/admin/*`.

**Isolation.** `_effective_user()` resolves the target of a request: a
non-admin viewer is always pinned to their own username, and an explicit
`?user=<other>` (or an import into another account) is rejected with `403`. Only
admins can read or import on behalf of someone else. This is enforced in the
endpoints, not just in the UI (D6).

## Dashboard and analytics

The HTML dashboard and the five `/api/analytics/*` endpoints share one helper
chain: resolve the user → parse the date range → **require InfluxDB** (ping;
`503` if it fails) → read the points → aggregate in Python. Keeping the
aggregation in Python (instead of complex Flux) makes the behaviour testable
and identical for the UI and the API.

When `scheduler.enabled` is false — the shipped default — no background job
runs at all; periodic imports are strictly opt-in.

## Scheduler

`scheduler.py` builds one APScheduler `interval` job per user that has an
`import_dir`. The interval and the first-run hour come, in order of precedence,
from `scheduler.per_user.<name>`, then the `users.<name>.scheduler` override,
then the global `scheduler.interval_seconds` / `start_hour`; the first run is
scheduled at the next occurrence of `start_hour` in `app.timezone`.

Each job body opens **its own** `SqliteStore` and `InfluxStore` inside the
worker thread (never reusing the main thread's connection) and calls
`import_folder(..., source="daily")`, so a scheduled scan is audited like any
other import and moves files to `done/`/`failed/`.

## Logging

`logger.get_logger(name)` returns a logger under the `daylio_api` namespace with
the format `[daylio_api] <LEVEL> <message> [key=value …]`. The threshold comes
from `LOG_LEVEL` (default `INFO`); `DEBUG` adds request-level detail and
`INFO` is the normal operational level. `WARNING` and above are always
meaningful. Import summaries, migration results, scheduler scans and every
Influx failure are logged with the user and the affected day, which is enough
to reconstruct what happened without reading the database.

## Error handling

Blueprints register handlers that turn Flask's `HTTPException` into the uniform
body `{"error": "...", "type": "..."}` (`bad_request`, `unauthorized`,
`forbidden`, `not_found`, `conflict`, `service_unavailable`); an unexpected
exception becomes `500` with `{"error": "internal server error", "type":
"internal_error"}` and is logged with a stack trace. Domain code never builds
its own error shape, so a client can rely on the two keys.
