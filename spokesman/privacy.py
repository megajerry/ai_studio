"""Public Privacy Policy page (required by Twilio / messaging providers).

Served unauthenticated at ``GET /privacy`` so a stable HTTPS URL can be pasted
into provider consoles. This is a plain HTML document — not legal advice; it
describes how *this* AI Studio / Spokesman instance handles messaging data.
"""

from __future__ import annotations

PRIVACY_HTML = """\
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Privacy Policy — AI Studio</title>
  <style>
    :root { color-scheme: light dark; }
    body {
      margin: 0 auto; max-width: 42rem; padding: 1.5rem 1.25rem 3rem;
      font-family: ui-sans-serif, system-ui, -apple-system, sans-serif;
      line-height: 1.55; color: #142033;
      background: #f6f8fb;
    }
    @media (prefers-color-scheme: dark) {
      body { color: #e8eef8; background: #0f1419; }
      a { color: #8bb4ff; }
      code { background: #1a2332; }
    }
    h1 { font-size: 1.6rem; margin: 0 0 .35rem; letter-spacing: -.02em; }
    h2 { font-size: 1.05rem; margin: 1.6rem 0 .5rem; }
    p, li { font-size: .95rem; }
    .meta { color: #5b6b82; font-size: .85rem; margin-bottom: 1.25rem; }
    @media (prefers-color-scheme: dark) { .meta { color: #8b9bb4; } }
    code {
      font-size: .85em; padding: .1em .35em; border-radius: 4px;
      background: #e8eef8;
    }
    ul { padding-left: 1.2rem; }
  </style>
</head>
<body>
  <h1>Privacy Policy</h1>
  <p class="meta">AI Studio · Spokesman messaging channel · Effective 2026-07-26</p>

  <p>
    This Privacy Policy describes how the operator of this <strong>AI Studio</strong>
    instance (&ldquo;we&rdquo;, &ldquo;us&rdquo;) handles information when you
    communicate with the <strong>Spokesman</strong> service over SMS and/or
    WhatsApp (via providers such as Twilio or Meta), or when you use related
    status pages hosted by this instance.
  </p>

  <h2>1. Who this is for</h2>
  <p>
    AI Studio is a local-first, private automation system for a single
    stakeholder (or a small invited set of operators). It is <strong>not</strong>
    a public consumer social product. Messaging is used to deliver operational
    alerts and to receive short commands (for example status checks and
    approve/deny replies).
  </p>

  <h2>2. Information we collect</h2>
  <p>Depending on how you use the channel, we may process:</p>
  <ul>
    <li><strong>Phone numbers</strong> you provide for WhatsApp/SMS delivery and replies.</li>
    <li><strong>Message content</strong> you send to or receive from the Spokesman
      (commands, acknowledgements, and short operational notices).</li>
    <li><strong>Delivery metadata</strong> from the messaging provider (timestamps,
      message IDs, delivery status) needed to operate the channel.</li>
    <li><strong>Technical logs</strong> on the host (request times, health checks)
      that may include IP addresses of webhook callers (typically the provider).</li>
  </ul>
  <p>
    We do <strong>not</strong> sell personal information. We do not use messaging
    content for advertising. Secrets and API credentials are stored only in the
    operator&rsquo;s private host configuration, not in public repositories.
  </p>

  <h2>3. How we use information</h2>
  <ul>
    <li>To send studio notifications (approvals, informs, alarms).</li>
    <li>To process inbound commands that control or query the studio.</li>
    <li>To secure and debug the webhook and service (abuse prevention, outages).</li>
    <li>To comply with provider requirements (e.g. Twilio A2P / WhatsApp rules).</li>
  </ul>

  <h2>4. Sharing</h2>
  <p>
    Message transport necessarily involves the messaging provider you configure
    (for example <strong>Twilio</strong> and/or <strong>Meta WhatsApp</strong>).
    Those providers process content and phone numbers under their own terms and
    privacy policies solely to deliver the service.
  </p>
  <p>
    <strong>We do not share, sell, or provide your mobile phone number or
    messaging consent data to third parties or affiliates for marketing or
    promotional purposes.</strong>
    We do not sell, rent, or transfer opt-in lists.
  </p>
  <p>
    <strong>Message frequency varies</strong> based on studio activity (approval
    requests, digests, and alarms).
    <strong>Message and data rates may apply.</strong>
  </p>

  <h2>5. Retention</h2>
  <p>
    Inbound message logs kept on the host are operational working state and may
    be rotated or deleted by the operator. Studio event/task records live in the
    operator&rsquo;s private database. You may ask the operator to delete messaging
    logs associated with your number.
  </p>

  <h2>6. Security</h2>
  <p>
    Access to control APIs is gated by shared secrets. Webhooks are verified with
    provider signatures where supported. The service is intended to run on the
    operator&rsquo;s controlled host; no method of transmission over the Internet
    is perfectly secure.
  </p>

  <h2>7. Your choices</h2>
  <ul>
    <li>Stop participating by asking the operator to remove your number from the
      stakeholder allowlist.</li>
    <li>For Twilio WhatsApp sandbox: reply <code>stop</code> to leave the sandbox
      session (provider-specific).</li>
    <li>For SMS: reply <code>STOP</code> where supported by the provider/campaign.</li>
  </ul>

  <h2>8. Children</h2>
  <p>
    The service is not directed at children under 13, and we do not knowingly
    collect information from children.
  </p>

  <h2>9. Changes</h2>
  <p>
    We may update this page as the messaging setup changes. The &ldquo;Effective&rdquo;
    date at the top will be revised when material changes are made.
  </p>

  <h2>10. Contact</h2>
  <p>
    For privacy requests related to this AI Studio instance, contact the operator
    who invited you to the messaging channel (the account that configured the
    Twilio / WhatsApp integration).
  </p>

  <p>
    Related: <a href="/terms">Terms of Service</a>.
  </p>

  <p class="meta">
    This page is provided for transparency and provider compliance for a private
    automation deployment. It is not a substitute for legal advice.
  </p>
</body>
</html>
"""


def render_privacy_policy() -> str:
    return PRIVACY_HTML
