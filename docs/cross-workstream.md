# Cross-workstream request contract

_The second half of the workstream-bootstrap primitive (the first half is
config/registration — `WorkstreamConfig`, ADR-0018). This half is how one
vertical **asks another to build something**._

Workstreams (verticals) are scope-isolated: their state lives in the DB scoped by
`workstream`, their artifacts in one bucket each, their product in its own repo
(ADR-0018). They **coordinate only through the shared task board + the append-only
event log — never by direct calls** (CLAUDE.md invariants 1 & 2). So a request
from workstream A to workstream B is not a function call into B; it is a typed
task placed on B's board, which B's PM triages on its own schedule.

- Contract + board/event plumbing: `runtime/crossworkstream.py`
- Receiving-PM intake/triage: `runtime/roles/pm.py` (`triage_request`)
- Tests: `runtime/tests/test_crossworkstream_db.py`

## The contract — `FeatureRequest`

A typed ask carried as a task with `type="feature_request"`,
`workstream=to_workstream` (so it lands on — and is only triageable by — the
**receiving** workstream's board):

| field | meaning |
| --- | --- |
| `from_workstream` | who is asking |
| `to_workstream` | who is being asked (owns the task board it lands on) |
| `title` | short label |
| `problem` | the problem the requester has |
| `desired_capability` | the capability they need built |
| `success_criteria[]` | how the requester defines "done" — these become the receiver's work-item criteria on accept |
| `impact` | why it matters (frames the portfolio trade-off) |
| `priority` | task priority for the request + its decomposed work |
| `deadline?` | optional |
| `context_refs[]?` | optional pointers (ids/urls) — no bodies |

The request **bodies** (`problem` / `desired_capability` / `success_criteria`)
live only in the task payload, which is scoped to the receiving workstream. They
are **never** put on the event stream (invariant 5).

## Sub-status state machine

A request carries its own sub-status (in the task payload under `request_status`
and on every `request.*` event as `status`), distinct from the task's lifecycle
status:

```
submitted ──▶ under_review ──┬──▶ accepted            (+ decomposed work items)
                            ├──▶ declined             (reason; no work)
                            ├──▶ needs_clarification  (back to requester)
                            └──▶ escalated            (+ a 🛑 approval)
```

Each `request.*` event carries **identity only** — `request_id`,
`from_workstream`, `to_workstream`, `status`, and for a decision the `decision` +
`reason` (and non-secret counts/ids like `work_item_count` / `approval_id`).
Never a body.

## The flow (API)

**Submit** (requester side):

```python
from runtime.crossworkstream import FeatureRequest, submit_request

task = submit_request(conn, request=FeatureRequest(
    from_workstream="video",
    to_workstream="productivity",
    title="Add a video_audit capability",
    problem="Our shorts fail platform checks (length/captions) before publish.",
    desired_capability="A reusable video_audit check (duration + loudness + captions).",
    success_criteria=["clip duration < 60s", "captions present", "loudness within -14 LUFS"],
    impact="Unblocks the video vertical's launch.",
    priority=5,
), sink=sink)
# → an up_for_grabs `feature_request` task on productivity's board + request.submitted
```

**List** (receiving PM finds what's addressed to it — scope-respecting):

```python
from runtime.crossworkstream import list_requests, STATUS_SUBMITTED
inbox = list_requests(conn, "productivity", status=STATUS_SUBMITTED)
```

**Triage** (receiving PM evaluates through **its own** success lens):

```python
from runtime.roles.pm import triage_request
result = triage_request(conn, task, sink, receiving_workstream="productivity")
```

`triage_request` picks the request up (up_for_grabs → in_progress), records
`under_review`, then takes one of four **first-class** paths. The decision comes
from an explicit `decision=` argument, else an injectable `evaluate` lens, else
the keyless default (accept a well-specified request; ask for clarification if
there is nothing checkable to build against). Pushback (decline) is as valid an
outcome as accept.

- **accept** → decompose into `up_for_grabs` work items on the receiver's board.
  The requester's `success_criteria` become the items' criteria (the requester
  defines "done"; the receiver owns "how"); each item back-links to the request
  (`request_id` / `from_workstream`). Emits `request.accepted`; the request task
  is driven to `merged`.
- **decline** → emits `request.declined` (reason); enqueues **no** work; the
  request task is `abandoned`.
- **needs_clarification** → emits `request.needs_clarification` back to the
  requester; the request returns to `up_for_grabs` so a clarified re-submission
  can be re-triaged.
- **escalate** → emits `request.escalated` and raises a 🛑 `request_approval`
  (a portfolio/resource decision, ADR-0006); the request task is parked
  `blocked` on that approval. Escalation is **symmetric** — either side may
  escalate a cross-workstream trade-off to a human.

**Observe** (requester side, no direct call): the requester watches the
`request.*` event stream and sees its request move `submitted → … → accepted`
(or declined/clarify/escalated). Every `request.*` event names `from_workstream`
so a requester can filter its own stream:

```python
from runtime.events import read_events
outcome = [e for e in read_events(conn, task_id=task.id) if e.type.startswith("request.")]
```

## Worked example — video → productivity `video_audit`

The video vertical needs a `video_audit` capability (the built-in domain checker
from the workstream-config work). It files a `FeatureRequest` (above) onto the
**productivity** board. Productivity's PM triages it through its own lens:

- **accept** → it decomposes the ask into `up_for_grabs` work items whose
  criteria are the video vertical's own (`duration < 60s`, `captions present`,
  `loudness within -14 LUFS`). Those items are then built + verified by the
  productivity fleet exactly like any other work, and the video vertical sees
  `request.accepted` on the stream.
- **decline** → productivity judges it out of scope this quarter; it emits
  `request.declined` with a reason and builds nothing. The push-back is
  first-class — the video vertical reads the reason and can re-scope.
- **escalate** → it is a real cross-portfolio resourcing call; productivity
  emits `request.escalated` and raises a 🛑 approval for a human to weigh both
  verticals' priorities.

## Invariants

- Verticals coordinate via a typed request on the task board — **never** direct
  calls (invariants 1 & 2).
- The receiving PM evaluates through **its own** success lens; decline/pushback
  is first-class (mirrors the PM confidence gate, ADR-0003).
- Escalation is **symmetric** and goes through the existing 🛑 approval loop
  (ADR-0006) — no new escalation channel.
- Scope is respected: a request to B is only listed/triageable as B's; a PM that
  passes a mismatched `receiving_workstream` is refused.
- `request.*` events leak **no bodies** and no secrets/PII (invariants 5 & 6);
  bodies stay in the receiver-scoped task payload.
