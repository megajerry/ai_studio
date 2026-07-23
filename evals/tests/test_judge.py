"""Swappable LLM-as-judge tests (keyless; no DB required).

Proves (a) the dryrun judge returns a DETERMINISTIC verdict via the real
:func:`runtime.model.call.call_model` path (flagged ``dry_run`` = mechanism only),
and (b) the SAME judge code parses a real model's JSON verdict when one is supplied
via a replay cassette — i.e. the swap to a real model is provider-selection only,
zero code change.
"""

from __future__ import annotations

from evals.corpus import Rubric
from evals.judge import Judge, build_messages, item_key
from evals.replay import REPLAY, Cassette, RecordedCompletion, request_key

RUBRIC = Rubric(
    id="test_rubric",
    description="Judge whether the item is well-formed.",
    criteria=["It has a goal.", "It records a decision."],
    pass_threshold=0.5,
)
ITEM = {"goal": "ship it", "steps": [{"step_type": "decide", "choice": "B"}]}


def test_dryrun_judge_is_deterministic_and_flagged():
    judge = Judge(force_dry_run=True)
    v1 = judge.score(RUBRIC, ITEM, conn=None)
    v2 = judge.score(RUBRIC, ITEM, conn=None)
    assert v1.dry_run is True and v1.provider == "dryrun"
    assert v1.rubric_id == "test_rubric"
    # Deterministic: same inputs -> identical verdict + score across calls.
    assert v1.score == v2.score and v1.passed == v2.passed
    assert 0.0 <= v1.score <= 1.0


def test_dryrun_score_varies_with_item_content():
    judge = Judge(force_dry_run=True)
    a = judge.score(RUBRIC, {"goal": "alpha"}, conn=None)
    b = judge.score(RUBRIC, {"goal": "beta"}, conn=None)
    # Different content -> (almost surely) different deterministic score.
    assert a.score != b.score


def test_build_messages_includes_rubric_and_item():
    msgs = build_messages(RUBRIC, ITEM)
    assert msgs[0]["role"] == "system" and msgs[1]["role"] == "user"
    body = msgs[1]["content"]
    assert "test_rubric" in body
    assert "It has a goal." in body
    assert "ship it" in body


def test_real_model_json_verdict_parsed_via_replay_cassette():
    """A recorded REAL-model JSON verdict is parsed by the same judge code path
    (dry_run=False) — proving the swap needs only provider selection."""
    messages = build_messages(RUBRIC, ITEM)
    key = request_key(f"judge:{RUBRIC.id}", messages)

    cas = Cassette(mode=REPLAY)
    cas.put(
        key,
        RecordedCompletion(
            text='{"verdict": "pass", "score": 0.91, "rationale": "well-formed"}',
            input_tokens=10, output_tokens=6, cached_tokens=0,
            model_id="claude-x", provider="anthropic",
        ),
    )

    judge = Judge(cassette=cas)
    v = judge.score(RUBRIC, ITEM, conn=None)
    assert v.dry_run is False and v.provider == "anthropic"
    assert v.passed is True
    assert abs(v.score - 0.91) < 1e-9
    assert v.rationale == "well-formed"


def test_real_model_fail_verdict_is_honored():
    messages = build_messages(RUBRIC, ITEM)
    key = request_key(f"judge:{RUBRIC.id}", messages)
    cas = Cassette(mode=REPLAY)
    cas.put(
        key,
        RecordedCompletion(
            text='here is my answer: {"verdict": "fail", "score": 0.2}',
            input_tokens=1, output_tokens=1, cached_tokens=0,
            model_id="claude-x", provider="anthropic",
        ),
    )
    v = Judge(cassette=cas).score(RUBRIC, ITEM, conn=None)
    assert v.dry_run is False and v.passed is False and v.score == 0.2


def test_item_key_is_id_free_stable():
    assert item_key({"a": 1, "b": 2}) == item_key({"b": 2, "a": 1})
