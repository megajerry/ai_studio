"""DB-outage resilience + remote-access allowlist tests (ADR-0017). Keyless.

Three groups, none of which need a live database (an outage is simulated with a
dead port / injected fakes, so these always run — no DB-skips):

- the degraded-mode contract (`connect_with_retry` / `DBUnavailable`): a dead DSN
  degrades cleanly (bounded retries, then the single signal — never crash/hang);
- the supervisor reconnect grace window (`GraceTracker` / `supervised_sweep`): no
  mass re-kick immediately on reconnect, but re-kicks resume once it elapses;
- the pg_hba allowlist template + renderer: scram-sha-256, host allowlist, and a
  hard refusal of `trust` / `0.0.0.0/0`.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from runtime import db
from runtime.db import DBUnavailable, connect_with_retry
from runtime.models import Task, TaskStatus
from runtime.supervisor import GraceTracker, supervised_sweep

# A DSN that is guaranteed unreachable (port 1 is never a Postgres) — the outage.
DEAD_DSN = "postgresql://aistudio@127.0.0.1:1/aistudio"


# --- Degraded-mode contract -------------------------------------------------


def test_connect_with_retry_raises_dbunavailable_on_dead_dsn():
    """A dead DSN degrades to the single DBUnavailable signal — no raw driver
    error, no crash, and (short timeout) no hang."""
    delays: list[float] = []
    with pytest.raises(DBUnavailable) as ei:
        connect_with_retry(
            DEAD_DSN, attempts=3, base_delay_s=0.5, connect_timeout=1,
            sleep=delays.append,  # don't actually wait
        )
    err = ei.value
    assert err.attempts == 3
    assert err.last_error is not None  # underlying cause carried for logging
    assert err.__cause__ is err.last_error
    # Bounded exponential backoff between (attempts-1) failures: 0.5, 1.0.
    assert delays == [0.5, 1.0]


def test_connect_with_retry_backoff_is_capped(monkeypatch):
    """Backoff doubles but never exceeds max_delay_s."""
    calls = {"n": 0}

    def boom(url=None, *, connect_timeout=None):
        calls["n"] += 1
        raise OSError("refused")

    monkeypatch.setattr(db, "connect", boom)
    delays: list[float] = []
    with pytest.raises(DBUnavailable):
        connect_with_retry(
            DEAD_DSN, attempts=5, base_delay_s=1.0, max_delay_s=3.0,
            sleep=delays.append,
        )
    assert calls["n"] == 5              # tried exactly `attempts` times
    assert delays == [1.0, 2.0, 3.0, 3.0]  # 1,2,(4→cap3),(8→cap3)


def test_connect_with_retry_returns_on_eventual_success(monkeypatch):
    """Recovers without raising once a later attempt connects (no DB needed)."""
    sentinel = object()
    seq = iter([OSError("down"), OSError("down"), sentinel])

    def flaky(url=None, *, connect_timeout=None):
        item = next(seq)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(db, "connect", flaky)
    got = connect_with_retry(DEAD_DSN, attempts=5, sleep=lambda d: None)
    assert got is sentinel


def test_connect_with_retry_on_retry_hook_fires_per_backoff(monkeypatch):
    def boom(url=None, *, connect_timeout=None):
        raise OSError("refused")

    monkeypatch.setattr(db, "connect", boom)
    seen: list[int] = []
    with pytest.raises(DBUnavailable):
        connect_with_retry(
            DEAD_DSN, attempts=3, sleep=lambda d: None,
            on_retry=lambda n, d, e: seen.append(n),
        )
    assert seen == [1, 2]  # fired before each of the 2 backoffs, not after the last


def test_can_connect_false_on_dead_dsn_never_raises():
    assert db.can_connect(DEAD_DSN, timeout=1) is False


# --- Reconnect grace window (anti thundering-herd) --------------------------


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def test_clean_first_connect_arms_no_grace():
    clk = _Clock()
    g = GraceTracker(grace_s=60, monotonic=clk)
    g.note_connected()  # startup connect, no prior failure
    assert g.in_grace() is False
    assert g.grace_remaining() == 0.0


def test_reconnect_after_outage_arms_grace_then_expires():
    clk = _Clock()
    g = GraceTracker(grace_s=60, monotonic=clk)
    g.note_failure()      # DB went down
    g.note_connected()    # recovered → grace armed
    assert g.in_grace() is True
    assert g.grace_remaining() == pytest.approx(60.0)
    clk.advance(59)
    assert g.in_grace() is True
    clk.advance(2)        # now past the 60s window
    assert g.in_grace() is False
    assert g.grace_remaining() == 0.0


def test_grace_zero_never_defers():
    g = GraceTracker(grace_s=0)
    g.note_failure()
    g.note_connected()
    assert g.in_grace() is False


def _task(retries: int = 0) -> Task:
    now = datetime.now(timezone.utc)
    return Task(
        id=uuid4(), workstream="ws", type="t", status=TaskStatus.IN_PROGRESS,
        priority=0, retries=retries, created_at=now, updated_at=now,
    )


def test_supervised_sweep_defers_all_rekicks_during_grace():
    """The core anti thundering-herd guarantee: while in grace, NOT ONE stale task
    is re-kicked (find_stale/rekick are never even consulted)."""
    clk = _Clock()
    g = GraceTracker(grace_s=60, monotonic=clk)
    g.note_failure()
    g.note_connected()  # armed

    calls = {"find": 0, "rekick": 0}

    def find_stale(conn, thr):
        calls["find"] += 1
        return [_task(), _task(), _task()]  # everything looks stale post-outage

    def rekick(conn, task):
        calls["rekick"] += 1
        return task

    deferred = supervised_sweep(
        object(), g, threshold_s=60, max_retries=5,
        find_stale=find_stale, rekick=rekick,
    )
    assert deferred is None            # sweep deferred
    assert calls["rekick"] == 0        # NO re-kick — no stampede
    assert calls["find"] == 0          # didn't even scan


def test_supervised_sweep_rekicks_after_grace_elapses():
    clk = _Clock()
    g = GraceTracker(grace_s=60, monotonic=clk)
    g.note_failure()
    g.note_connected()
    clk.advance(61)  # window elapsed → live workers had time to re-heartbeat

    stale = [_task(), _task()]
    kicked: list = []
    res = supervised_sweep(
        object(), g, threshold_s=60, max_retries=5, nudge_grace_s=0,
        find_stale=lambda c, t: stale,
        rekick=lambda c, task: (kicked.append(task.id) or task),
    )
    assert res is not None
    assert set(res.rekicked) == {t.id for t in stale}
    assert len(kicked) == 2


# --- Supervisor run() loop: degrade on outage + grace on reconnect ----------


class _Break(Exception):
    """Sentinel to break the supervisor's forever-loop from a fake sleep."""


