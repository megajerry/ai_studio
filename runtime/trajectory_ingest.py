"""Live-session trajectory ingestion bridge (ADR-0020, T5).

Ingest an EXTERNALLY-produced reasoning trajectory — e.g. this off-host PM /
orchestration session, or any agent whose work is NOT already persisted as tasks —
into the guarded trajectory store, so its reasoning becomes measurable and
replayable like any in-runtime episode. This is how a keyless, intermittent
off-host worker (ADR-0010) makes its own orchestration work visible: it exports an
ordered log and feeds it through here.

Discipline: an imported record is mapped ONLY onto the single guarded writer
(:func:`runtime.trajectory.start_trajectory` / :func:`~runtime.trajectory.add_step`
/ :func:`~runtime.trajectory.close_trajectory`) — there are **no ad-hoc INSERTs**.
Step order is preserved (the writer assigns the gapless per-trajectory ``seq``),
and per-step / per-trajectory timestamps are honored when supplied so an imported
episode keeps its real wall-clock shape.

Import format — one JSON object::

    {
      "role": "pm",                          # required
      "workstream": "productivity",          # required
      "goal": "orchestrate the trajectory work",  # required (body: LOCAL DB ONLY)
      "ttl": 604800,                         # optional seconds; omit = never expires
      "context_size_start": 12000,           # optional
      "outcome_summary": "all tracks merged",# optional; stamped on close
      "started_at": "2026-07-21T09:00:00+00:00",  # optional ISO-8601; else DB clock
      "ended_at":   "2026-07-21T18:00:00+00:00",  # optional; the close timestamp
      "close": true,                         # optional (default true): close after ingest
      "steps": [                             # ordered; each mapped to add_step
        {"step_type": "observe", "summary": "...", "rationale": "...",
         "options_considered": ["a", "b"], "choice": "a", "confidence": 0.8,
         "refs": {"task_ids": []}, "context_size": 12500, "tokens": 400,
         "cost_usd": 0.01, "latency_ms": 1200,
         "created_at": "2026-07-21T09:05:00+00:00"}
      ]
    }

Bodies (goal / summary / rationale / outcome_summary) are LOCAL-DB-ONLY (invariants
5 & 6): the ``trajectory.*`` events the writer emits stay body-free. **Do NOT commit
any real PII/secrets in an import file** — the shipped example
(``runtime/trajectory_ingest.example.json``) is synthetic.

CLI — feed one exported trajectory file into the live store::

    python -m runtime.trajectory_ingest runtime/trajectory_ingest.example.json

It prints the new trajectory id. DB-outage-safe (ADR-0017): if the store is
unreachable it reports the degraded signal and exits non-zero rather than hanging.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from typing import Any, Optional
from uuid import UUID

import psycopg
from pydantic import BaseModel, Field

from .db import DBUnavailable, connect_with_retry
from .trajectory import add_step, close_trajectory, start_trajectory


# --- import format (validated, lenient on optional fields) ------------------


class IngestStep(BaseModel):
    """One imported reasoning step (maps 1:1 onto :func:`runtime.trajectory.add_step`)."""

    step_type: str
    summary: str
    rationale: Optional[str] = None
    options_considered: Any = None
    choice: Optional[str] = None
    confidence: Optional[float] = None
    refs: Any = None
    context_size: Optional[int] = None
    tokens: Optional[int] = None
    cost_usd: Optional[float] = None
    latency_ms: Optional[int] = None
    #: Fixes this step's ``created_at`` for a faithful timeline; else the DB clock.
    created_at: Optional[datetime] = None


class IngestRecord(BaseModel):
    """A full imported trajectory: metadata + an ordered list of steps."""

    role: str
    workstream: str
    goal: str
    ttl: Optional[float] = None
    context_size_start: Optional[int] = None
    outcome_summary: Optional[str] = None
    #: Fixes ``started_at`` (and the ``expires_at = started_at + ttl`` horizon).
    started_at: Optional[datetime] = None
    #: The close timestamp (drives ``latency_ms``); else the DB clock.
    ended_at: Optional[datetime] = None
    #: Close the trajectory after ingesting its steps (default True).
    close: bool = True
    steps: list[IngestStep] = Field(default_factory=list)


def ingest_trajectory(
    conn: psycopg.Connection,
    record: Any,
    *,
    now: Optional[datetime] = None,
) -> UUID:
    """Ingest one exported trajectory via the GUARDED writer; return its new id.

    ``record`` is an :class:`IngestRecord` or a mapping validated into one. Opens
    the trajectory (:func:`start_trajectory`), appends each step IN ORDER
    (:func:`add_step` assigns the gapless ``seq``), and — unless ``close`` is
    false — closes it (:func:`close_trajectory`). Per-step/per-trajectory
    timestamps are used when supplied (falling back to ``now``, then the DB clock),
    so an imported episode keeps its real wall-clock shape. NO ad-hoc INSERTs.
    """
    rec = record if isinstance(record, IngestRecord) else IngestRecord.model_validate(record)

    tid = start_trajectory(
        conn,
        rec.role,
        rec.workstream,
        rec.goal,
        ttl=rec.ttl,
        context_size_start=rec.context_size_start,
        now=rec.started_at or now,
    )
    for step in rec.steps:  # order preserved — writer assigns seq 1..N
        add_step(
            conn,
            tid,
            step.step_type,
            step.summary,
            rationale=step.rationale,
            options_considered=step.options_considered,
            choice=step.choice,
            confidence=step.confidence,
            refs=step.refs,
            context_size=step.context_size,
            tokens=step.tokens,
            cost_usd=step.cost_usd,
            latency_ms=step.latency_ms,
            now=step.created_at or now,
        )
    if rec.close:
        close_trajectory(
            conn, tid, outcome_summary=rec.outcome_summary, now=rec.ended_at or now
        )
    return tid


# --- CLI --------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m runtime.trajectory_ingest",
        description="Ingest an exported trajectory JSON file into the guarded store.",
    )
    parser.add_argument("file", help="path to a trajectory JSON file (see module docstring)")
    args = parser.parse_args(argv)

    with open(args.file, "r", encoding="utf-8") as fh:
        record = json.load(fh)

    # DB-outage-safe (ADR-0017): degrade cleanly if the store is unreachable rather
    # than crashing/hanging — the off-host session simply retries later.
    try:
        conn = connect_with_retry()
    except DBUnavailable as exc:
        print(f"trajectory_ingest: database unavailable, not ingested: {exc}", file=sys.stderr)
        return 1
    try:
        tid = ingest_trajectory(conn, record)
        print(tid)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
