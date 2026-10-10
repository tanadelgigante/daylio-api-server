# Daylio API Server — documentation index

> **Purpose.** Cross-referenced index of the project documentation. This is the
> canonical entry point: the Gitea wiki mirrors these files one-to-one, so
> anyone reading the wiki lands here and can navigate to any topic.
>
> **Wiki fallback.** When the wiki is not published, this `docs/` hierarchy *is*
> the wiki. Keep the mapping stable: the page title and the file name must stay
> in sync.

## Start here

| Page | Read it when | Covers |
| --- | --- | --- |
| [../README.md](../README.md) | First visit | Features, quick start, configuration table, API at a glance, layout, development setup |
| [ARCHITECTURE.md](ARCHITECTURE.md) | You need to change the code | Components, app factory and lifecycle, dual storage, import pipeline, auth, dashboard, scheduler, logging, concurrency |
| [API.md](API.md) | You are calling or wiring the API | Conventions, auth, complete endpoint reference, record shape, error catalogue, import contract |
| [DATA_MODEL.md](DATA_MODEL.md) | You are changing scales, columns or storage | Input CSV/`.daylio` contract, mood and scale definitions, SQLite schema, InfluxDB point layout, serialization rules |
| [OPERATIONS.md](OPERATIONS.md) | You deploy or run the service | Docker Compose, configuration precedence, first run, backup and restore, resync, scheduler, upgrades, troubleshooting |
| [WORKPLAN.md](WORKPLAN.md) | You need the project history | Work plan, decisions D1–D18 and requirements (Italian, historical reference) |

## Reading paths

**I want to use the API**

1. [README — Quick start](../README.md#quick-start-docker-compose) to get a running instance.
2. [API — Authentication](API.md#authentication) to create a key.
3. [API — Records](API.md#records) and [API — Analytics](API.md#analytics-influxdb-only).
4. [DATA_MODEL — Record shape](DATA_MODEL.md#canonical-record-json) to interpret the values.

**I want to import Daylio data**

1. [DATA_MODEL — Input contract](DATA_MODEL.md#input-contract).
2. [API — Import endpoints](API.md#import) and [ARCHITECTURE — Import pipeline](ARCHITECTURE.md#import-pipeline).
3. [OPERATIONS — Automated imports](OPERATIONS.md#automated-imports-scheduler).

**I want to deploy it**

1. [OPERATIONS — Deployment](OPERATIONS.md#deployment) and [Configuration](OPERATIONS.md#configuration).
2. [OPERATIONS — Backup and restore](OPERATIONS.md#backup-and-restore).
3. [OPERATIONS — Troubleshooting](OPERATIONS.md#troubleshooting).

**I want to extend it**

1. [ARCHITECTURE — Layout and responsibilities](ARCHITECTURE.md#source-layout).
2. [DATA_MODEL — SQLite](DATA_MODEL.md#sqlite-schema) and [InfluxDB](DATA_MODEL.md#influxdb-point-model).
3. [OPERATIONS — Configuration precedence](OPERATIONS.md#precedence).

## Conventions used in these documents

- **Language.** All documentation is in English; `WORKPLAN.md` is kept in
  Italian as the historical project record (requirement R12/D12). Code comments
  follow the same rule, with `MAINTENANCE NOTES` docblocks on the core modules.
- **Dates** are always `YYYY-MM-DD` and are interpreted as calendar days.
- **Mood** is `1` = best … `5` = worst; `0` marks a label that could not be
  resolved.
- **Version.** `version.ini` is the single source of truth; the numbers quoted
  in these pages refer to 2.0.0.
- **Examples are real.** Request/response bodies were captured from the running
  application (against the files in `sample-data/`), not hand-written.
- **Decision references** such as `D3` or `R6` point to `WORKPLAN.md`.

## Keeping the documentation current

| Change in the code | Update |
| --- | --- |
| New or removed endpoint, changed parameter or response | [API.md](API.md) |
| New table/column/index, changed scale or column default | [DATA_MODEL.md](DATA_MODEL.md) |
| New component, changed write strategy or lifecycle | [ARCHITECTURE.md](ARCHITECTURE.md) |
| New environment variable, changed default, deployment step | [OPERATIONS.md](OPERATIONS.md) and the table in [../README.md](../README.md#configuration) |
| New dependency or tooling command | [../README.md](../README.md#development) |
