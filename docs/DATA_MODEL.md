# Data model

> **Scope.** The contract of the input files, the meaning of every value, and
> the physical layout in both stores. Version 2.0.0.
>
> **Three rules to remember:** one record per `(user, full_date)`; SQLite is the
> source of truth; InfluxDB holds one point per `(user, date)`.

## Input contract

Both supported formats are normalized into the same daily record, so a CSV
export and a `.daylio` backup of the same journal produce identical data.

### CSV export

The header must contain `full_date`; every other column is optional.

```csv
full_date,date,weekday,time,mood,activities,scales,note_title,note
2026-09-21,2026-09-21,lunedì,21:10,bene,"gratitudine | contentezza | noia","Stress: Moderato 3/5",,
```

| Column | Required | Notes |
| --- | --- | --- |
| `full_date` | **yes** | `YYYY-MM-DD`, the identity of the day |
| `date`, `weekday` | no | Derived from `full_date` when absent |
| `time` | no | `HH:MM` or `HH:MM:SS` |
| `mood` | no | A configured mood label, or its numeric value |
| `activities` | no | Tag list, `\|` separated by default (see `columns.options_json`) |
| `scales` | no | `Name: Level k/N`, pairs separated by `\|` |
| `note_title`, `note` | no | Free text |

The parser uses the standard library `csv` module (comma-separated) and
tolerates a UTF-8 BOM. Header names are lowercased and trimmed; a column that is
not declared in the configuration produces a warning but its data is kept. A row
without a non-empty `full_date` is rejected and counted in `errors`, while the
remaining valid rows are still imported. A missing `full_date` header is a hard
`400` for uploads.

Inside the `activities` column, tags are split on the separator configured in
`columns.options.activities.separator` (default `" | "`); in the `scales`
column, `Name: Level k/N` pairs are split on `|` and the non-breaking spaces
present in real exports are normalised before parsing.

### `.daylio` backup

A ZIP archive containing a JSON export. The parser reads the entries and maps
them as follows:

- the entry timestamp becomes `full_date` and `time`;
- the mood becomes the configured mood label → number;
- **tags** become the `activities` list, with the tags of the configured health
  scale (`scala_salute`, `source: tag-group:5`) extracted into the scale instead
  of staying in the activities;
- other tag groups can be mapped to custom scales by declaring a different
  `tag-group:<N>` source.

Both samples in `sample-data/` contain 630 rows for 628 distinct days: the two
duplicates are the same day exported twice, and the daily dedup keeps one.

## Mood

`mood` is an integer where **1 is the best and 5 the worst** (D1). The default
map is Italian:

| Level | Label |
| --- | --- |
| 1 | `alla grande` |
| 2 | `bene` |
| 3 | `così così` |
| 4 | `male` |
| 5 | `malissimo` |

`0` is a legal stored value meaning "the label could not be resolved to the
configured map" (it produces a warning instead of rejecting the row), so a
misconfigured mood never destroys a day of data. The map is configuration: a
per-user override can replace it entirely.

## Scales

A scale is an ordered set of levels. Two types are supported:

- **`text`** — an ordered list of labels, rank 1…N.
- **`number`** — a `min`/`max` range, used for numeric answers.

Each scale declares where its value comes from (`source`):

| `source` | CSV | `.daylio` |
| --- | --- | --- |
| `column` | The `scales` column, `Name: Level k/N` | — |
| `tag-group:<N>` | A level found among the `activities` | Tags whose `id_tag_group` is `N` |

### `scala_salute`

The health scale is **not** a list of activities: it is an ordered 8-level scale
(D2) with a user-defined best-to-worst order.

| Rank | Label |
| --- | --- |
| 1 | `ottimo` |
| 2 | `buono` |
| 3 | `discreto` |
| 4 | `sufficiente` |
| 5 | `mediocre` |
| 6 | `insufficiente` |
| 7 | `pessimo` |
| 8 | `terribile` |

Because the level is an ordered scale, its rank is a number in the record
(`value`) and the label travels with it; `levels_count: 8` tells a client the
range without reading the configuration. The labels are stripped from
`activities` so that the activity ranking is not polluted, and the
`/api/analytics/activities-top` endpoint filters them again defensively.

## Columns

`columns_config` describes the fields of a record. `full_date` is the only
mandatory one; `date` and `weekday` are `derived` (computed from `full_date`,
Italian weekday names) and every other column is optional. The configuration is
exposed through `GET /api/columns`, and a per-user override can narrow the set
of columns exposed for that user.

