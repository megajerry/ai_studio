"""FastAPI app for the WhatsApp Spokesman.

Endpoints:

- ``GET  /health``        — liveness probe.
- ``GET  /webhook``       — Meta verification handshake.
- ``POST /webhook``       — inbound messages (HMAC-SHA256 signature verified).
- ``POST /notify``        — classify + route a studio output (ADR-0006).
- ``POST /digest/flush``  — send the pending approve/inform digest.
- ``POST /poll``          — one runtime-bridge notifier pass (event log → sends).

``/notify``, ``/digest/flush`` and ``/poll`` are the control plane and require
the ``X-Spokesman-Token`` header (shared secret ``SPOKESMAN_API_TOKEN``);
``/webhook`` and ``/health`` are public.

The Spokesman is wired to the runtime DB via :mod:`spokesman.runtime_bridge`:
``/poll`` reads new events (past a persisted ``seq`` cursor), routes 🚨 alarms
immediately and batches 🛑/📣 into the digest; inbound ``approve <id>`` /
``deny <id>`` resolve a real approval and ``status`` returns live DB counts.

All config/secrets come from the environment (ADR-0011). Runs with no live
credentials when ``SPOKESMAN_DRY_RUN=1``; the DB is the live runtime Postgres.
"""

from __future__ import annotations

import hmac
import logging
from typing import Callable, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field, field_validator

from runtime.grounding import Claim

from .classify import Notifier, NotifyKind
from .config import Settings, get_settings
from .grounding_gate import relay_claims
from .runtime_bridge import (
    answer,
    load_cursor,
    poll_notifications,
    resolve,
    save_cursor,
    studio_status,
)
from .state import (
    InboundMessage,
    iter_inbound_messages,
    mask_number,
    record_inbound,
    status_summary,
)
from .whatsapp import WhatsAppClient, verify_signature

logger = logging.getLogger("spokesman.app")

STATUS_KEYWORD = "status"
#: Inbound command verbs that resolve an approval: ``<verb> <approval-id>``.
_RESOLVE_VERBS = {"approve", "approved", "deny", "denied", "reject", "yes", "no"}
#: Inbound verb that answers an OPEN-ENDED decision: ``decide <id> <answer>`` (ADR-0025).
_DECIDE_VERB = "decide"

#: Default DB connection factory (the live runtime Postgres). Injectable for tests.
ConnectFn = Callable[[], object]


def _default_connect() -> object:
    from runtime import db

    return db.connect()


class NotifyRequest(BaseModel):
    """A structured, GROUNDED notify request (ADR-0021 S2).

    Replaces the old free-text ``{kind, text}`` — the fabrication hole where any
    caller could relay arbitrary prose verbatim. A request now carries the
    ``originating_identity`` (who is accountable) and a list of typed
    :class:`~runtime.grounding.Claim`s, each with its own evidence. A non-judgment
    claim with empty evidence is rejected by :class:`Claim`'s own validator (→ 422)
    before it ever reaches the gate; the gate then verifies each claim's evidence
    against source of truth and relays only what it can confirm.
    """

    kind: str = Field(..., description="approve | inform | alarm")
    originating_identity: str = Field(..., min_length=1)
    claims: list[Claim] = Field(..., min_length=1)
    message_ref: str | None = None

    @field_validator("kind")
    @classmethod
    def _known_kind(cls, v: str) -> str:
        try:
            NotifyKind(v.strip().lower())
        except ValueError as exc:
            raise ValueError(
                f"unknown notify kind {v!r}; expected one of "
                f"{[k.value for k in NotifyKind]}"
            ) from exc
        return v.strip().lower()


def run_notifier_pass(
    settings: Settings,
    notifier: Notifier,
    connect: ConnectFn,
) -> dict:
    """One runtime-bridge pass: event log → classified sends, cursor persisted.

    Reads new events past the persisted ``seq`` cursor, sends 🚨 alarms
    immediately and batches 🛑/📣 into the pending digest (both via the existing
    :class:`~spokesman.classify.Notifier` routing), then advances the cursor so no
    event is ever notified twice. Returns per-tier counts + the new cursor.
    """
    conn = connect()
    try:
        cursor = load_cursor(settings.state_dir)
        batch = poll_notifications(conn, cursor)
    finally:
        _close(conn)

    alarms = 0
    digested = 0
    for item in batch.items:
        notifier.notify(item.kind.value, item.text)
        if item.kind.value == "alarm":
            alarms += 1
        else:
            digested += 1

    if batch.cursor != cursor:
        save_cursor(settings.state_dir, batch.cursor)

    return {
        "scanned_to_seq": batch.cursor,
        "alarms_sent": alarms,
        "digest_queued": digested,
        "pending_digest": notifier.pending_count,
    }


