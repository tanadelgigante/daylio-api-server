# API reference

> **Scope.** Every endpoint exposed by version 2.0.0, with the parameters it
> accepts and the bodies it actually returns (captured from the running
> application, using the files in `sample-data/`).
>
> **Conventions.** The service speaks JSON. All dates are `YYYY-MM-DD` and are
> interpreted as calendar days (no time zone, no time component).

## Conventions

### Base URL

| Deployment | URL |
| --- | --- |
| Docker Compose | `http://<host>:3177` (container listens on `5000`) |
| Local dev server | `http://<host>:5000` |

The examples below use `http://localhost:3177`.

### Authentication

Two credential types are accepted on every protected endpoint. They are checked
in this order:

1. **Session cookie** — obtained with `POST /api/auth/login`; sent back by the
   browser automatically (or with `curl -b cookies.txt`).
2. **API key** — sent as either header:

   ```http
   X-API-Key: daylio_<64 hex characters>
   Authorization: Bearer daylio_<64 hex characters>
   ```

A non-admin caller is always restricted to their own data. Admin callers may
pass `?user=<username>` (or the `user` form field on imports) to act on behalf
of another user; a non-admin asking for someone else gets `403`.

### Errors

Every error uses the same two keys:

```json
{ "error": "invalid date format (expected YYYY-MM-DD)", "type": "bad_request" }
```

| Status | `type` | When |
| --- | --- | --- |
| `400` | `bad_request` | Invalid parameter: bad date format, `start_date > end_date`, `page`/`limit` out of range, unknown column or scale, missing upload, unsupported extension, unrecognized file format, missing `import_dir` |
| `401` | `unauthorized` | No session, no API key, or wrong credentials on login |
| `403` | `forbidden` | Authenticated but not allowed: another user's data, non-admin on an admin endpoint |
| `404` | `not_found` | No record for `(user, full_date)`, or an unknown API-key prefix |
| `409` | `conflict` | Would remove the last admin user |
| `503` | `service_unavailable` | An analytics endpoint was called while InfluxDB is unavailable |
| `500` | `internal_error` | Unexpected server error (logged with a stack trace) |

### Record shape

A record returned by the API always has these keys (subject to `columns`):

| Key | Type | Notes |
| --- | --- | --- |
| `full_date` | string | `YYYY-MM-DD`, the only mandatory input |
| `date` | string | `YYYY-MM-DD`, derived from `full_date` |
| `weekday` | string | Italian name: `lunedì`…`domenica` |
| `time` | string | `HH:MM` as exported, `""` when absent |
| `mood` | integer | `1` = best … `5` = worst, `0` = unresolved label |
| `mood_label` | string | The configured label for `mood` |
| `activities` | array of strings | Tags of the day |
| `scales` | object | `{"<scale label>": {"value": int, "label": string, "levels_count": int}}` |
| `scala_salute` | object or `null` | Convenience copy of the health scale |
| `note_title`, `note` | string | Free text, `""` when absent |
| `source` | string | `csv`, `daylio`, or `legacy` for rows folded from a v1 database |
| `created_at`, `updated_at` | string | `YYYY-MM-DD HH:MM:SS` (UTC) |

Example (real response, trimmed):

```json
{
  "activities": ["gratitudine", "contentezza", "noia", "ambiente affollato", "buon sonno"],
  "created_at": "2026-09-29 00:26:24",
  "date": "2026-09-21",
  "full_date": "2026-09-21",
  "mood": 2,
  "mood_label": "bene",
  "note": "",
  "note_title": "",
  "scala_salute": { "label": "discreto", "levels_count": 8, "value": 3 },
  "scales": {
    "Scala salute": { "label": "discreto", "levels_count": 8, "value": 3 },
    "Stress": { "label": "Moderato", "levels_count": 5, "value": 3 }
  },
  "source": "csv",
  "time": "21:10",
  "updated_at": "2026-09-29 00:26:24",
  "weekday": "lunedì"
}
```