## SQLite schema

`src/daylio_api/db/migrations.py` applies versioned, idempotent DDL and records
the version in `PRAGMA user_version` (currently `3`). Foreign keys are enabled
on every connection.

### `users`

| Column | Type | Notes |
| --- | --- | --- |
| `id` | INTEGER | Primary key |
| `username` | TEXT | `UNIQUE`, referenced by `day_entries.user` |
| `password_hash` | TEXT | Werkzeug PBKDF2 hash, never the password |
| `is_admin` | INTEGER | `0`/`1` |
| `import_dir` | TEXT | Folder scanned by folder/daily imports, `NULL` if none |
| `created_at`, `updated_at` | TEXT | `YYYY-MM-DD HH:MM:SS` (UTC) |

### `day_entries` — the records

One row per `(user, full_date)`.

| Column | Type | Notes |
| --- | --- | --- |
| `id` | INTEGER | Primary key |
| `user` | TEXT | → `users.username` |
| `full_date` | TEXT | `YYYY-MM-DD` |
| `time` | TEXT | `HH:MM`, `""` if absent |
| `mood` | INTEGER | `0`…`5` (`CHECK (mood BETWEEN 0 AND 5)`) |
| `mood_label` | TEXT | The label that produced `mood` |
| `activities` | TEXT | JSON array of strings |
| `scales` | TEXT | JSON object, see below |
| `note_title`, `note` | TEXT | Free text |
| `source` | TEXT | `csv` or `daylio` |
| `created_at`, `updated_at` | TEXT | Preserved/refreshed by the upsert |
| — | — | **`UNIQUE (user, full_date)`** |

The stored `scales` JSON is keyed by the scale's display label:

```json
{
  "Stress": { "value": 3, "label": "Moderato", "levels_count": 5 },
  "Scala salute": { "value": 3, "label": "discreto", "levels_count": 8 }
}
```

`date` and `weekday` are **not** stored: they are derived at serialization time
from `full_date`.

### Write semantics

- **Dedup within a file** — multiple entries for the same day collapse into one;
  the most recent wins (later time first, then later position in the file). This
  is what turns 630 rows into 628 records.
- **Upsert across runs** — a later import of the same day updates the existing
  row:

  ```sql
  INSERT INTO day_entries (user, full_date, …) VALUES (?, ?, …)
  ON CONFLICT (user, full_date) DO UPDATE SET …
  ```

  `created_at` is kept, `updated_at` is refreshed, and the InfluxDB point for
  that day is replaced.

### Legacy v1 database

If a `mood_entries` table (the schema created by the v1 `server.py`, one row per
*timestamp*) is found next to the new database, the migration folds it into
`day_entries`, keeping the most recent `time` per `(user, full_date)` and
creating the missing users. The rows land with `source: "legacy"` and an empty
`mood_label`; the legacy mood numbering (0…4) is **not** remapped to the 1…5
scale, so review those rows — or simply re-import a clean Daylio export, which is
idempotent and overwrites them. The old table is left untouched.

### `import_jobs` — the audit trail

| Column | Type | Notes |
| --- | --- | --- |
| `id` | INTEGER | Primary key |
| `user` | TEXT | Owner |
| `source` | TEXT | `upload`, `folder` or `daily` (scheduler) |
| `filename` | TEXT | Original file name |
| `imported`, `updated`, `skipped`, `errors` | INTEGER | Counters |
| `detail` | TEXT | JSON array of per-row messages |
| `started_at`, `finished_at` | TEXT | ISO 8601 timestamps |
| `status` | TEXT | `running`, `completed` or `failed` |

A row is written for every run, including failures and rejected formats, and is
readable through `GET /api/import/jobs`.

### `api_keys`

| Column | Type | Notes |
| --- | --- | --- |
| `id` | INTEGER | Primary key |
| `user_id` | INTEGER | → `users.id` |
| `key_prefix` | TEXT | Display prefix, e.g. `daylio_9d312f1829daf` |
| `key_hash` | TEXT | **SHA-256 only** (`UNIQUE`); the plaintext is never stored |
| `name` | TEXT | Free label |
| `created_at`, `last_used_at` | TEXT | `last_used_at` is refreshed on every use |
| `revoked` | INTEGER | Soft delete, so a revoked key stays auditable |

