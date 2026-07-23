"""Swappable LLM-as-judge — the real-outcome eval mechanism (harness v2).

This is the mechanism the stakeholder demanded be *in place NOW even though real
models aren't running*: an LLM-as-judge that scores an item against a rubric and
returns a verdict/score, going through the ONE instrumented call site
(:func:`runtime.model.call.call_model`, ADR-0012). Today it runs against the
**dryrun provider** (deterministic, keyless); when a provider key lands it swaps to
a real model with **ZERO code change** — only provider selection changes (unset
``MODELS_DRY_RUN`` / a key becomes present, and ``call_model`` routes to the real
adapter). The judge code, the rubrics, and the report shape are all identical.

How the swap is zero-code:

- The judge always sends the SAME prompt (rubric criteria + item) and always tries
  to parse a JSON verdict ``{"verdict","score","rationale"}`` from the completion.
- A REAL model returns that JSON → the real path runs.
- The DRYRUN model returns a deterministic stub (not JSON) → the judge falls back to
  a **deterministic** score derived from the stub, so CI gets a stable verdict. This
  fallback is honest: it is flagged ``dry_run=True`` and is a MECHANISM signal, not a
  real quality estimate (a dryrun model cannot judge real quality).

Record/replay: an optional :class:`evals.replay.Cassette` lets a real judge run be
recorded once and replayed deterministically in CI (dryrun needs none).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Optional

from runtime.model.call import call_model

from .corpus import Rubric
from .replay import RECORD, REPLAY, Cassette, RecordedCompletion, request_key

#: Role + task_type the judge presents to the router/telemetry (ADR-0012).
JUDGE_ROLE = "judge"
JUDGE_TASK_TYPE = "judge"

_JSON_OBJ_RE = re.compile(r"\{.*\}", re.DOTALL)


@dataclass(frozen=True)
class JudgeVerdict:
    """The judge's answer for one item against one rubric.

    ``dry_run`` distinguishes a deterministic dryrun stub verdict (mechanism signal)
    from a real model's judgment (outcome signal) — so the report never presents a
    dryrun verdict as a real quality estimate.
    """

    rubric_id: str
    passed: bool
    score: float
    rationale: str
    provider: str
    dry_run: bool
    raw_text: str

    def to_dict(self) -> dict:
        return {
            "rubric_id": self.rubric_id,
            "passed": self.passed,
            "score": round(self.score, 4),
            "rationale": self.rationale,
            "provider": self.provider,
            "dry_run": self.dry_run,
        }


def item_key(item: dict) -> str:
    """A stable, id-free content key for an item (for deterministic dryrun scores).

    Canonicalizes the item to sorted-key JSON so the SAME logical content yields the
    same key across runs — callers should exclude volatile ids (e.g. UUIDs) from the
    item so the dryrun score is reproducible."""
    return json.dumps(item, sort_keys=True, default=str, ensure_ascii=True)


def build_messages(rubric: Rubric, item: dict) -> list[dict]:
    """Build the chat messages sent to the judge model (dryrun or real).

    The prompt is model-agnostic: it states the rubric criteria and the item, and
    asks for a strict JSON verdict. A real model answers it directly; the dryrun
    model ignores it and returns a stub (handled by the deterministic fallback)."""
    criteria = "\n".join(f"- {c}" for c in rubric.criteria)
    system = (
        "You are a strict evaluation judge. Score the ITEM against the RUBRIC "
        "criteria. Respond with ONLY a JSON object of the form "
        '{"verdict": "pass"|"fail", "score": <0..1>, "rationale": "<short>"}. '
        f"Return verdict 'pass' iff score >= {rubric.pass_threshold}."
    )
    user = (
        f"RUBRIC ({rubric.id}): {rubric.description}\n"
        f"CRITERIA:\n{criteria}\n\n"
        f"ITEM:\n{json.dumps(item, indent=2, default=str, ensure_ascii=False)}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


class Judge:
    """Scores an item against a rubric via :func:`call_model` (dryrun today).

    ``cassette`` (optional) records/replays real-model I/O for reproducible CI; with
    no cassette it calls straight through (the keyless default). ``force_dry_run``
    forces the dryrun provider regardless of env (used by tests)."""

    def __init__(
        self,
        *,
        role: str = JUDGE_ROLE,
        task_type: str = JUDGE_TASK_TYPE,
        cassette: Optional[Cassette] = None,
        force_dry_run: bool = False,
    ) -> None:
        self.role = role
        self.task_type = task_type
        self.cassette = cassette
        self.force_dry_run = force_dry_run

    # --- the call, wrapped by the record/replay seam ------------------------

    def _complete(
        self,
        rubric: Rubric,
        messages: list[dict],
        *,
        conn: Any,
        workstream: str,
        task_id: Any,
    ) -> tuple[str, str]:
        """Return ``(text, provider)`` for the judge call, honoring the cassette.

        REPLAY returns the recorded response (a miss raises — a reproducible run may
        not silently escalate to a live call). RECORD calls through then stores the
        completion. Otherwise (no cassette / OFF) it calls straight through."""
        # A stable logical model id for cassette keying (routing is deterministic).
        key = request_key(f"{self.role}:{rubric.id}", messages)

        if self.cassette is not None and self.cassette.mode == REPLAY:
            rec = self.cassette.replay(key)
            return rec.text, rec.provider

        completion = call_model(
            self.role,
            self.task_type,
            messages,
            conn=conn,
            workstream=workstream,
            task_id=task_id,
            force_dry_run=self.force_dry_run,
        )
        if self.cassette is not None and self.cassette.mode == RECORD:
            self.cassette.put(key, RecordedCompletion.from_completion(completion))
        return completion.text, completion.provider

    # --- scoring ------------------------------------------------------------

    def score(
        self,
        rubric: Rubric,
        item: dict,
        *,
        conn: Any = None,
        workstream: str = "eval-judge",
        task_id: Any = None,
    ) -> JudgeVerdict:
        """Score ``item`` against ``rubric`` and return a :class:`JudgeVerdict`."""
        messages = build_messages(rubric, item)
        text, provider = self._complete(
            rubric, messages, conn=conn, workstream=workstream, task_id=task_id
        )

        parsed = self._parse_json_verdict(text)
        if parsed is not None:
            # --- real-model path: trust the model's JSON verdict ---
            score = float(parsed.get("score", 0.0))
            verdict = str(parsed.get("verdict", "")).strip().lower()
            passed = verdict == "pass" if verdict in {"pass", "fail"} else (
                score >= rubric.pass_threshold
            )
            rationale = str(parsed.get("rationale", "")).strip() or "(no rationale)"
            return JudgeVerdict(
                rubric_id=rubric.id, passed=passed, score=score,
                rationale=rationale, provider=provider, dry_run=False, raw_text=text,
            )

        # --- dryrun fallback: deterministic stub score (mechanism signal) ---
        score = self._deterministic_score(rubric, item, text)
        passed = score >= rubric.pass_threshold
        return JudgeVerdict(
            rubric_id=rubric.id,
            passed=passed,
            score=score,
            rationale=(
                f"[dryrun-judge] deterministic score {score} vs threshold "
                f"{rubric.pass_threshold} (mechanism only — not a real quality signal)"
            ),
            provider=provider,
            dry_run=True,
            raw_text=text,
        )

    @staticmethod
    def _parse_json_verdict(text: str) -> Optional[dict]:
        """Extract a ``{...}`` verdict object from ``text``, or ``None`` if none.

        Tries the whole string then the first ``{...}`` span, so a real model that
        wraps its JSON in prose still parses. A dryrun stub has no object → ``None``
        → the deterministic fallback runs."""
        candidates = [text]
        match = _JSON_OBJ_RE.search(text or "")
        if match:
            candidates.append(match.group(0))
        for candidate in candidates:
            if not candidate:
                continue
            try:
                obj = json.loads(candidate)
            except (ValueError, TypeError):
                continue
            if isinstance(obj, dict) and ("score" in obj or "verdict" in obj):
                return obj
        return None

    @staticmethod
    def _deterministic_score(rubric: Rubric, item: dict, text: str) -> float:
        """A stable [0,1] score derived from the rubric+item+dryrun stub.

        Deterministic (same inputs → same score) so CI is reproducible; keyed partly
        on the dryrun completion ``text`` so it is provably tied to the model call
        that happened. NOT a real quality estimate — flagged ``dry_run`` upstream."""
        h = hashlib.sha256(
            f"{rubric.id}|{item_key(item)}|{text}".encode("utf-8")
        ).hexdigest()
        return round(int(h[:8], 16) / 0xFFFFFFFF, 4)


__all__ = [
    "JUDGE_ROLE",
    "JUDGE_TASK_TYPE",
    "JudgeVerdict",
    "Judge",
    "build_messages",
    "item_key",
]
