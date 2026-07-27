"""Stakeholder web chat — fallback when SMS/WhatsApp is down (ADR-0006).

Same command vocabulary as the messaging channel, plus free-form questions and
requirements (ADR-0026). Shortcuts: ``status``, ``approve <id>``, ``deny <id>``,
``decide <id> <answer>``.
"""

from __future__ import annotations

import html
import json
import re
from typing import Any, Optional


def _js_string(value: str) -> str:
    """Embed ``value`` as a JavaScript string literal inside a ``<script>`` block.

    Must use JSON (not ``html.escape`` / ``repr``): HTML character references are
    **not** decoded inside ``<script>`` in text/html, so ``&#x27;…&#x27;`` is
    invalid JS and the page silently sends a broken token.
    """
    # json.dumps gives a double-quoted JS string; neutralize </script> breakouts.
    return json.dumps(value).replace("<", "\\u003c").replace(">", "\\u003e")


def extract_embedded_token(page_html: str) -> str:
    """Parse ``const TOKEN = …`` from a rendered chat page (for tests)."""
    match = re.search(r"const TOKEN = (.*?);", page_html, flags=re.DOTALL)
    if not match:
        raise ValueError("TOKEN assignment not found in chat HTML")
    literal = match.group(1).strip()
    # HTML entities in a <script> source are NOT decoded by the browser — reject them.
    if re.search(r"&(#\d+|#x[0-9a-fA-F]+|[A-Za-z]+);", literal):
        raise ValueError(
            f"TOKEN literal still contains HTML entities (broken embed): {literal[:40]!r}"
        )
    return json.loads(literal)


def _model_banner(model_mode: Optional[dict[str, Any]]) -> str:
    """HTML-escaped MODEL indicator for the header (ADR-0026).

    Distinct from the CHANNEL indicator: it says whether the Spokesman's *brain* is
    a real provider or the keyless dry-run stub, so a stubbed brain can't hide
    behind a LIVE channel banner. ``model_mode`` is the dict from
    :func:`spokesman.context.spokesman_model_mode`; ``None`` degrades to STUB.
    """
    if not model_mode or model_mode.get("dry_run", True):
        return "model: STUB (dry-run — set ANTHROPIC_API_KEY)"
    model_id = str(model_mode.get("model") or "unknown")
    return f"model: LIVE ({model_id})"


