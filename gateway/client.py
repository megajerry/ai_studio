"""Remote-side client + CLI for the task gateway (ADR-0027) — **stdlib only**.

A remote session (Cursor cloud container, a laptop off-LAN) needs the queue, not
a dependency install: this module uses nothing but the standard library and is
**self-contained on purpose** — copy this one file next to an agent and it works
with no repo checkout, no psycopg, no DB credential.

Use it::

    export TASK_GATEWAY_URL=https://tasks.example.com
    export TASK_GATEWAY_TOKEN=…                 # the secret, host-side digest only

    python3 -m gateway.client whoami
    python3 -m gateway.client ready --workstream productivity
    python3 -m gateway.client enqueue --workstream productivity --type work.docs \\
        --payload '{"goal": "draft the runbook"}'
    python3 -m gateway.client claim
    python3 -m gateway.client heartbeat <task-id>
    python3 -m gateway.client complete <task-id> --status merged

Host-side, ``mint`` generates a credential and prints the ``TASK_GATEWAY_TOKENS``
entry to paste into the git-ignored ``.env`` — the secret is shown once and only
its SHA-256 digest is ever stored (ADR-0011).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Mapping, Optional

#: Where the gateway lives (the tunnel hostname) and the caller's token secret.
ENV_URL = "TASK_GATEWAY_URL"
ENV_TOKEN = "TASK_GATEWAY_TOKEN"

DEFAULT_TIMEOUT_S = 15.0


class GatewayError(RuntimeError):
    """A non-2xx answer from the gateway (carries the status + its reason code)."""

    def __init__(self, status: int, detail: str):
        super().__init__(f"gateway error {status}: {detail}")
        self.status = status
        self.detail = detail


class TaskGatewayClient:
    """Thin typed wrapper over the gateway's verbs (one method per endpoint)."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = DEFAULT_TIMEOUT_S,
    ):
        if not base_url:
            raise ValueError("base_url is required (e.g. https://tasks.example.com)")
        if not token:
            raise ValueError("token is required")
        self.base_url = base_url.rstrip("/")
        self._token = token
        self.timeout = timeout

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "TaskGatewayClient":
        """Build from ``TASK_GATEWAY_URL`` + ``TASK_GATEWAY_TOKEN``."""
        env = os.environ if env is None else env
        url = (env.get(ENV_URL) or "").strip()
        token = (env.get(ENV_TOKEN) or "").strip()
        if not url or not token:
            raise ValueError(
                f"set {ENV_URL} and {ENV_TOKEN} (the token is a secret — never commit it)"
            )
        return cls(url, token)

    # --- transport ---------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: Optional[dict] = None,
        params: Optional[dict] = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                url = f"{url}?{urllib.parse.urlencode(clean)}"
        data = None
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
        }
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
            # The gateway requires a declared length (it refuses chunked bodies).
            headers["Content-Length"] = str(len(data))
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:  # 4xx/5xx carry a JSON detail
            payload = exc.read()
            detail = ""
            try:
                detail = (json.loads(payload) or {}).get("detail", "")
            except Exception:  # noqa: BLE001 - non-JSON error body
                detail = payload.decode("utf-8", "replace")[:200]
            raise GatewayError(exc.code, str(detail)) from None
        except urllib.error.URLError as exc:
            raise GatewayError(0, f"gateway unreachable: {exc.reason}") from None
        if not raw:
            return None
        return json.loads(raw)

    # --- verbs -------------------------------------------------------------

    def health(self) -> dict:
        return self._request("GET", "/health")

    def whoami(self) -> dict:
        return self._request("GET", "/v1/whoami")

    def ready(
        self, *, workstream: Optional[str] = None, limit: Optional[int] = None
    ) -> dict:
        return self._request(
            "GET", "/v1/tasks/ready",
            params={"workstream": workstream, "limit": limit},
        )

    def waiting(self, *, workstream: Optional[str] = None) -> dict:
        return self._request(
            "GET", "/v1/tasks/waiting", params={"workstream": workstream}
        )

    def review(
        self, *, workstream: Optional[str] = None, limit: Optional[int] = None
    ) -> dict:
        return self._request(
            "GET", "/v1/tasks/review",
            params={"workstream": workstream, "limit": limit},
        )

    def get_task(self, task_id: str) -> dict:
        return self._request("GET", f"/v1/tasks/{task_id}")

    def enqueue(
        self,
        *,
        workstream: Optional[str] = None,
        type: str,
        payload: Optional[dict] = None,
        priority: int = 0,
        assignee: Optional[str] = None,
        budget_tokens: Optional[int] = None,
        depends_on: Optional[list] = None,
    ) -> dict:
        return self._request(
            "POST", "/v1/tasks",
            body={
                "workstream": workstream,
                "type": type,
                "payload": payload or {},
                "priority": priority,
                "assignee": assignee,
                "budget_tokens": budget_tokens,
                "depends_on": [str(d) for d in (depends_on or [])],
            },
        )

    def claim(
        self,
        *,
        workstream: Optional[str] = None,
        agent_type: Optional[str] = None,
        assignee: Optional[str] = "offhost",
    ) -> dict:
        return self._request(
            "POST", "/v1/tasks/claim",
            body={
                "workstream": workstream,
                "agent_type": agent_type,
                "assignee": assignee,
            },
        )

    def heartbeat(self, task_id: str) -> dict:
        return self._request("POST", f"/v1/tasks/{task_id}/heartbeat", body={})

    def complete(
        self,
        task_id: str,
        *,
        status: str = "merged",
        result: Optional[dict] = None,
        spent_tokens: Optional[int] = None,
    ) -> dict:
        return self._request(
            "POST", f"/v1/tasks/{task_id}/complete",
            body={
                "status": status,
                "result": result or {},
                "spent_tokens": spent_tokens,
            },
        )