`levels_count` comes from the scale configuration (5 for `Stress`, 8 for
`Scala salute`) and is what lets a client interpret `value` without knowing the
scale.

## Version

### `GET /api/version`

Public. Used as a health check.

```json
{ "name": "daylio-api-server", "version": "2.0.0" }
```

## Authentication

### `POST /api/auth/login`

Public. Body: `{"username": "...", "password": "..."}`. On success the response
sets the signed session cookie.

```json
{
  "user": {
    "username": "admin",
    "is_admin": true,
    "import_dir": null,
    "created_at": "2026-09-29 00:26:24",
    "updated_at": "2026-09-29 00:26:24"
  }
}
```

Wrong credentials → `401 {"error": "invalid credentials", "type": "unauthorized"}`.
Missing fields → `400`.

### `POST /api/auth/logout`

Clears the session cookie.

```json
{ "ok": true }
```

### `GET /api/auth/me`

Returns the authenticated identity, or `401` when anonymous.

```json
{
  "user": {
    "username": "bob",
    "is_admin": false,
    "import_dir": "/srv/daylio/bob",
    "created_at": "2026-09-29 00:26:24",
    "updated_at": "2026-09-29 00:26:24"
  }
}
```

### `GET /api/auth/api-keys`

Lists the caller's keys. Only prefixes are ever returned.

```json
{
  "api_keys": [
    {
      "id": 1,
      "name": "bob-ci",
      "key_prefix": "daylio_eb4352a2ef",
      "created_at": "2026-09-29 00:28:02",
      "last_used_at": null,
      "revoked": 0
    }
  ]
}
```

### `POST /api/auth/api-keys`

Body: `{"name": "label"}`. **The plaintext key is returned only here** — store
it immediately.

`201`:

```json
{
  "key": "daylio_9d312f1829daf25c98194de8956a2102ee1c3263f10491906494b7a092cc6adb",
  "prefix": "daylio_9d312f1829daf25c"
}
```

### `DELETE /api/auth/api-keys/<prefix>`

Revokes the key whose display prefix matches.

```json
{ "ok": true }
```

Unknown prefix → `404`.

> **Known issue (2.0.0).** The three `/api/auth/api-keys` endpoints resolve the
> caller's numeric id, which is not present when the request is authenticated by
> an API key; calling them *with an API key* therefore returns `500`. Use a
> session cookie (as in the dashboard) to manage keys.

## Users

### `GET /api/users`

Admin: every user with the record count. Non-admin: only themselves.

```json
{
  "users": [
    { "username": "admin", "is_admin": true, "import_dir": null,
      "records": 0, "created_at": "…", "updated_at": "…" },
    { "username": "alice", "is_admin": false, "import_dir": "/srv/daylio/alice",
      "records": 628, "created_at": "…", "updated_at": "…" }
  ]
}
```

## Admin (`/api/admin/*`)

All endpoints require an admin session (or an admin API key).

### `GET /api/admin/users`

Same payload as `GET /api/users`, always for every user.

### `POST /api/admin/users`

Body: `{"username": "...", "password": "...", "is_admin": false, "import_dir": "/srv/daylio/alice"}`.
`is_admin` and `import_dir` are optional.

`201`:

```json
{ "user": { "username": "bob", "is_admin": false, "import_dir": "/srv/daylio/bob" } }
```

`400` when `username`/`password` are missing, `409 {"error": "user already
exists", "type": "conflict"}` when the username is taken.

### `DELETE /api/admin/users/<username>`

Deletes the user, their records and their API keys. `404` for an unknown user,
`409 {"error": "cannot delete the last admin user", "type": "conflict"}` when
it is the only admin left.

## Configuration

### `GET /api/columns`

Public when called without `?user=`; with `?user=<name>` it requires access to
that user and applies their column allow-list override.

