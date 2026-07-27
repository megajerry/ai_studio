"""Public Terms of Service page (often required alongside Privacy Policy).

Served unauthenticated at ``GET /terms`` for messaging-provider consoles.
Describes use of this AI Studio / Spokesman instance — not general legal advice.
"""

from __future__ import annotations

TERMS_HTML = """\
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Terms of Service — AI Studio</title>
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
  <h1>Terms of Service</h1>
  <p class="meta">AI Studio · Spokesman messaging channel · Effective 2026-07-26</p>

  <p>
    These Terms of Service (&ldquo;Terms&rdquo;) govern use of the
    <strong>AI Studio</strong> instance operated by the account owner
    (&ldquo;Operator&rdquo;) and its <strong>Spokesman</strong> messaging
    channel (SMS and/or WhatsApp via providers such as Twilio or Meta).
    By messaging the service, joining a messaging sandbox, or otherwise using
    the channel, you agree to these Terms.
  </p>

  <h2>1. The service</h2>
  <p>
    AI Studio is a private, local-first automation system. The Spokesman channel
    delivers operational notifications (for example approvals, status updates,
    and alarms) and accepts short inbound commands from authorized recipients.
    It is not a public communications platform and is not offered as a
    mass-market consumer product.
  </p>

  <h2>2. Eligibility and authorization</h2>
  <ul>
    <li>You must be invited or otherwise authorized by the Operator to use the channel.</li>
    <li>You must provide a phone number you control for SMS/WhatsApp delivery.</li>
    <li>You are responsible for keeping access to that number secure.</li>
  </ul>

  <h2>3. Acceptable use</h2>
  <p>You agree not to:</p>
  <ul>
    <li>Use the channel for unlawful, abusive, or harassing communications.</li>
    <li>Attempt to bypass authentication, forge webhooks, or disrupt the service.</li>
    <li>Send content that violates your messaging provider&rsquo;s policies
      (including Twilio and WhatsApp/Meta policies).</li>
    <li>Use the service to spam or message people who have not opted in.</li>
  </ul>

  <h2>4. Messaging and opt-out</h2>
  <ul>
    <li>Message frequency varies with studio activity (alerts may be immediate;
      digests may be batched).</li>
    <li>Message and data rates may apply from your carrier.</li>
    <li>You may opt out by asking the Operator to remove your number, and/or by
      using provider opt-out keywords where supported (e.g. SMS <code>STOP</code>,
      WhatsApp sandbox <code>stop</code>).</li>
  </ul>

  <h2>5. No warranty</h2>
  <p>
    The service is provided <strong>&ldquo;as is&rdquo;</strong> for the
    Operator&rsquo;s private use. The Operator does not warrant uninterrupted
    availability, delivery of every message, or fitness for any particular
    purpose. Automation outputs may be incorrect; human judgment remains required
    for consequential actions.
  </p>

  <h2>6. Limitation of liability</h2>
  <p>
    To the maximum extent permitted by law, the Operator is not liable for
    indirect, incidental, special, consequential, or punitive damages, or for
    loss of data, profits, or business arising from use of the messaging channel
    or reliance on automated notifications.
  </p>

  <h2>7. Third-party services</h2>
  <p>
    Messaging is delivered through third parties (for example Twilio and/or Meta).
    Your use of those networks is also subject to their terms and policies. The
    Operator is not responsible for outages or enforcement actions by those
    providers.
  </p>

  <h2>8. Privacy</h2>
  <p>
    How information is handled is described in the
    <a href="/privacy">Privacy Policy</a>.
  </p>

  <h2>9. Changes and termination</h2>
  <p>
    The Operator may update these Terms, suspend the channel, or terminate access
    at any time (including for abuse or provider requirements). Continued use
    after an update constitutes acceptance of the revised Terms.
  </p>

  <h2>10. Contact</h2>
  <p>
    Questions about these Terms should be directed to the Operator who configured
    the Twilio / WhatsApp integration for this AI Studio instance.
  </p>

  <p class="meta">
    These Terms are provided for transparency and provider compliance for a
    private automation deployment. They are not a substitute for legal advice.
  </p>
</body>
</html>
"""


def render_terms() -> str:
    return TERMS_HTML