def test_supervisor_run_degrades_on_outage_without_crashing(monkeypatch):
    """A persistently-unreachable DB must NOT crash the supervisor: it catches the
    DBUnavailable degraded signal and retries after the interval sleep."""
    import runtime.supervisor as sup

    monkeypatch.setattr(
        sup, "connect_with_retry",
        lambda *a, **k: (_ for _ in ()).throw(DBUnavailable("down", attempts=3)),
    )
    sleeps = {"n": 0}

    def fake_sleep(_s):
        sleeps["n"] += 1
        raise _Break  # break out after reaching the end-of-loop sleep

    monkeypatch.setattr(sup.time, "sleep", fake_sleep)

    with pytest.raises(_Break):  # only our sentinel escapes — NOT DBUnavailable
        sup.run(interval_s=0, threshold_s=1, max_retries=1, grace_s=1)
    assert sleeps["n"] == 1  # degraded cleanly, reached the retry sleep


def test_supervisor_run_grace_defers_rekick_on_reconnect(monkeypatch):
    """After an outage→reconnect, the run loop arms the grace window and DEFERS the
    re-kick sweep (no thundering herd), instead of sweeping immediately."""
    import runtime.supervisor as sup

    class _FakeConn:
        closed = False

    seq = iter([DBUnavailable("down"), _FakeConn()])  # iter1 outage, iter2 recover

    def connect(*a, **k):
        item = next(seq)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(sup, "connect_with_retry", connect)

    swept = {"n": 0}
    monkeypatch.setattr(
        sup, "sweep",
        lambda *a, **k: (swept.__setitem__("n", swept["n"] + 1) or sup.SweepResult()),
    )

    sleeps = {"n": 0}

    def fake_sleep(_s):
        sleeps["n"] += 1
        if sleeps["n"] >= 2:  # let two iterations run (outage, then reconnect)
            raise _Break

    monkeypatch.setattr(sup.time, "sleep", fake_sleep)

    with pytest.raises(_Break):
        sup.run(interval_s=0, threshold_s=1, max_retries=1, grace_s=999)
    # iter2 reconnected → grace armed → supervised_sweep deferred → sweep NOT called.
    assert swept["n"] == 0