```json
{
  "columns": [
    { "name": "full_date", "label": "Full date", "type": "date", "required": true,
      "derived": false, "source": "csv+daylio", "sort_order": 10,
      "enabled": true, "options": {} },
    { "name": "activities", "label": "Activities", "type": "list",
      "required": false, "derived": false, "source": "csv+daylio",
      "sort_order": 60, "enabled": true, "options": { "separator": " | " } }
  ]
}
```

### `GET /api/scales`

Same authentication rules as `/api/columns`. Returns the configured scales and
the mood map; with `?user=` the per-user overrides are applied.

```json
{
  "scales": [
    { "name": "stress", "label": "Stress", "type": "text", "source": "column",
      "tag_group": null, "levels": 5, "min": null, "max": null,
      "values": [{ "label": "Calma", "rank": 1 }, { "label": "Basso", "rank": 2 }],
      "sort_order": 10, "enabled": true },
    { "name": "scala_salute", "label": "Scala salute", "type": "text",
      "source": "tag-group:5", "tag_group": 5, "levels": 8,
      "values": [{ "label": "ottimo", "rank": 1 }], "sort_order": 20, "enabled": true }
  ],
  "mood_map": [
    { "level": 1, "label": "alla grande" },
    { "level": 2, "label": "bene" },
    { "level": 3, "label": "così così" },
    { "level": 4, "label": "male" },
    { "level": 5, "label": "malissimo" }
  ]
}
```

## Records

### `GET /api/records`

| Parameter | Default | Notes |
| --- | --- | --- |
| `user` | the caller | Non-admins are pinned to themselves; admins may target anyone |
| `start_date`, `end_date` | – | Inclusive `YYYY-MM-DD` bounds |
| `columns` | all | Comma-separated allow-list of column names |
| `page` | `1` | 1-based, must be ≥ 1 |
| `limit` | `100` | 1…1000 |

Selectable column names are the configured columns (`full_date`, `date`,
`weekday`, `time`, `mood`, `activities`, `scales`, `note_title`, `note`) plus the
canonical extras `mood_label`, `scala_salute`, `source`, `created_at`,
`updated_at`. Unknown names → `400 unknown columns: …`.

Records are returned newest first.

```json
{
  "total": 1,
  "page": 1,
  "limit": 100,
  "records": [ { "full_date": "2026-09-21", "mood": 2, "mood_label": "bene" } ]
}
```

With `columns=full_date,mood,mood_label,scala_salute` only those keys are
present, while `total`, `page` and `limit` still describe the **full** result set.

### `GET /api/records/<full_date>`

Same `user` rules. Returns a single record, or `404 {"error": "not_found",
"type": "not_found"}` when that day has no record for the user.

```json
{
  "activities": ["gratitudine", "contentezza", "noia", "ambiente affollato", "buon sonno"],
  "full_date": "2026-09-21",
  "mood": 2,
  "mood_label": "bene",
  "scala_salute": { "label": "discreto", "levels_count": 8, "value": 3 },
  "scales": {
    "Scala salute": { "label": "discreto", "levels_count": 8, "value": 3 },
    "Stress": { "label": "Moderato", "levels_count": 5, "value": 3 }
  },
  "source": "csv",
  "time": "21:10",
  "weekday": "lunedì"
}
```

## Import

### `POST /api/import`

`multipart/form-data`:

| Field | Required | Notes |
| --- | --- | --- |
| `file` | yes | `.csv` or `.daylio`; the format is detected from the content |
| `user` | no | Target owner; admins only, non-admins always import for themselves |

The file is streamed to a temporary path and parsed exactly like a file found in
an import folder. `200` returns the audit summary:

```json
{
  "job": {
    "job_id": 1,
    "user": "alice",
    "source": "upload",
    "filename": "daylio_export_2026_09_22.csv",
    "status": "completed",
    "total_rows": 630,
    "imported": 628,
    "updated": 0,
    "skipped": 2,
    "errors": 0,
    "warnings": 0,
    "influx_errors": 0,
    "detail": [],
    "started_at": "2026-09-29T00:25:23.300451+00:00",
    "finished_at": "2026-09-29T00:25:23.333519+00:00"
  }
}
```

