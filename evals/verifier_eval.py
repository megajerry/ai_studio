"""Seeded-defect eval for the Verifier evidence gate (ADR-0014).

We build a **labeled** corpus of ``(artifact, criterion, expected pass/fail)``
cases — known-GOOD work and deliberately-planted known-BAD work — then run the
REAL :func:`runtime.roles.verifier.verify` gate over each and score it as a
binary defect classifier (:mod:`evals.metrics`). This is the empirical proof that
the evidence gate actually catches defects rather than trusting the Executor's
``ok`` claim.

Two checker families are exercised, both through the pluggable checker seam
(:mod:`runtime.roles.checkers`) so no Verifier/vertical code is touched:

- the horizontal ``marker`` checker (a success marker must be present in the
  artifact) — including a **hallucinated-success** defect (the Executor claims
  ``ok=True`` but the real artifact lacks the marker: evidence must beat the
  claim);
- a reference ``video_audit`` domain checker (defined here, registered on a fresh
  registry) with wrong-duration and missing-captions defects — mirroring the
  ADR-0014 example of a vertical injecting its own evidence check.

Runs dry-run + keyless: the Verifier's model call is a dry-run completion; the
verdict is decided by the deterministic evidence check. Needs no DB (``conn`` may
be ``None``); it only needs a scratch dir to write the artifacts into and the
policy-gated ``fs.read`` seam to re-read them.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from runtime.enforce import InvokeStatus
from runtime.models import Task, TaskStatus
from runtime.policy import load_policy
from runtime.roles.checkers import CheckResult, CheckerRegistry, marker_check
from runtime.roles.executor import ExecutorResult
from runtime.roles.verifier import verify
from runtime.tools import ToolRegistry
from runtime.tools.filesystem import FilesystemTool

from .corpus import VerifierCase, load_verifier_cases
from .metrics import Confusion, confusion_from_labels
from .stats import Rate, rate

# --- reference domain checker: video_audit (evidence over claims) ------------


def _parse_kv(text: str) -> dict:
    """Parse ``key: value`` lines from a clip artifact into a dict (lenient)."""
    out: dict = {}
    for line in text.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            out[k.strip().lower()] = v.strip()
    return out


def _to_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def video_audit_check(
    conn: Any, task: Task, artifact_ref: Any, require: Any
) -> CheckResult:
    """A ``video_audit``-style checker: re-read the clip artifact and check its
    OBSERVED duration + captions against ``require`` (``min_duration`` /
    ``max_duration`` / ``require_captions``).

    The verdict rests on the artifact's real contents (evidence), never the
    Executor's ``ok`` claim (ADR-0014). This is a reference domain checker living in
    the eval package — it demonstrates the vertical checker seam without editing the
    horizontal :mod:`runtime.roles.checkers`.
    """
    require = require or {}
    if not artifact_ref.path:
        return CheckResult(passed=False, facts={"artifact": None},
                           reason="no artifact produced by executor")
    read = artifact_ref.read_text(task)
    if read.status is not InvokeStatus.EXECUTED or not (read.result and read.result.ok):
        return CheckResult(passed=False, facts={"read_status": read.status.value},
                           reason=f"could not read artifact ({read.status.value})")

    meta = _parse_kv(read.result.output or "")
    duration = _to_float(meta.get("duration_seconds"))
    captions = (meta.get("captions") or "").strip().lower()
    facts = {"duration_seconds": duration, "captions": captions or None}

    min_d = require.get("min_duration")
    max_d = require.get("max_duration")
    need_caps = bool(require.get("require_captions", True))

    reasons: list[str] = []
    if duration is None:
        reasons.append("no duration_seconds observed in artifact")
    else:
        if min_d is not None and duration < min_d:
            reasons.append(f"duration {duration}s below minimum {min_d}s")
        if max_d is not None and duration > max_d:
            reasons.append(f"duration {duration}s above maximum {max_d}s")
    if need_caps and captions != "present":
        reasons.append("captions not present")

    if reasons:
        return CheckResult(passed=False, facts=facts, reason="; ".join(reasons))
    return CheckResult(
        passed=True, facts=facts,
        reason=f"clip ok (duration={duration}s, captions={captions})",
    )


def eval_checker_registry() -> CheckerRegistry:
    """A registry with the horizontal ``marker`` check + the reference
    ``video_audit`` domain check (what the seeded-defect eval dispatches on)."""
    reg = CheckerRegistry()
    reg.register("marker", marker_check)
    reg.register("video_audit", video_audit_check)
    return reg


# --- the labeled seeded-defect corpus (loaded from data, harness v2) ---------
#
# The cases used to be hardcoded here; they now live in
# ``evals/corpus/verifier_cases.yaml`` (:class:`evals.corpus.VerifierCase` is the
# loaded type) so the corpus grows by editing DATA, not code. ``default_cases`` is
# kept as the loader entrypoint the runner + tests call.


def default_cases() -> list[VerifierCase]:
    """The labeled seeded-defect corpus, loaded from the versioned data file.

    Includes known-GOOD cases and deliberately-planted defects across both checker
    families, with two *hallucinated-success* cases (``claimed_ok=True`` on defective
    work) the gate must FAIL on evidence — the recall-critical cases that prove the
    gate is not fooled by a "done" claim (ADR-0014)."""
    return load_verifier_cases()


# --- runner ------------------------------------------------------------------


@dataclass
class VerifierEvalResult:
    """Outcome of the seeded-defect Verifier eval: the matrix + per-case detail."""

    confusion: Confusion
    cases: list[dict] = field(default_factory=list)

    def rates(self) -> list[Rate]:
        """Precision / recall / accuracy as :class:`~evals.stats.Rate`s — each with
        its sample size ``n`` + Wilson 95% CI + small-``n`` flag, so the tiny-corpus
        weakness is visible in the numbers rather than hidden behind a bare ``1.0``.

        - precision = tp / (tp + fp)   (n = flagged-as-defective)
        - recall    = tp / (tp + fn)   (n = truly-defective, the positive class)
        - accuracy  = (tp + tn) / support
        """
        cm = self.confusion
        return [
            rate("precision", cm.tp, cm.tp + cm.fp),
            rate("recall", cm.tp, cm.tp + cm.fn),
            rate("accuracy", cm.tp + cm.tn, cm.support),
        ]

    def to_dict(self) -> dict:
        return {
            "name": "verifier_seeded_defect",
            "description": (
                "Verifier evidence-gate precision/recall on labeled GOOD/BAD "
                "artifacts (positive class = defective). Rates carry n + Wilson 95% CI."
            ),
            "confusion": self.confusion.to_dict(),
            "rates": [r.to_dict() for r in self.rates()],
            "cases": self.cases,
            "passed": (
                self.confusion.recall == 1.0
                and self.confusion.precision == 1.0
                and self.confusion.support == len(self.cases)
            ),
        }


def _make_task(ws: str, case: VerifierCase) -> Task:
    now = datetime.now(timezone.utc)
    payload: dict = {"criterion": f"{case.label} :: {case.check} check"}
    if case.check == "marker":
        payload["marker"] = case.marker
    else:
        payload["check"] = {"check": case.check, "require": case.require}
    return Task(
        id=uuid4(), workstream=ws, type="work.eval", status=TaskStatus.IN_PROGRESS,
        priority=0, payload=payload, created_at=now, updated_at=now,
    )


def run_verifier_eval(
    conn: Any = None, *, scratch: Optional[str] = None
) -> VerifierEvalResult:
    """Run the labeled corpus through the REAL Verifier gate and score it.

    Writes each case's artifact into ``scratch`` (a temp dir by default), builds the
    Task + un-trusted :class:`ExecutorResult`, runs :func:`verify`, and records
    ``(expected_pass, predicted_pass)`` for the confusion matrix. Keyless
    (``MODELS_DRY_RUN``); ``conn`` may be ``None`` (the gate's model call tolerates
    it and the fs.read seam needs no DB).
    """
    os.environ.setdefault("MODELS_DRY_RUN", "1")
    scratch = scratch or tempfile.mkdtemp(prefix="ai_studio_eval_verify_")
    registry: ToolRegistry = ToolRegistry()
    registry.register(FilesystemTool(root=scratch))
    config = load_policy()
    checkers = eval_checker_registry()
    ws = f"eval-verify-{uuid4().hex[:8]}"

    labels: list[tuple[bool, bool]] = []
    cases: list[dict] = []
    from pathlib import Path

    for case in default_cases():
        artifact_path = None
        if case.content is not None:
            artifact_path = f"{case.name}.txt"
            (Path(scratch) / artifact_path).write_text(case.content, encoding="utf-8")
        task = _make_task(ws, case)
        result = ExecutorResult(
            ok=case.claimed_ok, artifact_path=artifact_path, marker=case.marker,
            invoke_status=InvokeStatus.EXECUTED.value,
            note="seeded eval artifact",
        )
        verdict = verify(
            conn, task, result, registry=registry, config=config, checkers=checkers,
        )
        labels.append((case.expected_pass, verdict.passed))
        correct = verdict.passed == case.expected_pass
        cases.append({
            "name": case.name,
            "label": case.label,
            "check": case.check,
            "claimed_ok": case.claimed_ok,
            "expected_pass": case.expected_pass,
            "predicted_pass": verdict.passed,
            "correct": correct,
            "reason": verdict.reason,
            "facts": verdict.facts,
        })

    return VerifierEvalResult(confusion=confusion_from_labels(labels), cases=cases)
