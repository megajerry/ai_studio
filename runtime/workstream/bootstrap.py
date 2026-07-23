"""Bootstrap a workstream from its config — seed memory + wire the budget.

Loading a :class:`~runtime.workstream.config.WorkstreamConfig` is pure (no DB).
:func:`bootstrap_workstream` is the one-time (idempotent) side-effecting step that
prepares a vertical's DB-scoped state so its agents start with the right context:

- **memory seed** — each ``memory_seed`` item is written into the workstream's
  Knowledge memory (:func:`runtime.memory.add_lesson`) so the vertical's roles
  recall its founding docs/lessons from day one. **Idempotent**: an item whose
  exact text already exists for this workstream (or globally) is skipped, so
  re-running bootstrap never duplicates seeds.
- **budget** — the ``budget`` cap is written to the ``budgets`` table
  (:func:`runtime.budget.set_budget`, itself an idempotent upsert) so the
  existing per-workstream enforcement (:mod:`runtime.budget`) gates this
  workstream's model spend.

Object-store buckets + product repos are provisioned OUT of band (ADR-0018,
`workstreams/README.md`); this helper only touches the platform's own DB state.
It emits no secrets and is safe to re-run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from ..budget import set_budget
from ..memory import GLOBAL_WORKSTREAM, add_lesson
from .config import WorkstreamConfig


@dataclass
class BootstrapResult:
    """What :func:`bootstrap_workstream` did — for logging + tests (no secrets)."""

    workstream: str
    seeds_added: list[str] = field(default_factory=list)
    seeds_skipped: int = 0
    budget_set: bool = False


def _seed_exists(conn: Any, workstream: str, text: str) -> bool:
    """Has this exact Knowledge text already been seeded for ``workstream``?

    Checks the workstream's own corpus AND the shared global corpus, so a seed
    stored globally on a prior run is not re-added as a workstream-local copy.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM memory_items "
            "WHERE layer = 'knowledge' AND workstream = ANY(%s) AND text = %s "
            "LIMIT 1",
            ([workstream, GLOBAL_WORKSTREAM], text),
        )
        found = cur.fetchone() is not None
    if not conn.autocommit:
        conn.commit()
    return found


def bootstrap_workstream(
    conn: Any, cfg: WorkstreamConfig
) -> BootstrapResult:
    """Idempotently seed ``cfg``'s memory + set its budget. Safe to re-run.

    Returns a :class:`BootstrapResult` recording which seeds were added vs skipped
    and whether a budget was set. Requires an open ``conn`` (the caller owns the
    transaction boundary, like the rest of :mod:`runtime`).
    """
    result = BootstrapResult(workstream=cfg.name)

    for item in cfg.memory_seed:
        if _seed_exists(conn, cfg.name, item.text):
            result.seeds_skipped += 1
            continue
        # global_lesson=True stores under the shared '*' corpus regardless of the
        # workstream arg; otherwise it is scoped to this workstream (memory.api).
        stored = add_lesson(
            conn,
            cfg.name,
            item.text,
            metadata={"kind": item.kind, "seed": True, "workstream": cfg.name},
            global_lesson=item.global_,
        )
        result.seeds_added.append(str(stored.id) if stored.id else item.text[:32])

    if cfg.budget is not None and (
        cfg.budget.cap_usd is not None or cfg.budget.cap_tokens is not None
    ):
        set_budget(
            conn,
            cfg.name,
            period=cfg.budget.period,
            cap_usd=cfg.budget.cap_usd,
            cap_tokens=cfg.budget.cap_tokens,
        )
        result.budget_set = True

    return result