def handle_inbound_command(
    settings: Settings,
    client: WhatsAppClient,
    connect: ConnectFn,
    msg: InboundMessage,
) -> Optional[dict]:
    """Interpret one inbound message and act (status / approve / deny), or None.

    - ``status``            → live DB summary (falls back to ``state/status.md``).
    - ``approve <id>`` /
      ``deny <id>``         → resolve the real approval via the runtime store.
    - ``decide <id> <answer>`` → answer an OPEN-ENDED decision (ADR-0025) via the
      runtime decision store; this also resumes the parked dependent task.

    A reply is always sent for a recognized command (so the stakeholder gets
    confirmation); unrecognized text is ignored (returns ``None``). The webhook's
    HMAC gate already authenticated the sender, so no extra auth here.
    """
    parts = msg.text.strip().split()
    if not parts:
        return None
    verb = parts[0].lower()
    to = msg.sender or None

    if verb == STATUS_KEYWORD:
        try:
            conn = connect()
            try:
                summary = studio_status(conn).render()
            finally:
                _close(conn)
        except Exception:  # DB unreachable → fall back to the git status.md
            logger.warning("status: DB unreachable, falling back to state/status.md")
            summary = status_summary(settings)
        client.send_text(summary, to=to)
        return {"command": "status"}

    if verb in _RESOLVE_VERBS:
        if len(parts) < 2:
            client.send_text(f"Usage: {verb} <approval-id>", to=to)
            return {"command": verb, "ok": False, "error": "missing id"}
        approval_id = parts[1]
        resolver = f"whatsapp:{mask_number(msg.sender)}"
        try:
            conn = connect()
            try:
                approval = resolve(conn, approval_id, verb, resolver)
            finally:
                _close(conn)
        except ValueError:
            client.send_text(f"Invalid approval id: {approval_id}", to=to)
            return {"command": verb, "ok": False, "error": "invalid id"}
        except Exception:  # noqa: BLE001 - DB unreachable / transient
            logger.exception("resolve failed for approval %s", approval_id)
            client.send_text("Could not reach the runtime; try again shortly.", to=to)
            return {"command": verb, "ok": False, "error": "db unavailable"}

        if approval is None:
            client.send_text(
                f"Approval {approval_id} not found or already resolved.", to=to
            )
            return {"command": verb, "ok": False, "error": "not pending"}
        client.send_text(f"Approval {approval_id} {approval.status}.", to=to)
        return {"command": verb, "ok": True, "status": approval.status}

    if verb == _DECIDE_VERB:
        # `decide <id> <answer...>` — the answer is the free-text remainder, so a
        # multi-word answer ("go with vendor B") is preserved verbatim.
        if len(parts) < 3:
            client.send_text(f"Usage: {_DECIDE_VERB} <decision-id> <answer>", to=to)
            return {"command": _DECIDE_VERB, "ok": False, "error": "missing id/answer"}
        decision_id = parts[1]
        answer_text = msg.text.strip().split(None, 2)[2]
        resolver = f"whatsapp:{mask_number(msg.sender)}"
        try:
            conn = connect()
            try:
                decision = answer(conn, decision_id, answer_text, resolver)
            finally:
                _close(conn)
        except ValueError:
            client.send_text(f"Invalid decision id: {decision_id}", to=to)
            return {"command": _DECIDE_VERB, "ok": False, "error": "invalid id"}
        except Exception:  # noqa: BLE001 - DB unreachable / transient
            logger.exception("answer failed for decision %s", decision_id)
            client.send_text("Could not reach the runtime; try again shortly.", to=to)
            return {"command": _DECIDE_VERB, "ok": False, "error": "db unavailable"}

        if decision is None:
            client.send_text(
                f"Decision {decision_id} not found or already answered.", to=to
            )
            return {"command": _DECIDE_VERB, "ok": False, "error": "not open"}
        client.send_text(f"Decision {decision_id} answered.", to=to)
        return {"command": _DECIDE_VERB, "ok": True, "status": decision.status}

    return None


