"""Repo-root pytest guard: never seed a NON-disposable (production) DB (ADR-0028).

The DB-backed suites (``runtime/tests``, ``spokesman/tests``, ``evals/tests``)
each self-gate on :func:`runtime.db.can_connect` and then seed synthetic
``work.*`` / ``pollute-*`` tasks and workstreams with **no teardown** (isolation
is by unique workstream). That skip-when-UNREACHABLE guard never distinguished a
throwaway DB from the LIVE studio DB — so ``make test`` against production
permanently polluted the task queue.

There is no single shared ``conn`` fixture to gate (each module defines its own /
some connect inline), so the structural chokepoint is *collection*: if a DB is
reachable but NOT disposable and the operator has not opted in, every DB-backed
test is skipped **loudly** with the guard reason. Behavior matrix:

- no DB reachable        → do nothing (the per-module ``can_connect`` skipif skips)
- DB reachable + disposable (``AI_STUDIO_TEST_DB`` truthy OR ``*test*`` db name)
                         → do nothing (tests run normally)
- DB reachable + NOT disposable + no opt-in
                         → skip every DB-backed item with the loud guard reason

A DB-backed item is identified structurally: its test module CALLS
``can_connect(`` (that is precisely how every DB suite gates — ``not
db.can_connect(timeout=2.0)``) OR it requests a connection fixture (``conn`` /
``live_conn`` / ``setup_conn``). Matching the call form (with the open paren)
avoids flagging pure unit tests that merely mention the name (e.g. this guard's
own tests). The skip is visible in pytest output so "all skipped" is never
mistaken for "all passed".
"""

from __future__ import annotations

from pathlib import Path

import pytest

from runtime import db
from runtime.db_guard import require_disposable_db

#: Fixture names that hand a test a live DB connection in this repo.
_DB_FIXTURES = {"conn", "live_conn", "setup_conn"}

#: Per-file cache: does this test module CALL the ``can_connect`` DB gate?
_DB_MODULE_CACHE: dict[str, bool] = {}

#: The gate every DB suite uses (``not db.can_connect(timeout=2.0)``). Matching the
#: call form (open paren) avoids flagging tests that only mention the name.
_DB_GATE_MARKER = "can_connect("


def _module_is_db_backed(item: pytest.Item) -> bool:
    mod = getattr(item, "module", None)
    path = getattr(mod, "__file__", None)
    if not path:
        return False
    hit = _DB_MODULE_CACHE.get(path)
    if hit is None:
        try:
            hit = _DB_GATE_MARKER in Path(path).read_text("utf-8")
        except OSError:
            hit = False
        _DB_MODULE_CACHE[path] = hit
    return hit


def _item_is_db_backed(item: pytest.Item) -> bool:
    if _DB_FIXTURES.intersection(getattr(item, "fixturenames", ()) or ()):
        return True
    return _module_is_db_backed(item)


def pytest_collection_modifyitems(config, items):  # noqa: D401 - pytest hook
    # Only relevant when a DB is actually reachable. When it is not, the existing
    # per-module ``skipif(not can_connect(...))`` already skips these tests, and we
    # must not shadow that with a misleading "non-disposable" reason.
    if not db.can_connect(timeout=2.0):
        return

    disposable, reason = require_disposable_db()
    if disposable:
        return

    skip = pytest.mark.skip(reason=f"DB guard (ADR-0028): {reason}")
    for item in items:
        if _item_is_db_backed(item):
            item.add_marker(skip)
