"""Unit + behavior tests for the disposable-DB guard (ADR-0028).

Covers:
- :func:`runtime.db_guard.require_disposable_db` decision matrix (opt-in, test-named
  DB, production DB) and robust dbname extraction (URL with params, ``POSTGRES_*``).
- The repo-root ``conftest`` collection hook: when the DB is reachable but NOT
  disposable and there is no opt-in, DB-backed items are SKIPPED (not errored) with
  the loud guard reason. Simulated via monkeypatch — no real DB / no real run needed.
"""

from __future__ import annotations

import importlib
import types

from runtime.db_guard import (
    OPT_IN_ENV,
    extract_dbname,
    require_disposable_db,
)


# --- require_disposable_db decision matrix ----------------------------------


def test_opt_in_makes_any_db_disposable():
    ok, reason = require_disposable_db(
        "postgresql://u@h:5432/aistudio", env={OPT_IN_ENV: "1"}
    )
    assert ok is True
    assert OPT_IN_ENV in reason


def test_opt_in_falsey_values_do_not_count():
    for falsey in ("", "0", "false", "no", "off"):
        ok, _ = require_disposable_db(
            "postgresql://u@h/aistudio", env={OPT_IN_ENV: falsey}
        )
        assert ok is False, falsey


def test_test_suffixed_db_is_disposable_without_opt_in():
    ok, reason = require_disposable_db("postgresql://u@h:5432/aistudio_test", env={})
    assert ok is True
    assert "aistudio_test" in reason


def test_db_name_containing_test_is_disposable():
    ok, _ = require_disposable_db("postgresql://u@h/testing_ground", env={})
    assert ok is True


def test_production_db_without_opt_in_is_not_disposable():
    ok, reason = require_disposable_db("postgresql://u@h:5432/aistudio", env={})
    assert ok is False
    # The reason must be loud + explicit + name the DB + point at the escape hatch.
    assert "refusing" in reason.lower()
    assert "aistudio" in reason
    assert OPT_IN_ENV in reason
    assert "*_test" in reason


def test_reason_never_leaks_a_password():
    ok, reason = require_disposable_db(
        "postgresql://user:sup3rsecret@h:5432/aistudio", env={}
    )
    assert ok is False
    assert "sup3rsecret" not in reason


# --- robust dbname extraction -----------------------------------------------


def test_extract_dbname_url_with_query_params():
    assert extract_dbname("postgresql://u@h:5432/aistudio?sslmode=require") == "aistudio"


def test_extract_dbname_url_plain():
    assert extract_dbname("postgresql://u:p@h:5432/mydb") == "mydb"


def test_extract_dbname_kv_form():
    assert extract_dbname("host=localhost dbname=aistudio_test user=aistudio") == (
        "aistudio_test"
    )


def test_extract_dbname_unknown_returns_none():
    assert extract_dbname("") is None
    assert extract_dbname("postgresql://u@h:5432/") is None


def test_require_resolves_postgres_env_form():
    """With no DATABASE_URL, the name is assembled from POSTGRES_* (build_database_url)."""
    env = {"POSTGRES_DB": "aistudio_test", "POSTGRES_USER": "aistudio",
           "POSTGRES_HOST": "localhost", "POSTGRES_PORT": "5432"}
    ok, reason = require_disposable_db(env=env)
    assert ok is True and "aistudio_test" in reason

    env["POSTGRES_DB"] = "aistudio"
    ok, _ = require_disposable_db(env=env)
    assert ok is False


# --- conftest collection-hook behavior (skip, not error) --------------------


class _FakeItem:
    def __init__(self, name, fixturenames, module):
        self.name = name
        self.fixturenames = fixturenames
        self.module = module
        self.markers = []

    def add_marker(self, marker):
        self.markers.append(marker)


def _fake_module(tmp_path, name, source):
    f = tmp_path / f"{name}.py"
    f.write_text(source, "utf-8")
    mod = types.ModuleType(name)
    mod.__file__ = str(f)
    return mod


def test_conftest_skips_db_items_when_reachable_but_not_disposable(tmp_path, monkeypatch):
    conftest = importlib.import_module("conftest")

    # Simulate: a DB is reachable, but it is NOT disposable and there is no opt-in.
    monkeypatch.setattr(conftest.db, "can_connect", lambda *a, **k: True)
    guard_reason = (
        "refusing to run DB-backed tests against non-disposable DB 'aistudio'; "
        "set AI_STUDIO_TEST_DB=1 or use a *_test database"
    )
    monkeypatch.setattr(
        conftest, "require_disposable_db", lambda *a, **k: (False, guard_reason)
    )

    db_mod = _fake_module(tmp_path, "test_db_like", "from runtime import db\ncan_connect\n")
    plain_mod = _fake_module(tmp_path, "test_pure", "assert True\n")

    db_by_fixture = _FakeItem("t_conn", ["conn"], plain_mod)   # DB via fixture
    db_by_source = _FakeItem("t_src", [], db_mod)              # DB via can_connect ref
    pure = _FakeItem("t_pure", ["tmp_path"], plain_mod)        # not a DB test

    conftest.pytest_collection_modifyitems(None, [db_by_fixture, db_by_source, pure])

    # Both DB items are SKIPPED (marker added), never errored; the pure test is not.
    assert len(db_by_fixture.markers) == 1
    assert len(db_by_source.markers) == 1
    assert pure.markers == []
    marker = db_by_fixture.markers[0]
    assert marker.name == "skip"
    assert "ADR-0028" in marker.kwargs["reason"]
    assert "refusing" in marker.kwargs["reason"].lower()


def test_conftest_no_skip_when_disposable(tmp_path, monkeypatch):
    conftest = importlib.import_module("conftest")
    monkeypatch.setattr(conftest.db, "can_connect", lambda *a, **k: True)
    monkeypatch.setattr(conftest, "require_disposable_db", lambda *a, **k: (True, "ok"))

    db_item = _FakeItem("t_conn", ["conn"], _fake_module(tmp_path, "test_x", "can_connect"))
    conftest.pytest_collection_modifyitems(None, [db_item])
    assert db_item.markers == []  # disposable → run normally


def test_conftest_no_skip_when_unreachable(tmp_path, monkeypatch):
    conftest = importlib.import_module("conftest")
    # Unreachable → do nothing; the per-module can_connect skipif already skips.
    monkeypatch.setattr(conftest.db, "can_connect", lambda *a, **k: False)
    called = {"n": 0}

    def _guard(*a, **k):
        called["n"] += 1
        return (False, "should not be consulted")

    monkeypatch.setattr(conftest, "require_disposable_db", _guard)
    db_item = _FakeItem("t_conn", ["conn"], _fake_module(tmp_path, "test_y", "can_connect"))
    conftest.pytest_collection_modifyitems(None, [db_item])
    assert db_item.markers == []
    assert called["n"] == 0  # guard not even consulted when DB unreachable