def render_chat(
    *,
    channel: str,
    dry_run: bool,
    token: str,
    model_mode: Optional[dict[str, Any]] = None,
) -> str:
    """Return the chat page. ``token`` is embedded for same-origin API calls only
    (the page is itself token-gated via ``?token=``).

    ``model_mode`` (from :func:`spokesman.context.spokesman_model_mode`) drives a
    distinct MODEL indicator in the header (real provider vs dry-run stub); ``None``
    degrades to STUB — see :func:`_model_banner` (ADR-0026)."""
    mode = "DRY-RUN" if dry_run else "LIVE"
    esc_token = html.escape(token, quote=True)
    esc_channel = html.escape(channel, quote=True)
    esc_model = html.escape(_model_banner(model_mode), quote=True)
    token_js = _js_string(token)
    return f"""\
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>AI Studio Chat</title>
  <style>
    :root {{
      --bg: #0f1419; --panel: #151d2a; --text: #e7ecf3; --muted: #8b9bb4;
      --accent: #3d8bfd; --me: #243247; --bot: #1a2332; --bad: #f31260;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0; min-height: 100dvh; display: flex; flex-direction: column;
      font-family: ui-sans-serif, system-ui, -apple-system, sans-serif;
      background: radial-gradient(900px 500px at 0% 0%, #1b2a44, var(--bg));
      color: var(--text);
    }}
    header {{
      padding: .85rem 1rem; border-bottom: 1px solid #2a364a;
      display: flex; justify-content: space-between; align-items: baseline; gap: .75rem;
    }}
    h1 {{ font-size: 1.05rem; margin: 0; }}
    .meta {{ color: var(--muted); font-size: .75rem; }}
    a {{ color: var(--accent); text-decoration: none; }}
    #log {{
      flex: 1; overflow-y: auto; padding: 1rem; display: flex; flex-direction: column;
      gap: .65rem;
    }}
    .bubble {{
      max-width: min(36rem, 92%); padding: .7rem .85rem; border-radius: 12px;
      white-space: pre-wrap; word-break: break-word; line-height: 1.45; font-size: .95rem;
      border: 1px solid #2a364a;
    }}
    .bot {{ align-self: flex-start; background: var(--bot); }}
    .me {{ align-self: flex-end; background: var(--me); }}
    .sys {{ align-self: center; color: var(--muted); font-size: .8rem; border: none; background: transparent; }}
    .err {{ color: #ff8fa3; }}
    form {{
      display: flex; gap: .5rem; padding: .75rem; border-top: 1px solid #2a364a;
      background: var(--panel);
    }}
    input[type=text] {{
      flex: 1; border-radius: 10px; border: 1px solid #2a364a; background: #101826;
      color: var(--text); padding: .75rem .85rem; font-size: 1rem;
    }}
    button {{
      border: 0; border-radius: 10px; background: var(--accent); color: #fff;
      font-weight: 600; padding: 0 1rem; cursor: pointer;
    }}
    button:disabled {{ opacity: .5; cursor: default; }}
  </style>
</head>
<body>
  <header>
    <div>
      <h1>AI Studio · Spokesman</h1>
      <div class="meta">{esc_channel} · {mode} · web fallback · {esc_model}</div>
    </div>
    <div class="meta"><a href="/dashboard?token={esc_token}">Dashboard</a></div>
  </header>
  <div id="log"></div>
  <form id="f" autocomplete="off">
    <input id="msg" type="text" placeholder="Ask about the studio · or: status · approve &lt;id&gt;" autofocus/>
    <button type="submit" id="send">Send</button>
  </form>
  <script>
    const TOKEN = {token_js};
    const log = document.getElementById('log');
    const form = document.getElementById('f');
    const input = document.getElementById('msg');
    const sendBtn = document.getElementById('send');

    function add(role, text, extraClass) {{
      const d = document.createElement('div');
      d.className = 'bubble ' + role + (extraClass ? ' ' + extraClass : '');
      d.textContent = text;
      log.appendChild(d);
      log.scrollTop = log.scrollHeight;
    }}

    function formatDetail(detail) {{
      if (detail == null) return null;
      if (typeof detail === 'string') return detail;
      try {{ return JSON.stringify(detail); }} catch (_) {{ return String(detail); }}
    }}

    add('bot', 'Web chat is connected. Ask anything about the studio, or give a requirement.');
    add('sys', 'Shortcuts: status · approve <id> · deny <id> · decide <id> <answer> · end handoff');

    form.addEventListener('submit', async (e) => {{
      e.preventDefault();
      const text = input.value.trim();
      if (!text) return;
      add('me', text);
      input.value = '';
      sendBtn.disabled = true;
      try {{
        const res = await fetch('/chat/message?token=' + encodeURIComponent(TOKEN), {{
          method: 'POST',
          headers: {{ 'content-type': 'application/json', 'X-Spokesman-Token': TOKEN }},
          body: JSON.stringify({{ text }}),
        }});
        const data = await res.json();
        if (!res.ok) {{
          add('bot', formatDetail(data.detail) || ('Error ' + res.status), 'err');
        }} else if (data.replies && data.replies.length) {{
          data.replies.forEach(t => add('bot', t));
        }} else {{
          add('sys', data.note || 'No reply (unknown command).');
        }}
      }} catch (err) {{
        add('bot', 'Network error: ' + err, 'err');
      }} finally {{
        sendBtn.disabled = false;
        input.focus();
      }}
    }});
  </script>
</body>
</html>
"""


class CaptureClient:
    """Messaging client that records outbound text for the web UI.

    Optionally forwards to a live channel client; failures there do not block the
    web reply (this surface exists precisely when SMS/WhatsApp is broken).
    """

    def __init__(self, inner: object | None = None) -> None:
        self.replies: list[str] = []
        self._inner = inner

    def send_text(self, text: str, *, to: str | None = None) -> dict:
        self.replies.append(text)
        if self._inner is not None:
            try:
                return self._inner.send_text(text, to=to)  # type: ignore[union-attr]
            except Exception as exc:  # noqa: BLE001 - channel may be down
                return {
                    "captured": True,
                    "channel_error": type(exc).__name__,
                    "to": to,
                    "text": text,
                }
        return {"captured": True, "to": to, "text": text}