# --- Host-side credential minting -------------------------------------------


def mint(identity: str, scopes: list, workstreams: Optional[list] = None) -> dict:
    """Generate a token secret + the ``TASK_GATEWAY_TOKENS`` entry for it.

    Returns ``{"token", "digest", "spec"}``. Only ``spec`` (which contains the
    **digest**, never the secret) is ever stored; the secret goes to the remote
    once and is shown once. Hashing is inlined rather than imported from
    :mod:`gateway.auth` so this file stays self-contained/copyable — the digest is
    plain SHA-256 either way.
    """
    token = secrets.token_urlsafe(32)
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    spec = f"{identity}:{'|'.join(scopes)}:{digest}"
    if workstreams:
        spec = f"{spec}:{'|'.join(workstreams)}"
    return {"token": token, "digest": digest, "spec": spec}


# --- CLI --------------------------------------------------------------------


def _split_list(value: Optional[str]) -> list:
    if not value:
        return []
    return [v.strip() for v in value.replace("|", ",").split(",") if v.strip()]


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m gateway.client",
        description="Work the AI Studio task queue from a remote session (ADR-0027).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("health", help="gateway liveness (no token needed server-side)")
    sub.add_parser("whoami", help="show this token's identity + scopes")

    for name, helptext in (
        ("ready", "tasks grabbable now"),
        ("review", "tasks awaiting review"),
    ):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("--workstream")
        p.add_argument("--limit", type=int)

    p_waiting = sub.add_parser("waiting", help="tasks blocked on a prerequisite")
    p_waiting.add_argument("--workstream")

    p_get = sub.add_parser("get", help="read one task by id")
    p_get.add_argument("task_id")

    p_enq = sub.add_parser("enqueue", help="create a task")
    p_enq.add_argument(
        "--workstream",
        help="omit when the token is pinned to exactly one workstream",
    )
    p_enq.add_argument("--type", required=True)
    p_enq.add_argument("--payload", default="{}", help="JSON object")
    p_enq.add_argument("--priority", type=int, default=0)
    p_enq.add_argument("--assignee", choices=["host", "offhost"])
    p_enq.add_argument("--budget-tokens", type=int, dest="budget_tokens")
    p_enq.add_argument("--depends-on", dest="depends_on", help="comma-separated ids")

    p_claim = sub.add_parser("claim", help="grab + start the next task")
    p_claim.add_argument("--workstream")
    p_claim.add_argument("--agent-type", dest="agent_type")
    p_claim.add_argument("--assignee", choices=["host", "offhost"], default="offhost")

    p_hb = sub.add_parser("heartbeat", help="refresh a held task's heartbeat")
    p_hb.add_argument("task_id")

    p_done = sub.add_parser("complete", help="finalize a held task")
    p_done.add_argument("task_id")
    p_done.add_argument("--status", choices=["merged", "abandoned"], default="merged")
    p_done.add_argument("--result", default="{}", help="JSON object")
    p_done.add_argument("--spent-tokens", type=int, dest="spent_tokens")

    p_mint = sub.add_parser(
        "mint", help="HOST-SIDE: generate a token + its TASK_GATEWAY_TOKENS entry"
    )
    p_mint.add_argument("--identity", required=True)
    p_mint.add_argument("--scopes", default="read", help="read,enqueue,claim,complete")
    p_mint.add_argument("--workstreams", default="", help="optional pinning")

    args = parser.parse_args(argv)

    if args.command == "mint":
        out = mint(args.identity, _split_list(args.scopes), _split_list(args.workstreams))
        print(json.dumps(out, indent=2))
        print(
            "\n# Host: append the spec (digest only) to TASK_GATEWAY_TOKENS in .env,\n"
            "# then `docker compose --profile gateway up -d task-gateway`.\n"
            "# Remote: set TASK_GATEWAY_TOKEN to `token` above. It is shown ONCE.",
            file=sys.stderr,
        )
        return 0

    try:
        client = TaskGatewayClient.from_env()
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        if args.command == "health":
            out = client.health()
        elif args.command == "whoami":
            out = client.whoami()
        elif args.command == "ready":
            out = client.ready(workstream=args.workstream, limit=args.limit)
        elif args.command == "review":
            out = client.review(workstream=args.workstream, limit=args.limit)
        elif args.command == "waiting":
            out = client.waiting(workstream=args.workstream)
        elif args.command == "get":
            out = client.get_task(args.task_id)
        elif args.command == "enqueue":
            out = client.enqueue(
                workstream=args.workstream, type=args.type,
                payload=json.loads(args.payload), priority=args.priority,
                assignee=args.assignee, budget_tokens=args.budget_tokens,
                depends_on=_split_list(args.depends_on),
            )
        elif args.command == "claim":
            out = client.claim(
                workstream=args.workstream, agent_type=args.agent_type,
                assignee=args.assignee,
            )
        elif args.command == "heartbeat":
            out = client.heartbeat(args.task_id)
        elif args.command == "complete":
            out = client.complete(
                args.task_id, status=args.status,
                result=json.loads(args.result), spent_tokens=args.spent_tokens,
            )
        else:  # pragma: no cover - argparse enforces the choices
            parser.error(f"unknown command {args.command!r}")
            return 2
    except GatewayError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except json.JSONDecodeError as exc:
        print(f"error: --payload/--result must be valid JSON ({exc})", file=sys.stderr)
        return 2

    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
