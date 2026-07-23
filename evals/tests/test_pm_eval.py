"""PM structural eval tests.

The pure scorer tests (no DB) prove the eval FLAGS a bad decomposition — an empty
plan, missing criteria, a dependency cycle, and a dangling dependency. The live-DB
test runs the real dry-run PM and asserts it produces well-formed decompositions.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from evals.pm_eval import run_pm_structural_eval, score_decomposition
from runtime import db


def _item(criterion="c", deps=None):
    return {"id": uuid4(), "payload": {"criterion": criterion}, "depends_on": deps or []}


def test_scorer_passes_a_well_formed_decomposition():
    a = _item()
    b = _item(deps=[a["id"]])  # b depends on a (valid edge)
    score = score_decomposition([a, b])
    assert score["passed"]
    assert score["num_items"] == 2 and score["produces_items"]
    assert score["all_items_have_criteria"] and score["dag_acyclic"] and score["deps_sane"]


def test_scorer_flags_empty_plan():
    score = score_decomposition([])
    assert not score["passed"] and not score["produces_items"]


def test_scorer_flags_missing_criteria():
    bad = {"id": uuid4(), "payload": {"criterion": "   "}, "depends_on": []}
    score = score_decomposition([bad])
    assert not score["all_items_have_criteria"] and not score["passed"]


def test_scorer_flags_a_dependency_cycle():
    a = _item()
    b = _item()
    a["depends_on"] = [b["id"]]
    b["depends_on"] = [a["id"]]  # cycle: a <-> b
    score = score_decomposition([a, b])
    assert not score["dag_acyclic"] and not score["passed"]


def test_scorer_flags_dangling_and_self_dependencies():
    a = _item(deps=[uuid4()])  # depends on a non-existent sibling
    assert not score_decomposition([a])["deps_sane"]
    b = _item()
    b["depends_on"] = [b["id"]]  # self-dependency
    assert not score_decomposition([b])["deps_sane"]


@pytest.mark.skipif(
    not db.can_connect(timeout=2.0),
    reason="no reachable DATABASE_URL (expected off-host / no docker)",
)
def test_dry_run_pm_produces_well_formed_decompositions(monkeypatch):
    monkeypatch.setenv("MODELS_DRY_RUN", "1")
    conn = db.connect()
    try:
        from runtime.migrate import migrate
        migrate(conn)
        result = run_pm_structural_eval(conn)
        assert result.passed, result.to_dict()
        assert result.cases and all(c["decision"] == "planned" for c in result.cases)
        assert all(c["num_items"] >= 1 for c in result.cases)
    finally:
        conn.close()