Keys are removed with the user, at the application level.

### Configuration tables

| Table | Purpose | Uniqueness |
| --- | --- | --- |
| `columns_config` | Record columns (`name`, `type`, `required`, `derived`, `source`, `sort_order`, `enabled`, `options_json`) | one row per `column_name` globally (`user_id IS NULL`) and per `(user_id, column_name)` |
| `scales_config` | Scales (`scale_name`, `label`, `scale_type`, `source`, `tag_group`, `min_value`, `max_value`, `levels_count`, `values_json`, `sort_order`, `enabled`) | same global/per-user strategy |
| `mood_map` | The mood label → level map (`mood_level` 1…5, `label` `UNIQUE`) | global |

On boot the YAML configuration is **synchronised into** these tables (`mode=yaml`
by default): new definitions are inserted, changed ones updated, and values
edited in the database for names present in YAML are preserved. Reading
`/api/columns` and `/api/scales` always reflects the effective configuration.

## InfluxDB point model

Measurement: **`daylio_entry`** (configurable). One point per `(user, date)`,
timestamped at the start of the day.

Real point for `alice`, 2026-09-21:

```json
{
  "measurement": "daylio_entry",
  "tags": {
    "user": "alice",
    "date": "2026-09-21",
    "mood_label": "bene",
    "scala_salute_label": "discreto"
  },
  "fields": {
    "mood": 2,
    "mood_num": 2,
    "activities_count": 5,
    "is_favorite": 0,
    "notes_present": 0,
    "activities": "[\"gratitudine\", \"contentezza\", \"noia\", \"ambiente affollato\", \"buon sonno\"]",
    "scales": "[{\"scale_name\": \"Stress\", \"label\": \"Moderato\", \"rank\": 3, \"levels_count\": 5, \"scale_key\": \"stress\"}]",
    "note_title": "",
    "note": "",
    "source": "csv",
    "scale_stress_value": 3,
    "scale_scala_salute_value": 3
  },
  "time": "2026-09-21T00:00:00Z"
}
```

| Tag | Notes |
| --- | --- |
| `user` | Owner; every read filters on it |
| `date` | `YYYY-MM-DD`, convenient for grouping |
| `mood_label` | Present when a label is known |
| `scala_salute_label` | Present when the health scale is set |

| Field | Notes |
| --- | --- |
| `mood`, `mood_num` | Level 1…5 (`mood_num` is the numeric alias kept for charting) |
| `scale_<key>_value` | One integer per configured scale, e.g. `scale_stress_value`, `scale_scala_salute_value` |
| `activities_count` | Number of activities |
| `is_favorite`, `notes_present` | 0/1 markers used by the dashboard |
| `activities`, `scales` | JSON strings (the full detail, pivoted back in Python) |
| `note_title`, `note`, `source` | Mirror of the record |

Adding a new scale does not require a schema change: the field is derived from
the scale key.

### Delete-and-write

Overwriting a day is a **delete of that day's range followed by a write**:

```text
delete(start=2026-09-21T00:00:00Z, stop=2026-09-22T00:00:00Z,
       predicate='_measurement="daylio_entry" AND user="alice"')
write(daylio_entry, user=alice, date=2026-09-21, time=2026-09-21T00:00:00Z, …)
```

Deleting first is what prevents a field from a previous version of the record
(typically a scale the user stopped filling in) from lingering in the TSDB and
reappearing in analytics (R8). InfluxDB is written only when it is reachable;
otherwise the import completes and reports `influx_errors`, and a
[resync](OPERATIONS.md#resync-influxdb-from-sqlite) replays SQLite later.

### Reading

Analytics issue one Flux query per request — bucket, range, measurement, user —
and pivot the frames in Python into the same per-day dictionaries the importer
produces, so both paths agree on the shape of a day.

## Canonical record (JSON)

The API serialization of a `day_entries` row, applied by
`src/daylio_api/api/serializers.py`:

1. `activities` and `scales` are parsed from their stored JSON.
2. Each scale item is normalised to `{value, label}` and enriched with
   `levels_count` from the configuration (falling back to the stored value).
3. `scala_salute` is mirrored at the top level for convenience.
4. `date` and `weekday` are derived from `full_date` (Italian weekday names).
5. `columns`, when given, filters the result to the requested keys.

`mood` is exposed both as a number and, through `mood_label`, as the configured
text; consumers that do not know the language of the installation should use the
numeric level.
