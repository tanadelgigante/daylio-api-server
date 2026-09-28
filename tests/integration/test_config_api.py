"""API tests for Fase 1 configuration reads (Task 1.3): /api/columns, /api/scales.

These tests use the Flask test client against an isolated temporary SQLite
database (the ``DB_PATH`` env override is honoured by ``config.load``) so that
the developer/CI database is never touched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from daylio_api.app import create_app
from tests.helpers import login_assert_ok


@pytest.fixture
def api_client(tmp_path: Path, monkeypatch):
    """Flask test client backed by a fresh temp SQLite database."""
    tmp_db = tmp_path / "api_test.db"
    monkeypatch.setenv("DB_PATH", str(tmp_db))
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


def test_get_columns_returns_defaults(api_client) -> None:
    """GET /api/columns -> ordered columns from columns_config (D8/D8.a/D8.b)."""
    resp = api_client.get("/api/columns")
    assert resp.status_code == 200
    payload = resp.get_json()
    columns = payload["columns"]

    names = [column["name"] for column in columns]
    assert names == [
        "full_date", "date", "weekday", "time", "mood",
        "activities", "scales", "note_title", "note",
    ]
    by_name = {column["name"]: column for column in columns}

    # D8.b: full_date is the only mandatory column.
    assert by_name["full_date"]["required"] is True
    assert all(
        column["required"] is False
        for name, column in by_name.items()
        if name != "full_date"
    )
    # D8.a: date/weekday are derived.
    assert by_name["date"]["derived"] is True
    assert by_name["weekday"]["derived"] is True
    assert by_name["full_date"]["source"] == "csv+daylio"
    # D8.d: activities carries the separator option for phase 4.
    assert by_name["activities"]["options"]["separator"] == " | "
    # Every descriptor exposes the fields phase 4 consumes.
    for column in columns:
        assert set(column) == {
            "name", "label", "type", "required", "derived",
            "source", "sort_order", "enabled", "options",
        }


def test_get_scales_returns_defaults(api_client) -> None:
    """GET /api/scales -> stress (5) + scala salute (8) + D1 mood map."""
    resp = api_client.get("/api/scales")
    assert resp.status_code == 200
    payload = resp.get_json()

    scales = {scale["name"]: scale for scale in payload["scales"]}
    assert list(scales) == ["stress", "scala_salute"]

    stress = scales["stress"]
    assert stress["label"] == "Stress"
    assert stress["type"] == "text"
    assert stress["source"] == "column"
    assert stress["tag_group"] is None
    assert stress["levels"] == 5
    assert [v["label"] for v in stress["values"]] == [
        "Calma", "Basso", "Moderato", "Alto", "Sopraffatto",
    ]
    assert [v["rank"] for v in stress["values"]] == [1, 2, 3, 4, 5]

    salute = scales["scala_salute"]
    assert salute["label"] == "Scala salute"
    assert salute["source"] == "tag-group:5"
    assert salute["tag_group"] == 5
    assert salute["levels"] == 8
    assert [v["label"] for v in salute["values"]] == [
        "ottimo", "buono", "discreto", "sufficiente",
        "mediocre", "insufficiente", "pessimo", "terribile",
    ]

    # D1 mood map: 1 = best ... 5 = worst.
    mood_map = payload["mood_map"]
    assert [entry["label"] for entry in mood_map] == [
        "alla grande", "bene", "così così", "male", "malissimo",
    ]
    assert [entry["level"] for entry in mood_map] == [1, 2, 3, 4, 5]


def test_get_columns_user_allow_list(api_client, tmp_path: Path) -> None:
    """?user= applies the users-config columns.optional allow-list (R4)."""
    users_file = tmp_path / "users_allow.yml"
    users_file.write_text(
        "carol:\n  columns:\n    optional:\n"
        '      - "full_date"\n      - "mood"\n      - "activities"\n',
        encoding="utf-8",
    )
    tmp_db = tmp_path / "api_user_columns.db"
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("DB_PATH", str(tmp_db))
    app = create_app(users_file=users_file)
    app.config["TESTING"] = True
    client = app.test_client()
    monkeypatch.undo()
    login_assert_ok(client)  # D6: ?user= overrides require an authenticated admin

    resp = client.get("/api/columns?user=carol")
    assert resp.status_code == 200
    names = [column["name"] for column in resp.get_json()["columns"]]
    assert names == ["full_date", "mood", "activities"]


def test_get_scales_user_mood_override(api_client, tmp_path: Path) -> None:
    """?user= replaces mood_map when the user defines a custom mood scale."""
    users_file = tmp_path / "users_mood.yml"
    users_file.write_text(
        "alice:\n  scales:\n    mood:\n"
        '      "fantastico": 1\n      "bene": 2\n      "così così": 3\n'
        '      "male": 4\n      "malissimo": 5\n',
        encoding="utf-8",
    )
    tmp_db = tmp_path / "api_user_mood.db"
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("DB_PATH", str(tmp_db))
    app = create_app(users_file=users_file)
    app.config["TESTING"] = True
    client = app.test_client()
    monkeypatch.undo()
    login_assert_ok(client)  # D6: ?user= overrides require an authenticated admin

    resp = client.get("/api/scales?user=alice")
    assert resp.status_code == 200
    mood_map = resp.get_json()["mood_map"]
    assert [entry["label"] for entry in mood_map] == [
        "fantastico", "bene", "così così", "male", "malissimo",
    ]


def test_get_scales_user_mood_override_wrapper(api_client, tmp_path: Path) -> None:
    """?user= accepts the generic {type, values} mood wrapper (bugfix, no 500).

    Regression for the Fase 1 Tester report: the validator accepted the
    wrapper form ``mood: {type: "text", values: {label: rank, ...}}`` but the
    merge helper interpreted it as a direct mapping, crashing with a 500.
    """
    users_file = tmp_path / "users_mood_wrapper.yml"
    users_file.write_text(
        "alice:\n  scales:\n    mood:\n"
        '      type: "text"\n'
        "      values:\n"
        '        "fantastico": 1\n        "bene": 2\n        "così così": 3\n'
        '        "male": 4\n        "malissimo": 5\n',
        encoding="utf-8",
    )
    tmp_db = tmp_path / "api_user_mood_wrapper.db"
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("DB_PATH", str(tmp_db))
    app = create_app(users_file=users_file)
    app.config["TESTING"] = True
    client = app.test_client()
    monkeypatch.undo()
    login_assert_ok(client)  # D6: ?user= overrides require an authenticated admin

    resp = client.get("/api/scales?user=alice")
    assert resp.status_code == 200  # no 500
    mood_map = resp.get_json()["mood_map"]
    assert [entry["label"] for entry in mood_map] == [
        "fantastico", "bene", "così così", "male", "malissimo",
    ]
    assert [entry["level"] for entry in mood_map] == [1, 2, 3, 4, 5]


def test_get_scales_user_mood_wrapper_invalid_type_is_config_error(
    tmp_path: Path,
) -> None:
    """A mood wrapper with an invalid type fails validation with a clear error.

    The invalid users file must raise ConfigError at app creation (not a 500
    at request time).
    """
    users_file = tmp_path / "users_mood_bad_type.yml"
    users_file.write_text(
        "alice:\n  scales:\n    mood:\n"
        '      type: "bogus"\n'
        "      values:\n"
        '        "fantastico": 1\n        "bene": 2\n        "così così": 3\n'
        '        "male": 4\n        "malissimo": 5\n',
        encoding="utf-8",
    )
    tmp_db = tmp_path / "api_user_mood_bad_type.db"
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("DB_PATH", str(tmp_db))
    with pytest.raises(Exception, match="type must be 'text' or 'number'"):
        create_app(users_file=users_file)
    monkeypatch.undo()


def test_user_overrides_do_not_leak_globally(tmp_path: Path) -> None:
    """A per-user scale/mood override must NOT change the global response.

    Criterion 3: the override is visible for the target user only; other users
    and the anonymous/global call keep the default values.
    """
    users_file = tmp_path / "users_leak.yml"
    users_file.write_text(
        "alice:\n  scales:\n    mood:\n"
        '      "fantastico": 1\n      "bene": 2\n      "così così": 3\n'
        '      "male": 4\n      "malissimo": 5\n'
        "bob:\n  scales:\n    energy:\n      type: 'number'\n"
        "      source: 'column'\n      min: 0\n      max: 10\n",
        encoding="utf-8",
    )
    tmp_db = tmp_path / "api_leak.db"
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("DB_PATH", str(tmp_db))
    app = create_app(users_file=users_file)
    app.config["TESTING"] = True
    client = app.test_client()
    monkeypatch.undo()
    login_assert_ok(client)  # D6: ?user= overrides require an authenticated admin

    default_mood = ["alla grande", "bene", "così così", "male", "malissimo"]

    # Global (no ?user=): the overrides must not leak.
    global_resp = client.get("/api/scales")
    assert global_resp.status_code == 200
    payload = global_resp.get_json()
    assert [entry["label"] for entry in payload["mood_map"]] == default_mood
    assert "energy" not in [scale["name"] for scale in payload["scales"]]

    # alice sees only her custom mood; bob's energy scale must be absent.
    alice_resp = client.get("/api/scales?user=alice")
    alice_payload = alice_resp.get_json()
    assert [entry["label"] for entry in alice_payload["mood_map"]] == [
        "fantastico", "bene", "così così", "male", "malissimo",
    ]
    assert "energy" not in [scale["name"] for scale in alice_payload["scales"]]

    # bob sees only his extra scale; alice's mood stays global.
    bob_resp = client.get("/api/scales?user=bob")
    bob_payload = bob_resp.get_json()
    assert [scale["name"] for scale in bob_payload["scales"]] == [
        "stress", "scala_salute", "energy",
    ]
    assert [entry["label"] for entry in bob_payload["mood_map"]] == default_mood


def test_columns_allow_list_not_global(tmp_path: Path) -> None:
    """A user's columns.optional allow-list does not shrink the global columns."""
    users_file = tmp_path / "users_cols_leak.yml"
    users_file.write_text(
        "carol:\n  columns:\n    optional:\n"
        '      - "full_date"\n      - "mood"\n',
        encoding="utf-8",
    )
    tmp_db = tmp_path / "api_cols_leak.db"
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("DB_PATH", str(tmp_db))
    app = create_app(users_file=users_file)
    app.config["TESTING"] = True
    client = app.test_client()
    monkeypatch.undo()
    login_assert_ok(client)  # D6: ?user= overrides require an authenticated admin

    global_resp = client.get("/api/columns")
    assert global_resp.status_code == 200
    assert len(global_resp.get_json()["columns"]) == 9

    carol_resp = client.get("/api/columns?user=carol")
    assert carol_resp.status_code == 200
    assert [column["name"] for column in carol_resp.get_json()["columns"]] == [
        "full_date", "mood",
    ]


def test_get_scales_user_new_scale_appended(api_client, tmp_path: Path) -> None:
    """?user= appends user-only scales after the global ones (premium users)."""
    users_file = tmp_path / "users_scale.yml"
    users_file.write_text(
        "bob:\n  scales:\n    energy:\n      type: 'number'\n"
        "      source: 'column'\n      min: 0\n      max: 10\n",
        encoding="utf-8",
    )
    tmp_db = tmp_path / "api_user_scale.db"
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("DB_PATH", str(tmp_db))
    app = create_app(users_file=users_file)
    app.config["TESTING"] = True
    client = app.test_client()
    monkeypatch.undo()
    login_assert_ok(client)  # D6: ?user= overrides require an authenticated admin

    resp = client.get("/api/scales?user=bob")
    assert resp.status_code == 200
    names = [scale["name"] for scale in resp.get_json()["scales"]]
    assert names == ["stress", "scala_salute", "energy"]
    energy = resp.get_json()["scales"][-1]
    assert energy["type"] == "number"
    assert energy["min"] == 0 and energy["max"] == 10