Counters: `total_rows` rows read, `imported` days newly inserted, `updated`
days overwritten, `skipped` duplicate days dropped, `errors` rows rejected
(details in `detail`), `influx_errors` days that could not be synchronised to
InfluxDB (the SQLite import still succeeded).

Re-importing the same file updates instead of duplicating:

```json
{ "user": "alice", "source": "upload", "filename": "backup_2026_09_22.daylio",
  "imported": 0, "updated": 628, "skipped": 2, "errors": 0,
  "influx_errors": 0, "warnings": 0, "total_rows": 630, "status": "completed",
  "job_id": 2 }
```

Errors: `400` for a missing `file`, an extension other than `.csv`/`.daylio`,
unrecognized content, or a parse failure. A failed import still leaves an
`import_jobs` row with `status: "failed"` — every import leaves a trail.

### `POST /api/import/folder`

Scans a folder and imports every `.csv`/`.daylio` file, moving each to `done/`
(completed) or `failed/` (not completed).

| Field | Notes |
| --- | --- |
| `user` | Target owner (admins only) |
| `import_dir` | Overrides the configured folder; otherwise the user's `import_dir` (YAML override, then the `users` row) is used |

`200` returns one summary per processed file:

```json
{
  "imports": [
    { "job_id": 3, "user": "bob", "source": "folder", "filename": "export.csv",
      "status": "completed", "total_rows": 1, "imported": 1, "updated": 0,
      "skipped": 0, "errors": 0, "warnings": 0, "influx_errors": 0,
      "detail": [], "moved_to": "/srv/daylio/bob/done",
      "started_at": "…", "finished_at": "…" }
  ]
}
```

`400 {"error": "import_dir is not configured for user 'bob'"}` when no folder is
set, `400 {"error": "import folder not found: …"}` when the path does not exist.
The same call is what the scheduler runs, with `source: "daily"`.

### `GET /api/import/jobs`

| Parameter | Default | Notes |
| --- | --- | --- |
| `user` | the caller (admins: all users) | Non-admins cannot filter on another user |
| `limit` | `100` | 1…1000, most recent first |

```json
{
  "jobs": [
    { "id": 2, "user": "alice", "source": "upload",
      "filename": "backup_2026_09_22.daylio", "status": "completed",
      "imported": 0, "updated": 628, "skipped": 2, "errors": 0, "detail": [],
      "started_at": "2026-09-29T00:25:23.300451+00:00",
      "finished_at": "2026-09-29T00:25:23.333519+00:00" }
  ]
}
```

## Analytics (InfluxDB only)

All five endpoints require authentication and read **only** from InfluxDB. If
InfluxDB cannot be reached they answer `503`:

```json
{
  "error": "InfluxDB is not available: the dashboard reads analytics from InfluxDB only (R4/D4)",
  "type": "service_unavailable"
}
```

Common parameters: `user` (same rules as the records API), `start_date` and
`end_date` (inclusive; both default to the whole available range).

### `GET /api/analytics`

Catalogue of the available charts.

```json
{
  "source": "influxdb",
  "charts": [
    { "chart": "mood-over-time", "title": "Mood over time" },
    { "chart": "mood-distribution", "title": "Mood distribution" },
    { "chart": "heatmap", "title": "Mood heatmap (weekday x hour)" },
    { "chart": "scales-trend", "title": "Scales trend" },
    { "chart": "activities-top", "title": "Top activities" }
  ]
}
```

### `GET /api/analytics/mood-over-time`

`granularity=day|week`; it is chosen automatically — `week` for ranges wider than
92 days, `day` otherwise.

