"""Human remote ops control-plane (ADR-0033).

The SECURITY-CRITICAL properties, asserted here:

- **token-gated** — ``/ops`` is 401 without the control-plane token (fail-closed);
- **human fast-path ONLY** — the conversational LLM (``handle_conversation``) can
  NEVER invoke ops; the leading ``ops`` verb is parsed BEFORE the model and only
  on an authorized channel;
- **correct argv** — each named op + the arbitrary ``docker`` passthrough build
  the expected docker/compose command;
- **destructive-confirm** — volume-delete / prune / force-rm / postgres-stop are
  blocked without an explicit ``confirm``;
- **leak-free audit** — every attempt emits ``ops.invoked`` with a REDACTED argv
  and NO secrets / stdout / stderr in the payload.

No test touches a real docker daemon: the runner is always mocked.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from runtime.enforce import MemoryEventSink
from runtime.event_types import EVENT_OPS_INVOKED
from spokesman import ops as ops_mod
from spokesman.app import create_app, handle_inbound_command
from spokesman.config import Settings
from spokesman.ops import CompletedRun, OpsResult, run_ops
from spokesman.state import InboundMessage

from .conftest import API_TOKEN, make_settings


# --- Test doubles -----------------------------------------------------------


class RecordingRunner:
    """A mock docker runner: records the argv + timeout, never runs anything."""

    def __init__(self, exit_code: int = 0, stdout: str = "", stderr: str = "",
                 timed_out: bool = False) -> None:
        self.calls: list[list[str]] = []
        self.timeouts: list[float] = []
        self._result = CompletedRun(
            exit_code=exit_code, stdout=stdout, stderr=stderr, timed_out=timed_out
        )

    def __call__(self, argv, timeout):  # type: ignore[no-untyped-def]
        self.calls.append(list(argv))
        self.timeouts.append(timeout)
        return self._result


class FakeClient:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send_text(self, text: str, to=None):  # type: ignore[no-untyped-def]
        self.sent.append(text)
        return {"ok": True}


def _raising_connect():
    # No DB in unit tests → ops falls back to a NullEventSink (still runs).
    raise RuntimeError("no db in test")


def _msg(text: str) -> InboundMessage:
    return InboundMessage(
        message_id="t", sender="15550001111", text=text, timestamp=""
    )


# --- (c) correct argv for each named op + arbitrary docker passthrough -------

COMPOSE = ["docker", "compose", "--profile", "runtime", "--profile", "spokesman",
           "--profile", "gateway"]


@pytest.mark.parametrize(
    "tokens,expected",
    [
        (["worker", "start"], COMPOSE + ["up", "-d", "worker"]),
        (["worker", "stop"], COMPOSE + ["stop", "worker"]),
        (["worker", "status"], COMPOSE + ["ps", "worker"]),
        (["worker", "scale", "3"], COMPOSE + ["up", "-d", "--scale", "worker=3", "worker"]),
        (["ps"], COMPOSE + ["ps"]),
        (["logs", "worker"], COMPOSE + ["logs", "--no-color", "--tail", "200", "worker"]),
        (["restart", "spokesman"], COMPOSE + ["restart", "spokesman"]),
        (["up", "scheduler"], COMPOSE + ["up", "-d", "scheduler"]),
        (["docker", "ps", "-a"], ["docker", "ps", "-a"]),
    ],
)
def test_named_ops_build_correct_argv(tokens, expected) -> None:
    assert ops_mod.build_op(tokens).argv == expected


def test_unknown_op_is_rejected_not_run() -> None:
    runner = RecordingRunner()
    result = run_ops(["frobnicate"], identity="x", runner=runner, sink=MemoryEventSink())
    assert result.ok is False
    assert runner.calls == []  # never executed


def test_invalid_service_name_rejected() -> None:
    with pytest.raises(ops_mod.OpsError):
        ops_mod.build_op(["logs", "--rm"])  # leading dash can't be a service


def test_scale_requires_integer() -> None:
    with pytest.raises(ops_mod.OpsError):
        ops_mod.build_op(["worker", "scale", "lots"])


# --- (c bis) per-op timeout: build/up-triggering ops get the long budget -----
# The compose `runtime` services have `build:` with no prebuilt `image:`, so the
# FIRST up/start/scale/restart can build for minutes; the quick-op timeout would
# spuriously return exit 124 / "TIMED OUT" on exactly the remote-start path.


@pytest.mark.parametrize(
    "tokens",
    [
        ["worker", "start"],
        ["worker", "scale", "3"],
        ["up", "scheduler"],
        ["restart", "spokesman"],
    ],
)
def test_build_triggering_ops_use_long_timeout(tokens) -> None:
    op = ops_mod.build_op(tokens)
    assert ops_mod.timeout_for(op) == ops_mod.BUILD_TIMEOUT_S
    runner = RecordingRunner()
    run_ops(tokens, identity="x", runner=runner, sink=MemoryEventSink())
    # The long build budget is what actually reaches the runner (both call sites
    # go through run_ops with no explicit timeout).
    assert runner.timeouts == [ops_mod.BUILD_TIMEOUT_S]


@pytest.mark.parametrize(
    "tokens",
    [
        ["ps"],
        ["logs", "worker"],
        ["worker", "status"],
        ["worker", "stop"],
        ["docker", "ps", "-a"],
    ],
)
def test_quick_ops_use_default_timeout(tokens) -> None:
    op = ops_mod.build_op(tokens)
    assert ops_mod.timeout_for(op) == ops_mod.DEFAULT_TIMEOUT_S
    runner = RecordingRunner()
    run_ops(tokens, identity="x", runner=runner, sink=MemoryEventSink())
    assert runner.timeouts == [ops_mod.DEFAULT_TIMEOUT_S]


def test_explicit_timeout_overrides_per_op_default() -> None:
    runner = RecordingRunner()
    run_ops(["worker", "start"], identity="x", runner=runner,
            sink=MemoryEventSink(), timeout=5.0)
    assert runner.timeouts == [5.0]


def test_long_timeout_is_meaningfully_longer_than_quick() -> None:
    # Sanity: the build budget must be well beyond the ~60s quick timeout that
    # was tripping the cold-start use case.
    assert ops_mod.BUILD_TIMEOUT_S >= 300.0
    assert ops_mod.BUILD_TIMEOUT_S > ops_mod.DEFAULT_TIMEOUT_S


def test_timed_out_render_is_clear() -> None:
    runner = RecordingRunner(exit_code=124, timed_out=True)
    result = run_ops(["worker", "start"], identity="x", runner=runner,
                     sink=MemoryEventSink())
    rendered = result.render()
    assert "TIMED OUT" in rendered
    assert "ops ps" in rendered  # tells the human how to check the real state


# --- (d) destructive ops require confirm ------------------------------------


@pytest.mark.parametrize(
    "tokens",
    [
        ["docker", "system", "prune", "-f"],
        ["docker", "compose", "down", "-v"],
        ["docker", "rm", "-f", "x"],
        ["docker", "stop", "postgres"],
    ],
)
def test_destructive_blocked_without_confirm(tokens) -> None:
    runner = RecordingRunner()
    sink = MemoryEventSink()
    result = run_ops(tokens, identity="x", confirm=False, runner=runner, sink=sink)
    assert result.needs_confirm is True
    assert result.ok is False
    assert runner.calls == []  # NOTHING ran — a single message cannot destroy
    # The blocked attempt is still audited.
    assert sink.types() == [EVENT_OPS_INVOKED]
    assert sink.events[0].payload["blocked"] is True
    assert sink.events[0].payload["destructive"] is True


def test_destructive_runs_with_confirm() -> None:
    runner = RecordingRunner()
    result = run_ops(
        ["docker", "system", "prune", "-f"], identity="x", confirm=True,
        runner=runner, sink=MemoryEventSink(),
    )
    assert result.destructive is True
    assert result.needs_confirm is False
    assert len(runner.calls) == 1  # confirmed → executed


def test_confirm_token_parsed_from_message_form() -> None:
    tokens, confirm = ops_mod.parse_confirm(["docker", "system", "prune", "-f", "confirm"])
    assert confirm is True
    assert tokens == ["docker", "system", "prune", "-f"]


# --- (d bis) security-review bypasses that MUST now require confirm ----------
# Before the fix `classify_destructive` matched exact tokens, so these slipped
# past the confirm gate and ran silently. Each must now be blocked without
# confirm and run only with it.

_BYPASSES = [
    # 1. Stop/kill/restart/rm the DB by its REAL container name (compose project
    #    is `ai-studio` → container `ai-studio-postgres-1`), not the bare token.
    ["docker", "stop", "ai-studio-postgres-1"],
    ["docker", "kill", "ai-studio-postgres-1"],
    ["docker", "restart", "ai-studio-postgres-1"],
    ["docker", "rm", "ai-studio-postgres-1"],
    # 2. Bundled short flags smuggling `-f` (`-fv`, `-rf`).
    ["docker", "rm", "-fv", "c"],
    ["docker", "rm", "-rf", "c"],
    # 3. Arbitrary bind/volume mount on run/create (all flag forms).
    ["docker", "run", "-v", "vol:/x", "img"],
    ["docker", "run", "--volume", "vol:/x", "img"],
    ["docker", "run", "--volume=vol:/x", "img"],
]


@pytest.mark.parametrize("tokens", _BYPASSES)
def test_review_bypass_now_requires_confirm(tokens) -> None:
    assert ops_mod.classify_destructive(tokens)[0] is True  # classified destructive
    runner = RecordingRunner()
    blocked = run_ops(tokens, identity="x", confirm=False, runner=runner, sink=MemoryEventSink())
    assert blocked.needs_confirm is True and blocked.ok is False
    assert runner.calls == []  # NOTHING ran without confirm (was: ran silently)


@pytest.mark.parametrize("tokens", _BYPASSES)
def test_review_bypass_runs_with_confirm(tokens) -> None:
    runner = RecordingRunner()
    ok = run_ops(tokens, identity="x", confirm=True, runner=runner, sink=MemoryEventSink())
    assert ok.destructive is True and ok.needs_confirm is False
    assert len(runner.calls) == 1  # confirmed → executed


@pytest.mark.parametrize(
    "tokens",
    [
        ["docker", "compose", "down"],
        ["docker", "compose", "down", "-v"],
        ["docker", "compose", "down", "--volumes=true"],
        ["docker", "volume", "rm", "x"],
        ["docker", "system", "prune", "-f"],
        ["docker", "stop", "postgres"],  # bare service token still caught
    ],
)
def test_previously_blocked_still_blocked(tokens) -> None:
    assert ops_mod.classify_destructive(tokens)[0] is True


@pytest.mark.parametrize(
    "tokens",
    [
        ["worker", "start"],
        ["worker", "stop"],  # stopping the WORKER is not critical → no confirm
        ["worker", "scale", "3"],
        ["restart", "spokesman"],  # restarting a non-critical svc is fine
        ["up", "scheduler"],
        ["ps"],
        ["logs", "worker"],
        ["docker", "ps", "-a"],
        ["docker", "logs", "worker"],
    ],
)
def test_non_critical_ops_stay_non_destructive(tokens) -> None:
    argv = ops_mod.build_op(tokens).argv
    assert ops_mod.classify_destructive(argv)[0] is False


# --- (e) audit event is emitted with NO secrets -----------------------------


def test_audit_event_redacts_secrets_and_omits_output() -> None:
    runner = RecordingRunner(stdout="TOP-SECRET-STDOUT", stderr="")
    sink = MemoryEventSink()
    result = run_ops(
        # Two secret vectors: an `-e` env flag AND a bare KEY=VALUE token.
        ["docker", "run", "-e", "sk-env-secret", "ANTHROPIC_API_KEY=sk-inline", "img"],
        identity="whatsapp:••1111", runner=runner, sink=sink,
    )
    assert sink.types() == [EVENT_OPS_INVOKED]
    payload = sink.events[0].payload
    # No secret VALUE appears anywhere in the audit payload.
    blob = str(payload)
    assert "sk-env-secret" not in blob and "sk-inline" not in blob
    assert "<redacted>" in payload["argv"]  # -e value redacted whole
    assert "ANTHROPIC_API_KEY=<redacted>" in payload["argv"]  # inline value redacted
    assert payload["identity"] == "whatsapp:••1111"
    # Command output is returned to the human but NEVER written to the log.
    assert "stdout" not in payload and "stderr" not in payload
    assert "TOP-SECRET-STDOUT" not in blob
    assert "TOP-SECRET-STDOUT" in result.render()  # ... but the human sees it


# --- (a) token-gating on the /ops endpoint ----------------------------------


def _client(settings: Settings) -> TestClient:
    return TestClient(create_app(settings, connect=_raising_connect))


def test_ops_endpoint_rejects_without_token(settings: Settings) -> None:
    assert _client(settings).post("/ops", json={"args": "ps"}).status_code == 401


def test_ops_endpoint_rejects_wrong_token(settings: Settings) -> None:
    resp = _client(settings).post(
        "/ops", json={"args": "ps"}, headers={"X-Spokesman-Token": "nope"}
    )
    assert resp.status_code == 401


def test_ops_endpoint_fails_closed_when_token_unset(state_dir: Path) -> None:
    settings = make_settings(state_dir, api_token="")
    resp = TestClient(create_app(settings, connect=_raising_connect)).post(
        "/ops", json={"args": "ps"}, headers={"X-Spokesman-Token": "anything"}
    )
    assert resp.status_code == 401


def test_ops_endpoint_constructs_command(settings: Settings, monkeypatch) -> None:
    runner = RecordingRunner(exit_code=0, stdout="ok")
    monkeypatch.setattr(ops_mod, "_subprocess_runner", runner)
    resp = _client(settings).post(
        "/ops", json={"args": "worker start"},
        headers={"X-Spokesman-Token": API_TOKEN},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["action"] == "worker.start"
    assert runner.calls == [COMPOSE + ["up", "-d", "worker"]]


def test_ops_endpoint_destructive_needs_confirm(settings: Settings, monkeypatch) -> None:
    runner = RecordingRunner()
    monkeypatch.setattr(ops_mod, "_subprocess_runner", runner)
    client = _client(settings)
    # Without confirm → blocked, nothing runs.
    r1 = client.post(
        "/ops", json={"args": "docker system prune -f"},
        headers={"X-Spokesman-Token": API_TOKEN},
    )
    assert r1.json()["needs_confirm"] is True
    assert runner.calls == []
    # With confirm → runs.
    r2 = client.post(
        "/ops", json={"args": "docker system prune -f", "confirm": True},
        headers={"X-Spokesman-Token": API_TOKEN},
    )
    assert r2.json()["ok"] is True
    assert len(runner.calls) == 1


def test_ops_endpoint_honors_trailing_confirm_token(
    settings: Settings, monkeypatch
) -> None:
    """Confirm consistency with the chat fast-path: a trailing ``confirm`` token
    in ``args`` confirms the op (and is stripped from the docker argv), instead
    of being passed through to docker as a stray arg."""
    runner = RecordingRunner()
    monkeypatch.setattr(ops_mod, "_subprocess_runner", runner)
    client = _client(settings)
    resp = client.post(
        "/ops",
        json={"args": "docker system prune -f confirm"},  # no JSON confirm field
        headers={"X-Spokesman-Token": API_TOKEN},
    )
    body = resp.json()
    assert body["ok"] is True  # confirmed via the trailing token → ran
    assert body["needs_confirm"] is False
    # `confirm` was consumed as the confirm signal, NOT passed to docker.
    assert runner.calls == [["docker", "system", "prune", "-f"]]
    assert "confirm" not in runner.calls[0]


def test_ops_endpoint_confirm_field_still_works(
    settings: Settings, monkeypatch
) -> None:
    """The JSON ``confirm`` field remains a valid confirm source (either works)."""
    runner = RecordingRunner()
    monkeypatch.setattr(ops_mod, "_subprocess_runner", runner)
    resp = _client(settings).post(
        "/ops",
        json={"args": "docker system prune -f", "confirm": True},
        headers={"X-Spokesman-Token": API_TOKEN},
    )
    assert resp.json()["ok"] is True
    assert runner.calls == [["docker", "system", "prune", "-f"]]


# --- (b) human fast-path ONLY: the LLM can NEVER invoke ops ------------------


def test_ops_fastpath_intercepts_before_the_model(settings: Settings, monkeypatch) -> None:
    """`ops ...` is handled by the deterministic fast-path, never the model."""
    runner = RecordingRunner(exit_code=0, stdout="started")
    monkeypatch.setattr(ops_mod, "_subprocess_runner", runner)

    def _boom(*a, **k):  # handle_conversation must NOT be reached
        raise AssertionError("LLM path must not run for an ops command")

    monkeypatch.setattr("spokesman.converse.handle_conversation", _boom)

    client = FakeClient()
    result = handle_inbound_command(
        settings, client, _raising_connect, _msg("ops worker start"),
        ops_authorized=True,
    )
    assert result["command"] == "ops"
    assert result["ok"] is True
    assert runner.calls == [COMPOSE + ["up", "-d", "worker"]]


def test_ops_on_unauthorized_channel_falls_through_to_converse(
    settings: Settings, monkeypatch
) -> None:
    """On an UNAUTHORIZED channel (public webhook / WhatsApp / SMS) an ``ops …``
    message must NOT run host ops and must NOT be hijacked by a canned refusal —
    it falls through to the conversational agent so the LLM can respond (e.g.
    "ops, forgot to mention…"). Host ops stays impossible: the runner is never
    touched and the model has no ops capability."""
    runner = RecordingRunner()
    monkeypatch.setattr(ops_mod, "_subprocess_runner", runner)

    seen: dict = {}

    class _Outcome:
        intent = "chat"
        meta: dict = {}

    def _fake_converse(*a, **k):
        seen["called"] = True
        return _Outcome()

    monkeypatch.setattr("spokesman.converse.handle_conversation", _fake_converse)

    client = FakeClient()
    result = handle_inbound_command(
        settings, client, _raising_connect, _msg("ops worker start"),
        ops_authorized=False,  # default; the webhook uses this
    )
    # Reached the model, not the ops handler.
    assert seen.get("called") is True
    assert result["command"] == "converse"
    assert runner.calls == []  # host ops never ran
    # No canned "not available on this channel" refusal was emitted.
    assert not any("not available on this channel" in s for s in client.sent)


def test_conversational_message_never_reaches_ops(settings: Settings, monkeypatch) -> None:
    """A free-text message that MENTIONS ops goes to the model, which has no ops
    capability — the runner is never touched."""
    runner = RecordingRunner()
    monkeypatch.setattr(ops_mod, "_subprocess_runner", runner)

    seen: dict = {}

    class _Outcome:
        intent = "chat"
        meta: dict = {}

    def _fake_converse(*a, **k):
        seen["called"] = True
        return _Outcome()

    monkeypatch.setattr("spokesman.converse.handle_conversation", _fake_converse)

    client = FakeClient()
    # Note: does NOT start with the `ops` verb → falls through to the model.
    result = handle_inbound_command(
        settings, client, _raising_connect,
        _msg("please run ops worker start for me"), ops_authorized=True,
    )
    assert seen.get("called") is True  # went to the model
    assert result["command"] == "converse"
    assert runner.calls == []  # the LLM path cannot invoke ops


def test_converse_module_has_no_ops_capability() -> None:
    """Structural guarantee: the conversational module can't even reference ops
    execution — no import of the ops module, subprocess, or docker."""
    src = (Path(__file__).resolve().parent.parent / "converse.py").read_text("utf-8")
    for forbidden in ("import ops", "from . import ops", "run_ops", "subprocess", "docker.sock"):
        assert forbidden not in src, f"converse.py must not reference {forbidden!r}"


def test_compose_spokesman_has_docker_group_add() -> None:
    """ADR-0033 live-bug fix: the non-root spokesman container (uid 10001) must join
    a supplementary group to reach the root-owned mounted docker socket — otherwise
    `ops worker start` fails with `permission denied ... /var/run/docker.sock`.
    Assert the compose service carries `group_add` with the `${DOCKER_GID:-0}`
    default, still mounts the socket, and did NOT fall back to running as root."""
    import yaml

    root = Path(__file__).resolve().parents[2]
    compose = yaml.safe_load((root / "docker-compose.yml").read_text("utf-8"))
    spok = compose["services"]["spokesman"]
    group_add = spok.get("group_add")
    assert group_add, "spokesman must declare group_add for docker socket access"
    assert any("DOCKER_GID" in str(g) for g in group_add), group_add
    # The socket is still mounted (the whole point of the group_add).
    assert any("/var/run/docker.sock" in str(v) for v in spok.get("volumes", []))
    # Container stays NON-root — no `user: root` workaround.
    assert "user" not in spok


def test_ops_result_render_is_bounded() -> None:
    big = "x" * 10000
    res = OpsResult(ok=True, action="docker", argv=["docker", "ps"], exit_code=0,
                    stdout=ops_mod._truncate(big))
    assert "truncated" in res.render()
