"""End-to-end remote task access against a LIVE Postgres (ADR-0028).

This is the acceptance test for the stakeholder requirement: a session holding
nothing but a bearer token — **no DB credential** — can enqueue, list, claim,
heartbeat and complete real queue rows, and the canonical lifecycle + audit trail
record it. SKIPs cleanly when no DATABASE_URL is reachable.

    export DATABASE_URL=postgresql://aistudio@localhost:5432/aistudio
    python -m runtime.migrate
    pytest gateway/tests/test_gateway_db.py
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from gateway.app import DEFAULT_AGENT_TYPE, GATEWAY_WORKSTREAM, create_app
from gateway.auth import (
    REASON_NOT_OWNER,
    SCOPE_CLAIM,
    SCOPE_COMPLETE,
    SCOPE_ENQUEUE,
    SCOPE_READ,
    Token,
    TokenRegistry,
    token_digest,
)
from runtime import db
from runtime.event_types import EVENT_GATEWAY_ACCESS, EVENT_GATEWAY_DENIED
from runtime.events import read_events
from runtime.migrate import migrate
from runtime.models import Assignee, TaskStatus
from runtime.tasks import enqueue_task, get_task, task_lifecycle

from .conftest import bearer, make_settings

pytestmark = pytest.mark.skipif(
    not db.can_connect(timeout=2.0),
    reason="no reachable DATABASE_URL (expected off-host / no docker)",
)

REMOTE_SECRET = "db-test-remote-secret"
REMOTE_IDENTITY = "offhost-remote"
OTHER_SECRET = "db-test-other-secret"
OTHER_IDENTITY = "offhost-other"

_ALL = frozenset({SCOPE_READ, SCOPE_ENQUEUE, SCOPE_CLAIM, SCOPE_COMPLETE})


@pytest.fixture(scope="module")
def conn():
    c = db.connect()
    migrate(c)
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def ws() -> str:
    return f"gw-{uuid4().hex[:12]}"


def _registry(workstreams: frozenset = frozenset()) -> TokenRegistry:
    return TokenRegistry([
        Token(
            identity=REMOTE_IDENTITY, scopes=_ALL,
            digest=token_digest(REMOTE_SECRET), workstreams=workstreams,
        ),
        Token(
            identity=OTHER_IDENTITY, scopes=_ALL,
            digest=token_digest(OTHER_SECRET),
        ),
    ])


@pytest.fixture
def client(conn) -> TestClient:
    # A fresh connection per request, exactly like the live service.
    return TestClient(create_app(make_settings(tokens=_registry()), connect=db.connect))


def _events(conn, *, type: str, task_id=None) -> list:
    return read_events(conn, type=type, task_id=task_id)


# --- The acceptance path ----------------------------------------------------


def test_remote_can_enqueue_list_claim_heartbeat_and_complete(client, conn, ws) -> None:
    # 1. enqueue — a real row, attributed to the token identity.
    created = client.post(
        "/v1/tasks",
        json={"workstream": ws, "type": "work.remote", "priority": 3,
              "payload": {"goal": "prove remote access works"}},
        headers=bearer(REMOTE_SECRET),
    )
    assert created.status_code == 201, created.text
    task_id = created.json()["task"]["id"]
    row = get_task(conn, task_id)
    assert row is not None
    assert row.status is TaskStatus.UP_FOR_GRABS
    assert row.payload["enqueued_by"] == REMOTE_IDENTITY
    assert row.payload["goal"] == "prove remote access works"

    # 2. list — the new task is grabbable now.
    ready = client.get(f"/v1/tasks/ready?workstream={ws}", headers=bearer(REMOTE_SECRET))
    assert ready.status_code == 200
    assert [t["id"] for t in ready.json()["tasks"]] == [task_id]

    # 3. claim — grabbed AND started, held by this identity.
    claimed = client.post(
        "/v1/tasks/claim", json={"workstream": ws}, headers=bearer(REMOTE_SECRET)
    )
    assert claimed.status_code == 200
    body = claimed.json()["task"]
    assert body["id"] == task_id
    assert body["status"] == TaskStatus.IN_PROGRESS.value
    assert body["claimed_by"] == REMOTE_IDENTITY
    assert body["agent_type"] == DEFAULT_AGENT_TYPE

    # 4. heartbeat — liveness the supervisor can see.
    first_beat = get_task(conn, task_id).heartbeat_at
    beat = client.post(
        f"/v1/tasks/{task_id}/heartbeat", headers=bearer(REMOTE_SECRET)
    )
    assert beat.status_code == 200
    assert get_task(conn, task_id).heartbeat_at >= first_beat

    # 5. complete — through the canonical path, not a status write.
    done = client.post(
        f"/v1/tasks/{task_id}/complete",
        json={"status": "merged", "result": {"summary": "done"}, "spent_tokens": 42},
        headers=bearer(REMOTE_SECRET),
    )
    assert done.status_code == 200
    final = get_task(conn, task_id)
    assert final.status is TaskStatus.MERGED
    assert final.result["completed_by"] == REMOTE_IDENTITY
    assert final.spent_tokens == 42

    hops = [(t["from_status"], t["to_status"]) for t in
            task_lifecycle(conn, task_id)["transitions"]]
    assert hops == [
        ("up_for_grabs", "claimed"),
        ("claimed", "in_progress"),
        ("in_progress", "ready_for_review"),
        ("ready_for_review", "approved"),
        ("approved", "merged"),
    ]
    # The grab is attributed to the remote identity in lifecycle telemetry.
    grab = task_lifecycle(conn, task_id)["transitions"][0]
    assert grab["agent_id"] == REMOTE_IDENTITY


def test_remote_work_is_audited_in_the_event_log(client, conn, ws) -> None:
    created = client.post(
        "/v1/tasks", json={"workstream": ws, "type": "work.remote"},
        headers=bearer(REMOTE_SECRET),
    )
    task_id = created.json()["task"]["id"]

    access = _events(conn, type=EVENT_GATEWAY_ACCESS, task_id=task_id)
    assert len(access) == 1
    payload = access[0].payload
    assert payload["identity"] == REMOTE_IDENTITY
    assert payload["verb"] == "enqueue"
    assert payload["status"] == 201
    # Body-free: the audit event carries no task payload and no token.
    assert set(payload) == {"identity", "verb", "scope", "status", "reason"}


def test_an_authenticated_denial_is_audited(conn) -> None:
    reg = TokenRegistry([
        Token(identity="offhost-readonly-db", scopes=frozenset({SCOPE_READ}),
              digest=token_digest("readonly-db-secret")),
    ])
    client = TestClient(create_app(make_settings(tokens=reg), connect=db.connect))
    before = len(_events(conn, type=EVENT_GATEWAY_DENIED))

    denied = client.post(
        "/v1/tasks", json={"workstream": "gw-denied", "type": "work.remote"},
        headers=bearer("readonly-db-secret"),
    )
    assert denied.status_code == 403

    after = _events(conn, type=EVENT_GATEWAY_DENIED)
    assert len(after) == before + 1
    last = after[-1]
    assert last.workstream == GATEWAY_WORKSTREAM
    assert last.payload["identity"] == "offhost-readonly-db"
    assert last.payload["reason"] == "missing_scope"


# --- Claim ownership --------------------------------------------------------


def test_a_remote_cannot_finish_work_it_does_not_hold(client, conn, ws) -> None:
    created = client.post(
        "/v1/tasks", json={"workstream": ws, "type": "work.remote"},
        headers=bearer(REMOTE_SECRET),
    )
    task_id = created.json()["task"]["id"]
    assert client.post(
        "/v1/tasks/claim", json={"workstream": ws}, headers=bearer(REMOTE_SECRET)
    ).status_code == 200

    # A *different* valid token may not heartbeat or complete this task.
    for path in (f"/v1/tasks/{task_id}/heartbeat", f"/v1/tasks/{task_id}/complete"):
        resp = client.post(path, json={}, headers=bearer(OTHER_SECRET))
        assert resp.status_code == 403
        assert resp.json()["detail"] == REASON_NOT_OWNER
    assert get_task(conn, task_id).status is TaskStatus.IN_PROGRESS


def test_completing_an_unclaimed_task_is_refused(client, conn, ws) -> None:
    created = client.post(
        "/v1/tasks", json={"workstream": ws, "type": "work.remote"},
        headers=bearer(REMOTE_SECRET),
    )
    task_id = created.json()["task"]["id"]
    resp = client.post(f"/v1/tasks/{task_id}/complete", json={},
                       headers=bearer(REMOTE_SECRET))
    assert resp.status_code == 403  # nobody holds it → not the owner
    assert get_task(conn, task_id).status is TaskStatus.UP_FOR_GRABS


def test_unknown_task_is_404(client) -> None:
    missing = uuid4()
    assert client.get(f"/v1/tasks/{missing}",
                      headers=bearer(REMOTE_SECRET)).status_code == 404
    assert client.post(f"/v1/tasks/{missing}/heartbeat",
                       headers=bearer(REMOTE_SECRET)).status_code == 404


# --- Workstream pinning against real rows ----------------------------------


def test_a_pinned_token_cannot_see_another_workstreams_task(conn, ws) -> None:
    other_ws = f"{ws}-other"
    mine = enqueue_task(conn, workstream=ws, type="work.remote")
    theirs = enqueue_task(conn, workstream=other_ws, type="work.remote")

    pinned = TestClient(
        create_app(
            make_settings(tokens=_registry(workstreams=frozenset({ws}))),
            connect=db.connect,
        )
    )
    assert pinned.get(f"/v1/tasks/{mine.id}",
                      headers=bearer(REMOTE_SECRET)).status_code == 200
    assert pinned.get(f"/v1/tasks/{theirs.id}",
                      headers=bearer(REMOTE_SECRET)).status_code == 403

    # An unscoped list is confined to the pinned workstream, not widened.
    listed = pinned.get("/v1/tasks/ready", headers=bearer(REMOTE_SECRET))
    assert listed.status_code == 200
    assert {t["workstream"] for t in listed.json()["tasks"]} == {ws}


def test_a_pinned_token_cannot_claim_outside_its_workstream(conn, ws) -> None:
    other_ws = f"{ws}-other"
    enqueue_task(conn, workstream=other_ws, type="work.remote")
    pinned = TestClient(
        create_app(
            make_settings(tokens=_registry(workstreams=frozenset({ws}))),
            connect=db.connect,
        )
    )
    # Nothing grabbable inside the pinned workstream → no task, and certainly not
    # the other workstream's one.
    resp = pinned.post("/v1/tasks/claim", json={}, headers=bearer(REMOTE_SECRET))
    assert resp.status_code == 200 and resp.json()["task"] is None
    assert pinned.post(
        "/v1/tasks/claim", json={"workstream": other_ws}, headers=bearer(REMOTE_SECRET)
    ).status_code == 403


# --- Queue semantics preserved ---------------------------------------------


def test_a_remote_does_not_steal_host_pinned_work(client, conn, ws) -> None:
    host_task = enqueue_task(
        conn, workstream=ws, type="work.remote", assignee=Assignee.HOST
    )
    # Default claim (offhost pool) never sees host-pinned work.
    resp = client.post("/v1/tasks/claim", json={"workstream": ws},
                       headers=bearer(REMOTE_SECRET))
    assert resp.status_code == 200 and resp.json()["task"] is None

    # Explicit-host BYPASS (the CVE): asking for the host pool by name is refused
    # at validation (422) — it must NOT reach the queue, and the host task must
    # stay grabbable by the host (unchanged, still up_for_grabs, still unclaimed).
    bypass = client.post("/v1/tasks/claim", json={"workstream": ws, "assignee": "host"},
                         headers=bearer(REMOTE_SECRET))
    assert bypass.status_code == 422, bypass.text
    still = get_task(conn, host_task.id)
    assert still.status is TaskStatus.UP_FOR_GRABS
    assert still.claimed_by is None


@pytest.mark.parametrize("blank", [None, "", "   ", "\t"])
def test_a_blank_assignee_does_not_steal_host_pinned_work(client, conn, ws, blank) -> None:
    """The null/blank BYPASS: a blank/``null`` assignee must coerce to ``offhost``,
    NOT to ``None`` (which drops the pool clause in grab_task and spans every pool
    incl. host). For each blank form the claim must NOT grab the host-pinned task,
    which stays ``up_for_grabs`` / ``claimed_by IS NULL`` (mirrors the ``host`` case).

    An offhost task coexists so a 200-with-task result would prove the grab ran and
    still correctly avoided the host pool (not merely that the queue was empty)."""
    host_task = enqueue_task(
        conn, workstream=ws, type="work.host", assignee=Assignee.HOST
    )
    safe = enqueue_task(conn, workstream=ws, type="work.remote", assignee=Assignee.OFFHOST)
    resp = client.post("/v1/tasks/claim", json={"workstream": ws, "assignee": blank},
                       headers=bearer(REMOTE_SECRET))
    assert resp.status_code == 200, resp.text
    grabbed = resp.json()["task"]
    # Whatever it grabbed, it was NOT the host task.
    assert grabbed is None or grabbed["id"] != str(host_task.id)
    if grabbed is not None:
        assert grabbed["id"] == str(safe.id)
    still = get_task(conn, host_task.id)
    assert still.status is TaskStatus.UP_FOR_GRABS
    assert still.claimed_by is None


def test_a_bogus_assignee_is_422_not_500(client, conn, ws) -> None:
    """An out-of-range assignee is rejected cleanly (422), never an ungraceful 500
    from ``Assignee(<bad>)`` — the runtime store is never even opened."""
    enqueue_task(conn, workstream=ws, type="work.remote")
    resp = client.post("/v1/tasks/claim", json={"workstream": ws, "assignee": "bogus"},
                       headers=bearer(REMOTE_SECRET))
    assert resp.status_code == 422, resp.text


def test_default_offhost_claim_grabs_unassigned_and_offhost_work(client, conn, ws) -> None:
    """The permitted pool is unchanged: default (``offhost``) still grabs both an
    unassigned task and an explicitly ``offhost``-pinned one."""
    unassigned = enqueue_task(conn, workstream=ws, type="work.remote")
    first = client.post("/v1/tasks/claim", json={"workstream": ws},
                        headers=bearer(REMOTE_SECRET))
    assert first.status_code == 200 and first.json()["task"]["id"] == str(unassigned.id)

    offhost = enqueue_task(
        conn, workstream=ws, type="work.remote", assignee=Assignee.OFFHOST
    )
    second = client.post("/v1/tasks/claim", json={"workstream": ws, "assignee": "offhost"},
                         headers=bearer(REMOTE_SECRET))
    assert second.status_code == 200 and second.json()["task"]["id"] == str(offhost.id)


def test_dependencies_still_gate_a_remote_claim(client, conn, ws) -> None:
    prereq = enqueue_task(conn, workstream=ws, type="work.remote")
    blocked = enqueue_task(
        conn, workstream=ws, type="work.remote", depends_on=[prereq.id]
    )
    waiting = client.get(f"/v1/tasks/waiting?workstream={ws}",
                         headers=bearer(REMOTE_SECRET))
    assert waiting.status_code == 200
    assert [w["task"]["id"] for w in waiting.json()["tasks"]] == [str(blocked.id)]
    # The claim can only ever get the prerequisite, never the dependent.
    claimed = client.post("/v1/tasks/claim", json={"workstream": ws},
                          headers=bearer(REMOTE_SECRET))
    assert claimed.json()["task"]["id"] == str(prereq.id)


def test_priority_is_clamped_so_a_remote_cannot_outrank_the_host(conn, ws) -> None:
    client = TestClient(
        create_app(make_settings(tokens=_registry(), max_priority=5),
                   connect=db.connect)
    )
    created = client.post(
        "/v1/tasks", json={"workstream": ws, "type": "work.remote", "priority": 9999},
        headers=bearer(REMOTE_SECRET),
    )
    assert created.status_code == 201
    assert get_task(conn, created.json()["task"]["id"]).priority == 5


def test_budget_tokens_are_capped(conn, ws) -> None:
    client = TestClient(
        create_app(make_settings(tokens=_registry(), max_budget_tokens=1000),
                   connect=db.connect)
    )
    created = client.post(
        "/v1/tasks",
        json={"workstream": ws, "type": "work.remote", "budget_tokens": 10_000_000},
        headers=bearer(REMOTE_SECRET),
    )
    assert get_task(conn, created.json()["task"]["id"]).budget_tokens == 1000


def test_list_limit_is_clamped(conn, ws) -> None:
    for _ in range(4):
        enqueue_task(conn, workstream=ws, type="work.remote")
    client = TestClient(
        create_app(make_settings(tokens=_registry(), max_limit=2), connect=db.connect)
    )
    resp = client.get(f"/v1/tasks/ready?workstream={ws}&limit=1000",
                      headers=bearer(REMOTE_SECRET))
    assert resp.json()["count"] == 2


def test_a_revoked_identity_cannot_claim(client, conn, ws) -> None:
    """ADR-0021's trust fence covers remotes for free (identity == worker_id).

    Revocation is therefore a kill switch for a leaked token that takes effect
    before the token is even rotated out of ``TASK_GATEWAY_TOKENS``.
    """
    from runtime.trust import record_strike

    # A throwaway identity, so the shared dev ledger keeps no permanent scar.
    revoked_identity = f"offhost-revoked-{uuid4().hex[:8]}"
    revoked_secret = f"secret-{uuid4().hex}"
    revoked_client = TestClient(
        create_app(
            make_settings(tokens=TokenRegistry([
                Token(identity=revoked_identity, scopes=_ALL,
                      digest=token_digest(revoked_secret)),
            ])),
            connect=db.connect,
        )
    )
    enqueue_task(conn, workstream=ws, type="work.remote")
    record_strike(conn, revoked_identity, detail="test fixture")

    resp = revoked_client.post(
        "/v1/tasks/claim", json={"workstream": ws}, headers=bearer(revoked_secret)
    )
    assert resp.status_code == 200 and resp.json()["task"] is None
    # …while a trusted identity still claims the very same task.
    ok = client.post("/v1/tasks/claim", json={"workstream": ws},
                     headers=bearer(REMOTE_SECRET))
    assert ok.json()["task"] is not None
