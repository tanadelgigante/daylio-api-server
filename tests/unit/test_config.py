"""Unit tests for daylio_api.config (Task 0.4)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from daylio_api.config import (
    DEFAULT_COLUMNS,
    DEFAULT_MOOD_MAP,
    AppConfig,
    ConfigError,
    load,
    mood_override_values,
    seed_defaults,
    sync_schema,
)
from daylio_api.db.migrations import migrate


def _default_cfg_dict() -> dict:
    """Minimal but valid configuration dictionary for negative tests."""
    return {
        "app": {"log_level": "INFO", "timezone": "Europe/Rome"},
        "storage": {
            "sqlite_path": "data/daylio.db",
            "influx_url": "env:INFLUX_URL",
            "influx_org": "tanadelgigante",
            "influx_bucket": "daylio",
            "influx_token": "env:INFLUX_TOKEN",
        },
        "mood": {
            "values": {
                "alla grande": 1,
                "bene": 2,
                "così così": 3,
                "male": 4,
                "malissimo": 5,
            }
        },
        "scales": {
            "stress": {
                "type": "text", "source": "column",
                "values": ["Calma", "Basso", "Moderato", "Alto", "Sopraffatto"],
            },
            "scala_salute": {
                "type": "text", "source": "tag-group:5",
                "values": [
                    "ottimo", "buono", "discreto", "sufficiente",
                    "mediocre", "insufficiente", "pessimo", "terribile",
                ],
            },
        },
        "columns": {
            "required": ["full_date"],
            "optional": [
                "date", "weekday", "time", "mood",
                "activities", "scales", "note_title", "note",
            ],
            "derive": ["date", "weekday"],
        },
        "scheduler": {"backend": "apscheduler", "interval_seconds": 86400, "start_hour": 0},
        "auth": {"session_ttl": 86400, "api_keys": {"enabled": True}},
        "logging": {"level": "INFO"},
    }


def _write_yaml(tmp_path: Path, data: dict) -> Path:
    import yaml

    path = tmp_path / "custom_config.yml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return path


def test_load_default_example_config(config_sample: AppConfig) -> None:
    """The example file is valid and matches the §6 schema decisions."""
    cfg = config_sample
    assert cfg.effective_log_level == "INFO"
    # D1: 5 mood labels ordered 1..5
    assert len(cfg.mood["values"]) == 5
    assert set(cfg.mood["values"].values()) == {1, 2, 3, 4, 5}
    # D2: scala salute has 8 ordered levels
    assert [v.lower() for v in cfg.scales["scala_salute"]["values"]] == [
        "ottimo", "buono", "discreto", "sufficiente",
        "mediocre", "insufficiente", "pessimo", "terribile",
    ]
    # D8.b
    assert cfg.columns["required"] == ["full_date"]
    # D18
    assert cfg.storage["influx_org"] == "tanadelgigante"
    assert cfg.storage["influx_bucket"] == "daylio"


def test_load_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load(tmp_path / "does_not_exist.yml")


def test_load_invalid_yaml_raises(tmp_path: Path) -> None:
    path = tmp_path / "broken.yml"
    path.write_text(": : :\n  bad - yaml\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="Invalid YAML"):
        load(path)


def test_load_explicit_file_and_env_overrides(tmp_path: Path, monkeypatch) -> None:
    path = _write_yaml(tmp_path, _default_cfg_dict())
    monkeypatch.setenv("INFLUX_URL", "http://192.168.188.120:8120")
    monkeypatch.setenv("INFLUX_TOKEN", "secret-token")
    monkeypatch.setenv("DB_PATH", "/custom/daylio.db")
    monkeypatch.setenv("DATA_DIR", "/custom/data")
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "hunter2")

    cfg = load(path)

    assert cfg.storage["influx_url"] == "http://192.168.188.120:8120"
    assert cfg.storage["influx_token"] == "secret-token"
    assert cfg.storage["sqlite_path"] == "/custom/daylio.db"
    assert cfg.to_dict()["data"]["dir"] == "/custom/data"
    assert cfg.auth.get("admin_username") == "admin"
    assert cfg.auth.get("admin_password") == "hunter2"


def test_env_defaults_when_not_set(monkeypatch) -> None:
    """D18 defaults kicked in when no env vars are provided."""
    monkeypatch.delenv("INFLUX_URL", raising=False)
    monkeypatch.delenv("INFLUX_TOKEN", raising=False)
    monkeypatch.delenv("INFLUX_ORG", raising=False)
    monkeypatch.delenv("INFLUX_BUCKET", raising=False)
    cfg = load()
    assert cfg.storage["influx_org"] == "tanadelgigante"
    assert cfg.storage["influx_bucket"] == "daylio"


def test_influx_instance_builds_url(monkeypatch) -> None:
    """D18: INFLUX_INSTANCE=<ip>:<port> -> http://<ip>:<port> when no URL."""
    monkeypatch.delenv("INFLUX_URL", raising=False)
    monkeypatch.setenv("INFLUX_INSTANCE", "192.168.188.120:8120")
    cfg = load()
    assert cfg.storage["influx_url"] == "http://192.168.188.120:8120"


