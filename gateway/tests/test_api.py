"""HTTP-level gates of the task gateway (ADR-0027) — no database needed.

Every gate asserted here fires BEFORE the handler opens a connection, so these
run anywhere: the injected ``connect`` raises, which doubles as the DB-outage
test (a 503 with a generic detail — never a DSN or driver string).
"""

from __future__ import annotations

import json
import logging

import pytest
from fastapi.testclient import TestClient

from gateway.app import create_app
from gateway.auth import (
    REASON_MISSING_SCOPE,
    REASON_NO_TOKEN,
    REASON_NO_TOKENS,
    REASON_RATE_LIMITED,
    REASON_UNKNOWN_TOKEN,
    REASON_WORKSTREAM_DENIED,
    RateLimiter,
    TokenRegistry,
)
from gateway.app import DB_UNAVAILABLE_DETAIL

from .conftest import (
    FULL_SECRET,
    PINNED_SECRET,
    PINNED_WORKSTREAM,
    READONLY_SECRET,
    bearer,
    make_settings,
)

#: A DSN-shaped string the failing connect raises with — no response may echo it.
SECRET_DSN = "postgresql://aistudio:sup3r-s3cret@10.0.0.5:5432/aistudio"


def exploding_connect():
    raise RuntimeError(f"could not connect to {SECRET_DSN}")


def _client(settings=None, *, limiter=None, connect=exploding_connect) -> TestClient:
    return TestClient(
        create_app(settings or make_settings(), connect=connect, limiter=limiter),
        raise_server_exceptions=False,
    )


#: (method, path, body) for every task endpoint, with the scope each needs.
READ_ENDPOINTS = [
    ("GET", "/v1/tasks/ready", None),
    ("GET", "/v1/tasks/waiting", None),
    ("GET", "/v1/tasks/review", None),
    ("GET", "/v1/tasks/11111111-1111-1111-1111-111111111111", None),
]
WRITE_ENDPOINTS = [
    ("POST", "/v1/tasks", {"workstream": "productivity", "type": "work.demo"}),
    ("POST", "/v1/tasks/claim", {}),
    ("POST", "/v1/tasks/11111111-1111-1111-1111-111111111111/heartbeat", {}),
    ("POST", "/v1/tasks/11111111-1111-1111-1111-111111111111/complete", {}),
]
ALL_ENDPOINTS = READ_ENDPOINTS + WRITE_ENDPOINTS


def _call(client: TestClient, method: str, path: str, body, headers=None):
    if method == "GET":
        return client.get(path, headers=headers)
    return client.request(method, path, json=body, headers=headers)


# --- Public surface ---------------------------------------------------------


def test_health_is_public_and_leaks_nothing() -> None:
    resp = _client().get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok" and body["tokens_configured"] == 3
    # The count is fine; identities/digests/tokens are not exposed.
    assert "tokens" not in body and FULL_SECRET not in resp.text


def test_whoami_reports_only_the_callers_own_grant() -> None:
    resp = _client().get("/v1/whoami", headers=bearer(READONLY_SECRET))
    assert resp.status_code == 200
    assert resp.json() == {
        "identity": "offhost-readonly",
        "scopes": ["read"],
        "workstreams": [],
        "default_workstream": None,
    }


# --- Gate 1: a token is required, and the surface fails closed --------------


@pytest.mark.parametrize("method,path,body", ALL_ENDPOINTS)
def test_no_token_is_401(method: str, path: str, body) -> None:
    resp = _call(_client(), method, path, body)
    assert resp.status_code == 401
    assert resp.json()["detail"] == REASON_NO_TOKEN


@pytest.mark.parametrize("method,path,body", ALL_ENDPOINTS)
def test_unknown_token_is_401(method: str, path: str, body) -> None:
    resp = _call(_client(), method, path, body, bearer("not-a-real-token"))
    assert resp.status_code == 401
    assert resp.json()["detail"] == REASON_UNKNOWN_TOKEN


