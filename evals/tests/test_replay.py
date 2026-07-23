"""Record/replay (VCR) scaffold tests (pure; no DB, no model).

Proves the seam that will make real-model judge runs reproducible in CI: a recorded
completion round-trips through a JSON cassette byte-stably, request keys are stable
and order-independent, and a REPLAY miss raises loudly (never silently escalates to
a live call).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from evals.replay import (
    OFF,
    RECORD,
    REPLAY,
    Cassette,
    CassetteMiss,
    RecordedCompletion,
    request_key,
)


def _fake_completion(text="{\"verdict\": \"pass\", \"score\": 0.9}"):
    usage = SimpleNamespace(input_tokens=12, output_tokens=8, cached_tokens=0)
    return SimpleNamespace(
        text=text, usage=usage, model_id="test-model", provider="anthropic"
    )


def test_request_key_is_stable_and_order_independent():
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    k1 = request_key("m", msgs, {"a": 1, "b": 2})
    k2 = request_key("m", msgs, {"b": 2, "a": 1})  # opts key order flipped
    assert k1 == k2
    # Different content -> different key.
    assert k1 != request_key("m", msgs, {"a": 9})


def test_recorded_completion_from_completion_captures_subset():
    rec = RecordedCompletion.from_completion(_fake_completion())
    assert rec.provider == "anthropic" and rec.model_id == "test-model"
    assert rec.input_tokens == 12 and rec.output_tokens == 8


def test_cassette_record_save_load_replay_roundtrip(tmp_path):
    path = tmp_path / "judge.cassette.json"
    key = request_key("judge:r1", [{"role": "user", "content": "hi"}])

    # RECORD: store a completion, persist to disk.
    rec_cas = Cassette(str(path), mode=RECORD)
    rec_cas.put(key, RecordedCompletion.from_completion(_fake_completion()))
    rec_cas.save()
    assert path.exists()

    # REPLAY: a fresh cassette loads from disk and replays byte-stably.
    play_cas = Cassette(str(path), mode=REPLAY)
    assert len(play_cas) == 1
    got = play_cas.replay(key)
    assert got.text == "{\"verdict\": \"pass\", \"score\": 0.9}"
    assert got.provider == "anthropic"
    assert got.input_tokens == 12


def test_replay_miss_raises_loudly(tmp_path):
    cas = Cassette(str(tmp_path / "empty.json"), mode=REPLAY)
    with pytest.raises(CassetteMiss):
        cas.replay("no-such-key")


def test_unknown_mode_rejected():
    with pytest.raises(ValueError):
        Cassette(mode="bogus")


def test_off_mode_default_needs_no_path():
    cas = Cassette()
    assert cas.mode == OFF and len(cas) == 0