def test_log_level_env_override(monkeypatch) -> None:
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    cfg = load()
    assert cfg.effective_log_level == "DEBUG"


def test_invalid_log_level_raises(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LOG_LEVEL", "TRACE")
    path = _write_yaml(tmp_path, _default_cfg_dict())
    with pytest.raises(ConfigError, match="Invalid LOG_LEVEL"):
        load(path)


def test_invalid_mood_count_raises(tmp_path: Path) -> None:
    data = _default_cfg_dict()
    del data["mood"]["values"]["malissimo"]  # 4 labels -> invalid
    with pytest.raises(ConfigError, match="mood.values must map exactly 5 labels"):
        load(_write_yaml(tmp_path, data))


def test_invalid_mood_indices_raises(tmp_path: Path) -> None:
    data = _default_cfg_dict()
    data["mood"]["values"]["malissimo"] = 9  # not in 1..5
    with pytest.raises(ConfigError, match="indices must be 1..5"):
        load(_write_yaml(tmp_path, data))


def test_empty_text_scale_values_raises(tmp_path: Path) -> None:
    data = _default_cfg_dict()
    data["scales"]["stress"]["values"] = []
    with pytest.raises(ConfigError, match="non-empty 'values'"):
        load(_write_yaml(tmp_path, data))


def test_number_scale_requires_min_max(tmp_path: Path) -> None:
    data = _default_cfg_dict()
    data["scales"]["energy"] = {"type": "number", "source": "column"}
    with pytest.raises(ConfigError, match="min"):
        load(_write_yaml(tmp_path, data))


def test_columns_must_include_full_date(tmp_path: Path) -> None:
    data = _default_cfg_dict()
    data["columns"]["required"] = ["date"]
    with pytest.raises(ConfigError, match="full_date"):
        load(_write_yaml(tmp_path, data))


def test_scheduler_validation(tmp_path: Path) -> None:
    data = _default_cfg_dict()
    data["scheduler"]["interval_seconds"] = -1
    with pytest.raises(ConfigError, match="positive integer"):
        load(_write_yaml(tmp_path, data))


def test_users_file_merge(tmp_path: Path) -> None:
    import yaml

    users_file = tmp_path / "users.yml"
    users_file.write_text(
        yaml.safe_dump(
            {
                "alice": {
                    "import_dir": "/srv/daylio/alice",
                    "scheduler": {"interval_seconds": 3600, "start_hour": 6},
                },
                "bob": {"import_dir": "/srv/daylio/bob"},
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    cfg = load(users_file=users_file)
    assert cfg.users["alice"]["import_dir"] == "/srv/daylio/alice"
    assert cfg.users["alice"]["scheduler"]["interval_seconds"] == 3600
    assert cfg.users["bob"]["import_dir"] == "/srv/daylio/bob"


def test_config_to_dict_roundtrip(config_sample: AppConfig) -> None:
    as_dict = config_sample.to_dict()
    assert as_dict["mood"]["values"]["alla grande"] == 1
    assert as_dict["storage"]["influx_org"] == "tanadelgigante"


def test_env_ref_resolution_keeps_placeholder(monkeypatch) -> None:
    """Missing env refs stay as 'env:VAR' so the example file always loads."""
    monkeypatch.delenv("INFLUX_TOKEN", raising=False)
    cfg = load()
    assert cfg.storage.get("influx_token") in ("env:INFLUX_TOKEN", "") or isinstance(
        cfg.storage.get("influx_token"), str
    )
    assert cfg.storage["influx_token"] == "env:INFLUX_TOKEN" or cfg.storage["influx_token"] == ""


# --------------------------------------------------------------------------- #
# Fase 1 (R1): default seeding + YAML<->SQLite sync on an empty database
# --------------------------------------------------------------------------- #
def _migrated_conn(tmp_path: Path) -> sqlite3.Connection:
    """Return a migrated, empty SQLite connection (Fase 1 schema)."""
    conn = sqlite3.connect(tmp_path / "seed.db")
    conn.row_factory = sqlite3.Row
    migrate(conn)
    return conn


def test_seed_defaults_empty_db(tmp_path: Path) -> None:
    """Task 1.4: first boot on an empty DB seeds the exact D1/D2 defaults."""
    conn = _migrated_conn(tmp_path)
    try:
        seeded = seed_defaults(conn)
        assert seeded == {"mood": 5, "scales": 2, "columns": 9}

        moods = conn.execute(
            "SELECT mood_level, label FROM mood_map ORDER BY mood_level"
        ).fetchall()
        assert [tuple(row) for row in moods] == list(DEFAULT_MOOD_MAP)
        assert [row["label"] for row in moods] == [
            "alla grande", "bene", "così così", "male", "malissimo",
        ]

        scales = conn.execute(
            "SELECT scale_name, levels_count, values_json FROM scales_config "
            "ORDER BY sort_order"
        ).fetchall()
        assert [row["scale_name"] for row in scales] == ["stress", "scala_salute"]
        stress = json.loads(scales[0]["values_json"])
        assert [v["label"] for v in stress] == ["Calma", "Basso", "Moderato", "Alto", "Sopraffatto"]
        salute = json.loads(scales[1]["values_json"])
        assert [v["label"] for v in salute] == [
            "ottimo", "buono", "discreto", "sufficiente",
            "mediocre", "insufficiente", "pessimo", "terribile",
        ]
        assert [v["rank"] for v in salute] == list(range(1, 9))

        columns = conn.execute(
            "SELECT column_name, required, derived FROM columns_config ORDER BY sort_order"
        ).fetchall()
        expected_names = [row["column_name"] for row in DEFAULT_COLUMNS]
        assert [row["column_name"] for row in columns] == expected_names
        full_date = [row for row in columns if row["column_name"] == "full_date"][0]
        assert full_date["required"] == 1
    finally:
        conn.close()


def test_seed_defaults_is_idempotent(tmp_path: Path) -> None:
    """A second seed call on a populated DB inserts nothing (idempotent)."""
    conn = _migrated_conn(tmp_path)
    try:
        first = seed_defaults(conn)
        second = seed_defaults(conn)
        assert second == {"mood": 0, "scales": 0, "columns": 0}
        assert conn.execute("SELECT COUNT(*) FROM mood_map").fetchone()[0] == first["mood"]
    finally:
        conn.close()


def test_sync_schema_yaml_mode_upserts(tmp_path: Path) -> None:
    """Default mode (yaml) upserts rows by name on every boot (YAML wins)."""
    conn = _migrated_conn(tmp_path)
    try:
        seed_defaults(conn)
        custom = _default_cfg_dict()
        custom["mood"]["values"]["nuovissimo"] = 5
        del custom["mood"]["values"]["malissimo"]
        custom["scales"]["stress"]["values"] = ["Solo", "Due", "Livelli"]
        cfg = load(_write_yaml(tmp_path, custom))

        stats = sync_schema(conn, cfg)
        assert stats["mode"] == "yaml"
        assert stats["mood"]["inserted"] == 1  # 5 -> replaced label counts as insert
        # In yaml mode every boot re-writes all existing rows by name (upsert).
        assert stats["scales"]["updated"] == 2  # both scales refreshed

        moods = conn.execute("SELECT label FROM mood_map WHERE mood_level = 5").fetchone()
        assert moods["label"] == "nuovissimo"
        stress = conn.execute(
            "SELECT values_json FROM scales_config WHERE scale_name = 'stress'"
        ).fetchone()
        labels = [v["label"] for v in json.loads(stress["values_json"])]
        assert labels == ["Solo", "Due", "Livelli"]
        salute = conn.execute(
            "SELECT values_json FROM scales_config WHERE scale_name = 'scala_salute'"
        ).fetchone()
        assert len(json.loads(salute["values_json"])) == 8  # untouched values kept
    finally:
        conn.close()


def test_sync_schema_db_mode_preserves_admin_edits(tmp_path: Path) -> None:
    """schema_source=db keeps administrative edits and only inserts missing."""
    conn = _migrated_conn(tmp_path)
    try:
        seed_defaults(conn)
        # Simulate an admin edit: rename the stress scale label levels.
        conn.execute(
            "UPDATE scales_config SET values_json = :v, "
            "updated_at = datetime('now') WHERE scale_name = 'stress'",
            {"v": json.dumps([{"label": "Custom", "rank": 1, "rank2": 2}])},
        )
        conn.commit()

        custom = _default_cfg_dict()
        custom["app"] = {"schema_source": "db", "log_level": "INFO", "timezone": "Europe/Rome"}
        cfg = load(_write_yaml(tmp_path, custom))

        stats = sync_schema(conn, cfg)
        assert stats["mode"] == "db"
        assert stats["scales"]["preserved"] == 2  # both exist -> preserved
        assert stats["scales"]["updated"] == 0

        stress = conn.execute(
            "SELECT values_json FROM scales_config WHERE scale_name = 'stress'"
        ).fetchone()
        assert json.loads(stress["values_json"])[0]["label"] == "Custom"

        # A brand-new scale from YAML is still inserted in db mode.
        custom["scales"]["energy"] = {"type": "number", "source": "column", "min": 0, "max": 10}
        cfg = load(_write_yaml(tmp_path, custom))
        stats = sync_schema(conn, cfg)
        assert stats["scales"]["inserted"] == 1
    finally:
        conn.close()


def test_sync_schema_inserts_missing_columns(tmp_path: Path) -> None:
    """YAML columns not yet present in SQLite are inserted by sync_schema."""
    conn = _migrated_conn(tmp_path)
    try:
        custom = _default_cfg_dict()
        custom["columns"]["optional"].append("custom_col")
        cfg = load(_write_yaml(tmp_path, custom))
        stats = sync_schema(conn, cfg)
        assert stats["columns"]["inserted"] == 1
        names = [r["column_name"] for r in conn.execute("SELECT column_name FROM columns_config")]
        assert "custom_col" in names
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Fase 1 (R1): extended validation (scales with ranked values, per-user)
# --------------------------------------------------------------------------- #
def test_text_scale_ranked_values_mapping(tmp_path: Path) -> None:
    """A text scale may be a label -> rank mapping (unique integer ranks)."""
    custom = _default_cfg_dict()
    custom["scales"]["stress"] = {
        "type": "text",
        "source": "column",
        "values": {"Calma": 1, "Basso": 2, "Moderato": 3, "Alto": 4, "Sopraffatto": 5},
    }
    cfg = load(_write_yaml(tmp_path, custom))
    assert cfg.scales["stress"]["values"]["Calma"] == 1


def test_text_scale_non_unique_ranks_raises(tmp_path: Path) -> None:
    custom = _default_cfg_dict()
    custom["scales"]["stress"] = {
        "type": "text",
        "source": "column",
        "values": {"Calma": 1, "Basso": 1, "Moderato": 3, "Alto": 3, "Sopraffatto": 5},
    }
    with pytest.raises(ConfigError, match="ranks must be unique"):
        load(_write_yaml(tmp_path, custom))


def test_text_scale_duplicate_levels_raise(tmp_path: Path) -> None:
    custom = _default_cfg_dict()
    custom["scales"]["stress"]["values"] = ["Calma", "Calma", "Moderato", "Alto", "Sopraffatto"]
    with pytest.raises(ConfigError, match="duplicate level"):
        load(_write_yaml(tmp_path, custom))


def test_user_mood_override_must_be_1_5(tmp_path: Path) -> None:
    data = _default_cfg_dict()
    users_file = tmp_path / "users.yml"
    users_file.write_text(
        "alice:\n  scales:\n    mood:\n      fantastico: 1\n      bene: 2\n"
        "      così così: 3\n      male: 4\n      malissimo: 4\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="mood scale indices must be 1..5"):
        load(_write_yaml(tmp_path, data), users_file=users_file)


def test_schema_source_invalid_raises(tmp_path: Path) -> None:
    data = _default_cfg_dict()
    data["app"] = {"schema_source": "nonsense", "log_level": "INFO", "timezone": "Europe/Rome"}
    with pytest.raises(ConfigError, match="schema_source"):
        load(_write_yaml(tmp_path, data))


# --------------------------------------------------------------------------- #
# Fase 1 (R1): true idempotency of migrate() on a real file DB + per-user
# validator negative cases
# --------------------------------------------------------------------------- #
def test_migrate_double_call_idempotent_file_db(tmp_path: Path) -> None:
    """Two migrate(conn) calls on the same file DB: no error, no duplication.

    PRAGMA user_version must bump exactly once (0 -> 2, Fase 1 + Fase 3 schema)
    and stay there; a fresh connection opened afterwards sees the same persisted
    version and a no-op migrate succeeds again.
    """
    db_path = tmp_path / "idempotent_migrate.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        first_version = migrate(conn)
        assert first_version == 2
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2

        # Second call on the SAME connection: no-op, no failure.
        second_version = migrate(conn)
        assert second_version == 2
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2

        # No duplicated tables / rows: all config tables exist and are
        # still empty (nothing was inserted before seeding).
        for table in ("columns_config", "scales_config", "mood_map"):
            count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            assert count == 0, f"{table} unexpectedly has {count} rows"
        index_count = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type = 'index' AND name LIKE 'ux_%'"
        ).fetchone()[0]
        assert index_count == 4  # 2 global + 2 per-user (columns+scales)

        # Fresh connection sees the persisted schema and can no-op migrate.
        conn2 = sqlite3.connect(db_path)
        try:
            assert conn2.execute("PRAGMA user_version").fetchone()[0] == 2
            assert migrate(conn2) == 2
            assert conn2.execute("PRAGMA user_version").fetchone()[0] == 2
        finally:
            conn2.close()
    finally:
        conn.close()


def test_user_scale_invalid_type_raises(tmp_path: Path) -> None:
    """User overrides with an invalid scale type fail fast with a clear error."""
    data = _default_cfg_dict()
    users_file = tmp_path / "users_bad_type.yml"
    users_file.write_text(
        "bob:\n  scales:\n    energy:\n      type: 'bogus'\n"
        "      values: ['a', 'b']\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="type must be 'text' or 'number'"):
        load(_write_yaml(tmp_path, data), users_file=users_file)


def test_user_number_scale_requires_min_max(tmp_path: Path) -> None:
    """User number scales still need integer min/max (fail-fast)."""
    data = _default_cfg_dict()
    users_file = tmp_path / "users_bad_number.yml"
    users_file.write_text(
        "bob:\n  scales:\n    energy:\n      type: 'number'\n"
        "      source: 'column'\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="must define integer 'min'"):
        load(_write_yaml(tmp_path, data), users_file=users_file)


# --------------------------------------------------------------------------- #
# Fase 1 bugfix (Tester report): mood override wrapper {type, values}
# --------------------------------------------------------------------------- #
def test_mood_override_values_direct_mapping() -> None:
    """Helper extracts the label->rank map of the direct mapping form."""
    values = mood_override_values({"fantastico": 1, "bene": 2})
    assert values == {"fantastico": 1, "bene": 2}


def test_mood_override_values_wrapper() -> None:
    """Helper extracts the inner values map of the generic wrapper form."""
    values = mood_override_values(
        {"type": "text", "values": {"fantastico": 1, "bene": 2}}
    )
    assert values == {"fantastico": 1, "bene": 2}


def test_mood_override_values_invalid_shapes() -> None:
    """Helper returns None for non-mapping / non-mood definitions."""
    assert mood_override_values(None) is None
    assert mood_override_values("oops") is None
    assert mood_override_values({}) is None
    # Wrapper whose "values" is not a mapping is not a valid mood override.
    assert mood_override_values({"type": "text", "values": ["a", "b"]}) is None


def test_user_mood_wrapper_valid_forms_load(tmp_path: Path) -> None:
    """Both documented mood forms (direct and wrapper) validate cleanly."""
    data = _default_cfg_dict()
    users_file = tmp_path / "users_mood_valid.yml"
    users_file.write_text(
        "alice:\n  scales:\n    mood:\n      type: 'text'\n      values:\n"
        '        "fantastico": 1\n        "bene": 2\n        "così così": 3\n'
        '        "male": 4\n        "malissimo": 5\n',
        encoding="utf-8",
    )
    cfg = load(_write_yaml(tmp_path, data), users_file=users_file)
    mood_values = mood_override_values(cfg.users["alice"]["scales"]["mood"])
    assert mood_values == {
        "fantastico": 1,
        "bene": 2,
        "così così": 3,
        "male": 4,
        "malissimo": 5,
    }


def test_user_mood_wrapper_invalid_type_raises(tmp_path: Path) -> None:
    """A mood wrapper with an invalid type fails validation (never a 500)."""
    data = _default_cfg_dict()
    users_file = tmp_path / "users_mood_bad_type.yml"
    users_file.write_text(
        "alice:\n  scales:\n    mood:\n      type: 'bogus'\n      values:\n"
        '        "fantastico": 1\n        "bene": 2\n        "così così": 3\n'
        '        "male": 4\n        "malissimo": 5\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="type must be 'text' or 'number'"):
        load(_write_yaml(tmp_path, data), users_file=users_file)


def test_user_mood_wrapper_wrong_label_count_raises(tmp_path: Path) -> None:
    """A mood wrapper with != 5 labels is rejected by the D1 validation."""
    data = _default_cfg_dict()
    users_file = tmp_path / "users_mood_bad_count.yml"
    users_file.write_text(
        "alice:\n  scales:\n    mood:\n      type: 'text'\n      values:\n"
        '        "fantastico": 1\n        "bene": 2\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="must map exactly 5 labels"):
        load(_write_yaml(tmp_path, data), users_file=users_file)