def _close(conn: object) -> None:
    try:
        conn.close()  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - best-effort cleanup
        pass


def create_app(
    settings: Settings | None = None,
    *,
    connect: ConnectFn | None = None,
) -> FastAPI:
    """Application factory (lets tests inject settings / a DB connection factory).

    ``connect`` returns an open runtime DB connection (defaults to the live
    ``runtime.db.connect``); the app closes each connection it opens.
    """
    settings = settings or get_settings()
    connect = connect or _default_connect
    client = WhatsAppClient(settings)
    notifier = Notifier(client)

    app = FastAPI(title="AI Studio Spokesman", version="0.1.0")
    app.state.settings = settings
    app.state.client = client
    app.state.notifier = notifier

    def require_api_token(
        x_spokesman_token: str | None = Header(default=None),
    ) -> None:
        """Gate the control plane (/notify, /digest/flush) with a shared secret.

        These endpoints sit on the same publicly-tunneled port as the webhook,
        so they must not be open: an attacker could otherwise inject spoofed
        alarms/approvals to the stakeholder. Fails **closed** — if
        ``SPOKESMAN_API_TOKEN`` is unset the endpoints are unusable.
        """
        expected = settings.api_token
        if not expected or not x_spokesman_token or not hmac.compare_digest(
            x_spokesman_token, expected
        ):
            raise HTTPException(status_code=401, detail="invalid or missing token")

    @app.get("/health")
    def health() -> dict:
        return {
            "status": "ok",
            "dry_run": settings.dry_run,
            "pending_digest": notifier.pending_count,
        }

    # Meta sends hub.* query params (dots), which are not valid Python
    # identifiers, so we read them off the raw request.
    @app.get("/webhook")
    def verify_webhook(request: Request) -> Response:
        params = request.query_params
        mode = params.get("hub.mode")
        token = params.get("hub.verify_token")
        challenge = params.get("hub.challenge", "")
        if (
            mode == "subscribe"
            and token
            and settings.verify_token
            and hmac.compare_digest(token, settings.verify_token)
        ):
            return PlainTextResponse(challenge)
        logger.warning("Webhook verification failed (mode=%s)", mode)
        return PlainTextResponse("verification failed", status_code=403)

    @app.post("/webhook")
    async def receive_webhook(
        request: Request,
        x_hub_signature_256: str | None = Header(default=None),
    ) -> Response:
        raw_body = await request.body()
        if not verify_signature(raw_body, x_hub_signature_256, settings.app_secret):
            logger.warning("Rejected inbound webhook: bad signature")
            return JSONResponse({"error": "invalid signature"}, status_code=403)

        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001 - malformed body after signature check
            return JSONResponse({"error": "invalid json"}, status_code=400)

        messages = iter_inbound_messages(payload)
        record_inbound(settings, messages)

        replies = 0
        for msg in messages:
            if handle_inbound_command(settings, client, connect, msg) is not None:
                replies += 1

        return JSONResponse({"received": len(messages), "replies": replies})

    @app.post("/notify", dependencies=[Depends(require_api_token)])
    def notify(req: NotifyRequest) -> dict:
        """Verify-or-refuse grounding gate for a studio→human message (ADR-0021).

        The request's claims are recorded for provenance and independently verified
        against source of truth; only VERIFIED facts + labelled judgments are
        relayed (see :func:`spokesman.grounding_gate.relay_claims`). Fails CLOSED
        on a DB outage: if the runtime cannot be reached to verify, nothing is
        relayed (a 200 with ``blocked=True``, never a crash — ADR-0017).
        """
        try:
            conn = connect()
        except Exception:  # noqa: BLE001 - DB unreachable ⇒ fail closed, don't crash
            logger.warning("/notify: runtime DB unreachable — relaying nothing (fail closed)")
            return {"blocked": True, "relayed": [], "claims": [], "escalated": False,
                    "reason": "runtime unreachable (fail closed)"}
        try:
            return relay_claims(
                conn, notifier, kind=req.kind,
                originating_identity=req.originating_identity,
                claims=req.claims, message_ref=req.message_ref,
            )
        finally:
            _close(conn)

    @app.post("/digest/flush", dependencies=[Depends(require_api_token)])
    def flush_digest() -> dict:
        return notifier.flush()

    @app.post("/poll", dependencies=[Depends(require_api_token)])
    def poll() -> dict:
        return run_notifier_pass(settings, notifier, connect)

    return app


app = create_app()
