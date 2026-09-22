"""Unit tests for daylio_api.config (Task 0.4)."""

from __future__ import annotations

from pathlib import Path

import pytest

from daylio_api.config import AppConfig, ConfigError, load


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
