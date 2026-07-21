"""FastAPI app for the WhatsApp Spokesman.

Endpoints:

- ``GET  /health``        — liveness probe.
- ``GET  /webhook``       — Meta verification handshake.
- ``POST /webhook``       — inbound messages (HMAC-SHA256 signature verified).
- ``POST /notify``        — classify + route a studio output (ADR-0006).
- ``POST /digest/flush``  — send the pending approve/inform digest.

All config/secrets come from the environment (ADR-0011). Runs with no live
credentials when ``SPOKESMAN_DRY_RUN=1``.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Header, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from .classify import Notifier
from .config import Settings, get_settings
from .state import (
    iter_inbound_messages,
    record_inbound,
    status_summary,
)
from .whatsapp import WhatsAppClient, verify_signature

logger = logging.getLogger("spokesman.app")

STATUS_KEYWORD = "status"


class NotifyRequest(BaseModel):
    kind: str = Field(..., description="approve | inform | alarm")
    text: str = Field(..., min_length=1)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Application factory (lets tests inject settings / env overrides)."""
    settings = settings or get_settings()
    client = WhatsAppClient(settings)
    notifier = Notifier(client)

    app = FastAPI(title="AI Studio Spokesman", version="0.1.0")
    app.state.settings = settings
    app.state.client = client
    app.state.notifier = notifier

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
        if mode == "subscribe" and token and token == settings.verify_token:
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
            if msg.text.strip().lower() == STATUS_KEYWORD:
                client.send_text(status_summary(settings), to=msg.sender or None)
                replies += 1

        return JSONResponse({"received": len(messages), "replies": replies})

    @app.post("/notify")
    def notify(req: NotifyRequest) -> dict:
        return notifier.notify(req.kind, req.text)

    @app.post("/digest/flush")
    def flush_digest() -> dict:
        return notifier.flush()

    return app


app = create_app()
