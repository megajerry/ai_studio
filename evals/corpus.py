"""Corpus-as-data loader for the eval harness (v2).

Harness v1 hardcoded every seeded case in Python. Harness v2 moves them into
versioned data files under ``evals/corpus/`` (``*.yaml``) so the corpus can grow to
hundreds of cases by editing DATA, not code — the mechanism the stakeholder asked
for. This module is the single loader: it reads those files into typed objects the
individual evals consume.

Loaded here:

- :func:`load_verifier_cases` — the labeled seeded-defect Verifier corpus
  (``verifier_cases.yaml``), with ``{good_marker}``/``{bad_marker}`` templated to
  per-run UNIQUE markers so cases never collide (the guarantee the old uuid4 code
  had, preserved as data).
- :func:`load_pm_goals` — the labeled PM decomposition goals (``pm_goals.yaml``).
- :func:`load_rubrics` / :func:`load_rubric` — the judge rubrics (``rubrics.yaml``)
  the swappable LLM-as-judge scores against.

No DB, no model, no secrets — pure file loading.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

import yaml

#: Directory holding the versioned corpus data files.
CORPUS_DIR = Path(__file__).resolve().parent / "corpus"


# --- Verifier seeded-defect cases -------------------------------------------


@dataclass
class VerifierCase:
    """One labeled ``(artifact, criterion, expected pass/fail)`` case.

    ``content`` is written to the scratch root as the artifact (``None`` = the
    Executor produced no artifact). ``claimed_ok`` is what the Executor *asserts*
    about its own result — deliberately True on some defective cases so the eval
    proves the gate ignores the claim and decides on evidence (ADR-0014).
    """

    name: str
    label: str  # human tag: "GOOD" / "BAD:<why>"
    check: str  # "marker" | "video_audit"
    expected_pass: bool
    content: Optional[str] = None
    marker: Optional[str] = None
    require: Any = None
    claimed_ok: bool = True


def _verifier_path(path: Optional[str]) -> Path:
    return Path(path) if path else CORPUS_DIR / "verifier_cases.yaml"


def load_verifier_cases(path: Optional[str] = None) -> list[VerifierCase]:
    """Load the labeled Verifier corpus from YAML, templating unique markers.

    ``{good_marker}`` / ``{bad_marker}`` in ``marker`` and ``content`` are replaced
    with fresh per-call unique markers so no marker leaks between cases or runs
    (exactly what the v1 ``uuid4`` code guaranteed, now data-driven). Any other
    fields are passed through verbatim.
    """
    data = yaml.safe_load(_verifier_path(path).read_text(encoding="utf-8")) or {}
    subs = {
        "good_marker": f"studio-ok:{uuid4().hex[:8]}",
        "bad_marker": f"studio-ok:{uuid4().hex[:8]}",
    }

    def _fmt(v: Any) -> Any:
        return v.format(**subs) if isinstance(v, str) else v

    cases: list[VerifierCase] = []
    for row in data.get("cases", []):
        cases.append(
            VerifierCase(
                name=row["name"],
                label=row["label"],
                check=row["check"],
                expected_pass=bool(row["expected_pass"]),
                content=_fmt(row.get("content")),
                marker=_fmt(row.get("marker")),
                require=row.get("require"),
                claimed_ok=bool(row.get("claimed_ok", True)),
            )
        )
    return cases


# --- PM decomposition goals -------------------------------------------------


def load_pm_goals(path: Optional[str] = None) -> list[str]:
    """Load the labeled PM decomposition goals from ``pm_goals.yaml``."""
    p = Path(path) if path else CORPUS_DIR / "pm_goals.yaml"
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return list(data.get("goals", []))


# --- Judge rubrics ----------------------------------------------------------


@dataclass(frozen=True)
class Rubric:
    """A scoring contract handed to the swappable judge (:mod:`evals.judge`).

    ``criteria`` is the checklist the judge scores the item against; ``score >=
    pass_threshold`` maps to a ``pass`` verdict. Sent verbatim to the model — the
    same rubric feeds the dryrun judge today and a real judge at go-live.
    """

    id: str
    description: str
    criteria: list[str] = field(default_factory=list)
    pass_threshold: float = 0.5


def load_rubrics(path: Optional[str] = None) -> dict[str, Rubric]:
    """Load all judge rubrics from ``rubrics.yaml`` keyed by rubric id."""
    p = Path(path) if path else CORPUS_DIR / "rubrics.yaml"
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    out: dict[str, Rubric] = {}
    for key, row in (data.get("rubrics") or {}).items():
        rid = row.get("id", key)
        out[rid] = Rubric(
            id=rid,
            description=(row.get("description") or "").strip(),
            criteria=list(row.get("criteria") or []),
            pass_threshold=float(row.get("pass_threshold", 0.5)),
        )
    return out


def load_rubric(rubric_id: str, path: Optional[str] = None) -> Rubric:
    """Load a single rubric by id (raises ``KeyError`` if absent)."""
    rubrics = load_rubrics(path)
    if rubric_id not in rubrics:
        raise KeyError(f"rubric {rubric_id!r} not found (have: {sorted(rubrics)})")
    return rubrics[rubric_id]


__all__ = [
    "CORPUS_DIR",
    "VerifierCase",
    "Rubric",
    "load_verifier_cases",
    "load_pm_goals",
    "load_rubrics",
    "load_rubric",
]
