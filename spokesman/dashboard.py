"""Mobile-friendly stakeholder status page (ADR-0006 dashboard channel).

Renders :class:`spokesman.runtime_bridge.DashboardSnapshot` as a single HTML
document — task stats + agent stats, no secrets / payloads. Auth is the same
shared ``SPOKESMAN_API_TOKEN`` used by the control plane (query ``?token=`` so a
phone browser can open it after a tunnel).
"""

from __future__ import annotations

import html
from datetime import datetime, timezone

from .runtime_bridge import DashboardSnapshot


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def _bars(items: dict[str, int]) -> str:
    if not items:
        return '<p class="empty">None yet.</p>'
    max_n = max(items.values()) or 1
    rows = []
    for label, n in items.items():
        width = max(4, int(100 * n / max_n))
        rows.append(
            f'<div class="row"><span class="label">{_esc(label)}</span>'
            f'<div class="bar-wrap"><div class="bar" style="width:{width}%"></div></div>'
            f'<span class="n">{n}</span></div>'
        )
    return "\n".join(rows)


def render_dashboard(
    snap: DashboardSnapshot, *, dry_run: bool, token: str | None = None
) -> str:
    """Return a complete HTML document for the snapshot."""
    s = snap.status
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    mode = "DRY-RUN" if dry_run else "LIVE"
    chat_href = f"/chat?token={_esc(token)}" if token else "/chat"
    approvals = (
        "<ul>"
        + "".join(f"<li><code>{_esc(i)}</code></li>" for i in snap.pending_approval_ids)
        + "</ul>"
        if snap.pending_approval_ids
        else '<p class="empty">No pending approvals.</p>'
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <meta http-equiv="refresh" content="30"/>
  <title>AI Studio</title>
  <style>
    :root {{
      --bg: #0f1419; --card: #1a2332; --text: #e7ecf3; --muted: #8b9bb4;
      --accent: #3d8bfd; --ok: #3dd68c; --warn: #f5a524; --bad: #f31260;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0; font-family: ui-sans-serif, system-ui, -apple-system, sans-serif;
      background: radial-gradient(1200px 600px at 10% -10%, #1b2a44, var(--bg));
      color: var(--text); line-height: 1.4; padding: 1rem;
    }}
    header {{ margin-bottom: 1rem; }}
    h1 {{ font-size: 1.4rem; margin: 0 0 .25rem; letter-spacing: -.02em; }}
    .meta {{ color: var(--muted); font-size: .85rem; }}
    .badge {{
      display: inline-block; padding: .15rem .5rem; border-radius: 999px;
      font-size: .7rem; font-weight: 700; letter-spacing: .04em;
      background: #243247; color: var(--muted);
    }}
    .badge.live {{ background: #163528; color: var(--ok); }}
    .badge.dry {{ background: #3a2a12; color: var(--warn); }}
    .grid {{
      display: grid; gap: .75rem;
      grid-template-columns: repeat(auto-fit, minmax(7.5rem, 1fr));
      margin: 1rem 0;
    }}
    .stat {{
      background: var(--card); border: 1px solid #2a364a; border-radius: 12px;
      padding: .85rem .9rem;
    }}
    .stat .k {{ color: var(--muted); font-size: .75rem; text-transform: uppercase; }}
    .stat .v {{ font-size: 1.6rem; font-weight: 700; margin-top: .15rem; }}
    section {{
      background: var(--card); border: 1px solid #2a364a; border-radius: 12px;
      padding: 1rem; margin-bottom: .75rem;
    }}
    h2 {{ font-size: .95rem; margin: 0 0 .75rem; color: var(--muted);
         text-transform: uppercase; letter-spacing: .06em; }}
    .row {{
      display: grid; grid-template-columns: 7.5rem 1fr 2.2rem; gap: .5rem;
      align-items: center; margin: .35rem 0; font-size: .9rem;
    }}
    .label {{ color: var(--text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
    .bar-wrap {{ background: #101826; border-radius: 999px; height: .55rem; overflow: hidden; }}
    .bar {{ background: linear-gradient(90deg, var(--accent), #6aa8ff); height: 100%; }}
    .n {{ text-align: right; color: var(--muted); font-variant-numeric: tabular-nums; }}
    .empty {{ color: var(--muted); margin: 0; }}
    code {{ font-size: .8rem; word-break: break-all; }}
    ul {{ margin: 0; padding-left: 1.1rem; }}
    .nav {{ margin-top: .75rem; font-size: .85rem; }}
    .nav a {{ color: #8bb4ff; }}
  </style>
</head>
<body>
  <header>
    <h1>AI Studio</h1>
    <div class="meta">
      <span class="badge {'dry' if dry_run else 'live'}">{mode}</span>
      · refreshed { _esc(now) } · auto every 30s
    </div>
    <div class="nav"><a href="{chat_href}">Open chat</a></div>
  </header>

  <div class="grid">
    <div class="stat"><div class="k">Open</div><div class="v">{s.open_tasks}</div></div>
    <div class="stat"><div class="k">In progress</div><div class="v">{s.in_progress}</div></div>
    <div class="stat"><div class="k">Blocked</div><div class="v">{s.blocked}</div></div>
    <div class="stat"><div class="k">Approvals</div><div class="v">{s.pending_approvals}</div></div>
    <div class="stat"><div class="k">Done</div><div class="v">{s.done}</div></div>
    <div class="stat"><div class="k">Failed</div><div class="v">{s.failed}</div></div>
    <div class="stat"><div class="k">Tokens</div><div class="v">{s.spent_tokens}</div></div>
    <div class="stat"><div class="k">Trajectories</div><div class="v">{snap.open_trajectories}/{snap.closed_trajectories}</div></div>
  </div>

  <section>
    <h2>Tasks by status</h2>
    {_bars(snap.by_status)}
  </section>
  <section>
    <h2>Tasks by agent type</h2>
    {_bars(snap.by_agent_type)}
  </section>
  <section>
    <h2>Tasks by assignee</h2>
    {_bars(snap.by_assignee)}
  </section>
  <section>
    <h2>Tasks by workstream</h2>
    {_bars(snap.by_workstream)}
  </section>
  <section>
    <h2>Recent event mix</h2>
    <p class="empty">Last 200 events, by type.</p>
    {_bars(snap.recent_event_types)}
  </section>
  <section>
    <h2>Pending approvals</h2>
    {approvals}
  </section>
</body>
</html>
"""