```json
{
  "user": "alice", "start_date": "2026-09-20", "end_date": "2026-09-21",
  "granularity": "day", "days": 2, "buckets": 2,
  "dates": ["2026-09-20", "2026-09-21"],
  "values": [1.0, 2.0],
  "levels": [{ "1": 1, "2": 0, "3": 0, "4": 0, "5": 0 },
             { "1": 0, "2": 1, "3": 0, "4": 0, "5": 0 }],
  "average": 1.5
}
```

`days` counts the records read, `buckets` the plotted points (they differ when
`granularity=week`); `values` is `null` for buckets with no data.

### `GET /api/analytics/mood-distribution`

```json
{
  "user": "alice", "start_date": "2026-09-01", "end_date": "2026-09-21",
  "levels": [1, 2, 3, 4, 5],
  "labels": ["alla grande", "bene", "così così", "male", "malissimo"],
  "counts": [4, 10, 3, 3, 1],
  "days": 21, "total": 21, "average": 2.38
}
```

### `GET /api/analytics/heatmap`

Weekday × hour matrix of the mean mood. `metric=mean_mood|count`
(default `mean_mood`).

```json
{
  "user": "alice", "start_date": "2026-09-20", "end_date": "2026-09-21",
  "metric": "mean_mood", "days": 2, "total": 2,
  "weekdays": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
  "buckets": ["00", "01", "…", "23"],
  "data": "7 rows (Mon..Sun) × 24 columns of mean mood, null when empty",
  "counts": "7 rows × 24 columns of record counts"
}
```

Because points are timestamped at the start of the day, every record currently
lands in the `00` column; the matrix is intraday-ready for when the measurement
carries a real time.

### `GET /api/analytics/scales-trend`

`scale=<name or config key>`, case-insensitive; unknown values return
`400 {"error": "unknown scale 'bogus' (available: scala_salute, stress)"}`.
Without `scale` the response holds one entry per scale.

```json
{
  "user": "alice", "start_date": "2026-09-20", "end_date": "2026-09-21",
  "scale_name": "stress", "scale_label": "Stress",
  "days": 2, "days_with_value": 2,
  "dates": ["2026-09-20", "2026-09-21"],
  "values": [2, 3],
  "labels": ["Basso", "Moderato"]
}
```

### `GET /api/analytics/activities-top`

`limit` (default 10, 1…50). Ties are ordered alphabetically. The health scale
labels are excluded from the ranking and reported as `excluded_scale`.

```json
{
  "user": "alice", "start_date": "2026-09-01", "end_date": "2026-09-21",
  "limit": 3, "days": 21, "distinct": 27, "excluded_scale": "scala_salute",
  "activities": [
    { "activity": "contentezza", "count": 11 },
    { "activity": "soddisfazione", "count": 11 },
    { "activity": "sonno medio", "count": 11 }
  ]
}
```

## Dashboard

### `GET /dashboard` (and `/dashboard/`)

HTML page with the five charts. Requires a session; renders the same
aggregations as the analytics endpoints.

## Legacy endpoints

Kept for compatibility with the v1 clients. They are authenticated and obey the
same isolation rules, but return a reduced shape (no scales, no notes
structure) and have **no pagination**.

### `GET /users`

```json
[ { "user": "alice", "records": 628 }, { "user": "admin", "records": 0 } ]
```

A non-admin sees only their own entry.

### `GET /moods`

| Parameter | Required | Notes |
| --- | --- | --- |
| `user` | yes | `400` when missing |
| `start_date`, `end_date` | no | Inclusive bounds |

```json
[
  { "date": "2026-09-21", "time": "21:10", "mood": 2, "note": "",
    "note_title": "",
    "activities": ["gratitudine", "contentezza", "noia", "ambiente affollato", "buon sonno"] }
]
```

> The `/average_mood` endpoint of v1 no longer exists: averages are exposed by
> `/api/analytics/mood-over-time` (`average`) and
> `/api/analytics/mood-distribution` (`average`).