# --- Remote-access pg_hba allowlist template + renderer ----------------------

_REPO = Path(__file__).resolve().parents[2]
_PG = _REPO / "infra" / "postgres"
_TEMPLATE = _PG / "pg_hba.conf.template"
_RENDER = _PG / "render-pg-hba.sh"


def test_pg_hba_template_is_allowlist_scram_never_trust():
    text = _TEMPLATE.read_text()
    assert "scram-sha-256" in text.lower()
    rule_lines = _rule_lines(text)
    assert rule_lines, "template has no auth rules"
    for ln in rule_lines:
        fields = ln.split()
        assert fields[0] in {"local", "host", "hostssl", "hostnossl"}, ln
        # No password-less `trust` auth, and never the internet as an address —
        # on any actual RULE (the header comments legitimately name them to forbid).
        assert fields[-1] == "scram-sha-256", f"non-scram method: {ln}"
        assert "0.0.0.0/0" not in fields, f"internet CIDR in rule: {ln}"
        assert "::/0" not in fields, f"internet CIDR in rule: {ln}"
    assert "@PG_ALLOWED_HOSTS@" in text  # the allowlist injection marker


def _render(allowlist: str):
    return subprocess.run(
        ["sh", str(_RENDER), str(_TEMPLATE)],
        env={"PG_ALLOWED_HOSTS": allowlist, "POSTGRES_DB": "aistudio",
             "POSTGRES_USER": "aistudio", "PATH": "/usr/bin:/bin"},
        capture_output=True, text=True,
    )


def _rule_lines(text: str) -> list[str]:
    return [ln for ln in text.splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")]


def test_render_expands_allowlist_to_scram_host_rules():
    out = _render("203.0.113.5/32, 198.51.100.0/24")
    assert out.returncode == 0, out.stderr
    assert "host    aistudio    aistudio    203.0.113.5/32    scram-sha-256" in out.stdout
    assert "198.51.100.0/24" in out.stdout
    # No rule line uses `trust` or the internet CIDR (header comments may name
    # them to forbid them — scope the check to actual rules).
    for ln in _rule_lines(out.stdout):
        fields = ln.split()
        assert fields[-1] == "scram-sha-256", f"non-scram method: {ln}"
        assert "0.0.0.0/0" not in fields, f"internet CIDR in rule: {ln}"
    # Rendered file has no unresolved marker.
    assert "@PG_ALLOWED_HOSTS@" not in out.stdout


def test_render_empty_allowlist_has_no_remote_rules():
    out = _render("")
    assert out.returncode == 0, out.stderr
    # No `host … <public/remote CIDR> …` beyond the internal/loopback ones.
    for ln in out.stdout.splitlines():
        if ln.startswith("host") and "scram-sha-256" in ln:
            addr = ln.split()[3]
            assert addr in {"127.0.0.1/32", "::1/128", "172.28.0.0/16"}, addr


def test_render_refuses_internet_wide_cidr():
    for internet in ("0.0.0.0/0", "::/0"):
        out = _render(internet)
        assert out.returncode == 2, f"{internet} should be refused: {out.stdout}"
        assert "refusing" in out.stderr.lower()
