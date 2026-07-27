"""Prod vs test traffic tagging for queue rows.

Never infer test-ness from goal text. A row is test traffic iff
``payload.traffic == \"test\"`` (or the process default when enqueueing under
``AI_STUDIO_TRAFFIC=test`` / ``AI_STUDIO_TEST_DB=1``). Production defaults to
``traffic=prod``. Stakeholder views and ops cleanups must key off this flag
(and disposable-DB workstream conventions), not fixture phrase matching.
"""

from __future__ import annotations

import os
from typing import Any, Mapping, MutableMapping, Optional

TRAFFIC_KEY = "traffic"
TRAFFIC_PROD = "prod"
TRAFFIC_TEST = "test"

_ENV_TRAFFIC = "AI_STUDIO_TRAFFIC"
_ENV_TEST_DB = "AI_STUDIO_TEST_DB"


def default_traffic() -> str:
    """Process-wide default for newly enqueued tasks."""
    explicit = (os.environ.get(_ENV_TRAFFIC) or "").strip().lower()
    if explicit in {TRAFFIC_PROD, TRAFFIC_TEST}:
        return explicit
    if (os.environ.get(_ENV_TEST_DB) or "").strip().lower() in {
        "1", "true", "yes", "on",
    }:
        return TRAFFIC_TEST
    return TRAFFIC_PROD


def tag_payload(payload: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    """Return a copy of ``payload`` with ``traffic`` set if missing."""
    out: dict[str, Any] = dict(payload or {})
    raw = out.get(TRAFFIC_KEY)
    if isinstance(raw, str) and raw.strip().lower() in {TRAFFIC_PROD, TRAFFIC_TEST}:
        out[TRAFFIC_KEY] = raw.strip().lower()
    else:
        out[TRAFFIC_KEY] = default_traffic()
    return out


def is_test_payload(payload: Optional[Mapping[str, Any]]) -> bool:
    """True when the payload is explicitly marked test traffic."""
    if not payload:
        return False
    return str(payload.get(TRAFFIC_KEY) or "").strip().lower() == TRAFFIC_TEST


def is_test_task_row(row: Mapping[str, Any]) -> bool:
    """True for a tasks-table mapping with test traffic (payload jsonb)."""
    payload = row.get("payload")
    if isinstance(payload, Mapping):
        return is_test_payload(payload)
    return False
