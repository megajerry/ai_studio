"""AI Studio — Evaluation Harness v1 (the empirical quality framework).

The stakeholder asked: *"do we have an empirical way to understand component
quality?"* This package is the answer. It turns "the platform feels good" into
**numbers you can re-run**, honest about what is measurable **now** (everything is
dry-run/keyless) vs what needs real models at go-live.

What it measures NOW (dry-run, keyless, no secrets):

- **Seeded-defect Verifier precision/recall** (:mod:`evals.verifier_eval`) — a
  labeled set of (artifact, criterion, expected pass/fail) with known-GOOD and
  deliberately-planted known-BAD artifacts (a missing success marker; a
  wrong-duration / missing-captions clip for a ``video_audit``-style domain
  checker). Runs the REAL Verifier evidence gate over them and computes a
  confusion matrix + precision/recall/F1 — empirical proof the gate catches
  defects (and does not false-alarm on good work).
- **PM decomposition structural eval** (:mod:`evals.pm_eval`) — runs the (dry-run)
  PM on labeled goals and scores structural properties of the plan it produces:
  >=1 work item, every item carries a checkable criterion, the dependency graph is
  acyclic, and dependencies reference real sibling items.
- **Telemetry quality rollup** (:mod:`runtime.quality`) — reads the live event log
  / ``task_transitions`` to compute per-workstream ops/quality metrics (task
  success rate, verify pass-rate, re-kick rate, avg latency + cost per completed
  task, error rate).
- **Coverage** — wired via ``pytest-cov`` (see ``docs/evaluation.md`` /
  ``make coverage``).

What is DEFERRED to go-live (documented, not faked): real-model golden-set and
LLM-as-judge OUTCOME evals, and real-integration smoke (Docker/Qdrant/live
providers/WhatsApp). See ``docs/evaluation.md``.

Entrypoint::

    python -m evals            # run every eval, print the metrics
    python -m evals --json out.json --markdown out.md   # also write a report
"""

from __future__ import annotations

from .metrics import Confusion, confusion_from_labels

__all__ = ["Confusion", "confusion_from_labels"]