@pytest.mark.parametrize("method,path,body", ALL_ENDPOINTS)
def test_zero_tokens_configured_fails_closed(method: str, path: str, body) -> None:
    client = _client(make_settings(tokens=TokenRegistry(())))
    resp = _call(client, method, path, body, bearer(FULL_SECRET))
    assert resp.status_code == 503
    assert resp.json()["detail"] == REASON_NO_TOKENS


# --- Gate 3: scopes ---------------------------------------------------------


@pytest.mark.parametrize("method,path,body", WRITE_ENDPOINTS)
def test_read_only_token_cannot_mutate(method: str, path: str, body) -> None:
    resp = _call(_client(), method, path, body, bearer(READONLY_SECRET))
    assert resp.status_code == 403
    assert resp.json()["detail"] == REASON_MISSING_SCOPE


@pytest.mark.parametrize("method,path,body", READ_ENDPOINTS)
def test_read_scope_passes_the_gate(method: str, path: str, body) -> None:
    # Past the gate the injected connect fails → 503, which proves the gate
    # allowed the call through (a 401/403 would have short-circuited earlier).
    resp = _call(_client(), method, path, body, bearer(READONLY_SECRET))
    assert resp.status_code == 503


# --- Gate 4: workstream pinning --------------------------------------------


def test_pinned_token_cannot_enqueue_into_another_workstream() -> None:
    resp = _client().post(
        "/v1/tasks",
        json={"workstream": "not-my-vertical", "type": "work.demo"},
        headers=bearer(PINNED_SECRET),
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == REASON_WORKSTREAM_DENIED


def test_pinned_token_cannot_read_another_workstream() -> None:
    resp = _client().get(
        f"/v1/tasks/ready?workstream=not-my-vertical", headers=bearer(PINNED_SECRET)
    )
    assert resp.status_code == 403


def test_pinned_token_may_act_on_its_own_workstream() -> None:
    resp = _client().post(
        "/v1/tasks",
        json={"workstream": PINNED_WORKSTREAM, "type": "work.demo"},
        headers=bearer(PINNED_SECRET),
    )
    assert resp.status_code == 503  # past the gate, DB is the only failure


def test_a_singly_pinned_token_need_not_restate_its_workstream() -> None:
    # Omitting the workstream resolves to the pin (as it does for read/claim),
    # so the request reaches the DB rather than being rejected as incomplete.
    resp = _client().post(
        "/v1/tasks", json={"type": "work.demo"}, headers=bearer(PINNED_SECRET)
    )
    assert resp.status_code == 503


def test_an_unpinned_token_must_name_the_workstream() -> None:
    # Nothing to infer from: refuse rather than guess a destination workstream.
    resp = _client().post(
        "/v1/tasks", json={"type": "work.demo"}, headers=bearer(FULL_SECRET)
    )
    assert resp.status_code == 422
    assert resp.json()["detail"] == "workstream_required"


def test_a_blank_workstream_is_not_a_workstream() -> None:
    resp = _client().post(
        "/v1/tasks",
        json={"workstream": "   ", "type": "work.demo"},
        headers=bearer(FULL_SECRET),
    )
    assert resp.status_code == 422


# --- Gate 5: rate limiting -------------------------------------------------


def test_rate_limited_identity_gets_429_with_retry_after() -> None:
    clock = [0.0]
    limiter = RateLimiter(rate_per_min=60, burst=2, clock=lambda: clock[0])
    client = _client(limiter=limiter)
    for _ in range(2):
        client.get("/v1/whoami", headers=bearer(FULL_SECRET))
    resp = client.get("/v1/whoami", headers=bearer(FULL_SECRET))
    assert resp.status_code == 429
    assert resp.json()["detail"] == REASON_RATE_LIMITED
    assert int(resp.headers["retry-after"]) >= 1


# --- Bounded input ---------------------------------------------------------


def test_oversize_body_is_rejected_before_parsing() -> None:
    client = _client(make_settings(max_body_bytes=512))
    resp = client.post(
        "/v1/tasks",
        content=json.dumps({"workstream": "productivity", "type": "work.demo",
                            "payload": {"blob": "x" * 4096}}),
        headers={"Content-Type": "application/json", **bearer(FULL_SECRET)},
    )
    assert resp.status_code == 413


def test_chunked_body_without_a_declared_length_is_refused() -> None:
    def chunks():
        yield b'{"workstream": "productivity", "type": "work.demo"}'

    resp = _client().post(
        "/v1/tasks", content=chunks(),
        headers={"Content-Type": "application/json", **bearer(FULL_SECRET)},
    )
    assert resp.status_code == 411


def test_oversize_payload_is_rejected_even_within_the_body_cap() -> None:
    client = _client(make_settings(max_payload_bytes=64))
    resp = client.post(
        "/v1/tasks",
        json={"workstream": "productivity", "type": "work.demo",
              "payload": {"blob": "x" * 256}},
        headers=bearer(FULL_SECRET),
    )
    assert resp.status_code == 413


@pytest.mark.parametrize(
    "body",
    [
        {"workstream": "Bad Workstream", "type": "work.demo"},
        {"workstream": "productivity", "type": "DROP TABLE tasks;--"},
        {"workstream": "productivity", "type": "work.demo", "assignee": "root"},
        {"workstream": "productivity"},                      # missing type
        {"type": "work.demo"},                               # missing workstream
    ],
)
def test_identifier_shaped_input_only(body: dict) -> None:
    resp = _client().post("/v1/tasks", json=body, headers=bearer(FULL_SECRET))
    assert resp.status_code == 422


def test_complete_rejects_a_non_terminal_status() -> None:
    resp = _client().post(
        "/v1/tasks/11111111-1111-1111-1111-111111111111/complete",
        json={"status": "in_progress"},
        headers=bearer(FULL_SECRET),
    )
    assert resp.status_code == 422


def test_a_malformed_task_id_never_reaches_the_store() -> None:
    resp = _client().get("/v1/tasks/not-a-uuid", headers=bearer(FULL_SECRET))
    assert resp.status_code == 422


# --- No leakage ------------------------------------------------------------


def test_db_outage_answers_a_generic_503(caplog) -> None:
    resp = _client().get("/v1/tasks/ready", headers=bearer(FULL_SECRET))
    assert resp.status_code == 503
    assert resp.json()["detail"] == DB_UNAVAILABLE_DETAIL
    # Neither the DSN nor the driver text may reach the client.
    assert SECRET_DSN not in resp.text and "RuntimeError" not in resp.text


def test_tokens_are_never_logged(caplog) -> None:
    caplog.set_level(logging.DEBUG)
    client = _client()
    client.get("/v1/tasks/ready", headers=bearer(FULL_SECRET))          # allowed
    client.post("/v1/tasks", json={"workstream": "productivity", "type": "work.demo"},
                headers=bearer(READONLY_SECRET))                        # 403
    client.get("/v1/tasks/ready", headers=bearer("some-guessed-token"))  # 401
    logged = "\n".join(r.getMessage() for r in caplog.records)
    for secret in (FULL_SECRET, READONLY_SECRET, "some-guessed-token"):
        assert secret not in logged
    # The identity, however, IS logged — denials must be attributable.
    assert "offhost-readonly" in logged


def test_authenticated_denials_try_to_audit_unauthenticated_ones_do_not() -> None:
    """An unauthenticated flood must not make the host open DB connections."""
    opened: list[int] = []

    def counting_connect():
        opened.append(1)
        raise RuntimeError("db down")

    client = _client(connect=counting_connect)
    client.get("/v1/tasks/ready", headers=bearer("bogus"))  # 401 → no DB touch
    assert opened == []

    client.post(
        "/v1/tasks", json={"workstream": "productivity", "type": "work.demo"},
        headers=bearer(READONLY_SECRET),
    )  # 403, identity known → one audit attempt
    assert len(opened) == 1
