"""Public SMS opt-in / consent-evidence page (Twilio 10DLC A2P campaign review).

Served unauthenticated at ``GET /sms-opt-in`` so a stable HTTPS URL can be
pasted into the Twilio A2P campaign registration as the call-to-action /
consent-evidence page. This is a plain HTML document — not legal advice; it
describes how the stakeholder opts in to *this* AI Studio / Spokesman instance
and mirrors the A2P disclosures established in the Terms and Privacy pages.
"""

from __future__ import annotations

SMS_OPT_IN_HTML = """\
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>SMS Opt-In &amp; Consent — AI Studio</title>
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
  <h1>SMS Opt-In &amp; Consent</h1>
  <p class="meta">AI Studio · Spokesman messaging channel · Effective 2026-07-26</p>

  <p>
    This page describes the SMS program operated by the account owner
    (&ldquo;Operator&rdquo;) through the <strong>Spokesman</strong> messaging
    channel, and how the authorized stakeholder consents to receive messages. It
    is provided as consent evidence for Twilio 10DLC / A2P campaign review.
  </p>

  <h2>1. Program</h2>
  <ul>
    <li>Program name: <strong>Jerry Studio / AI Studio Spokesman</strong>.</li>
    <li>Purpose: operational approvals, status digests, and alarms for the
      authorized stakeholder. This is <strong>not marketing</strong> and is not a
      mass-market consumer product; messages are strictly operational.</li>
  </ul>

  <h2>2. How the stakeholder opts in</h2>
  <p>
    Consent is one-to-one and explicit. The Operator is the sole authorized
    stakeholder: the Operator provides and confirms their own mobile number
    during the private onboarding of this AI Studio instance and enables SMS
    delivery in the instance configuration. That configuration step is the
    call-to-action and the record of opt-in — no public web form collects
    numbers, and no number is added to the program without the owner&rsquo;s
    explicit action.
  </p>
  <p>
    <strong>We do not sell, rent, share, or transfer your mobile number or
    messaging consent to any third party for their own use, and we do not sell,
    rent, or transfer opt-in lists.</strong> Numbers are used only to deliver
    this program&rsquo;s messages.
  </p>

  <h2>3. Message frequency &amp; rates</h2>
  <ul>
    <li><strong>Message frequency varies</strong> with studio activity (alarms
      may be immediate; digests may be batched).</li>
    <li><strong>Message &amp; data rates may apply</strong> from your carrier.</li>
  </ul>

  <h2>4. Help &amp; opting out</h2>
  <ul>
    <li>Reply <code>HELP</code> for help.</li>
    <li>Reply <code>STOP</code> to cancel and stop receiving further messages
      from this program at any time.</li>
    <li>You may also opt out by asking the Operator to remove your number.</li>
  </ul>

  <h2>5. Privacy &amp; terms</h2>
  <p>
    How information is handled is described in the
    <a href="/privacy">Privacy Policy</a>; program use is governed by the
    <a href="/terms">Terms of Service</a>. As stated there, we do not sell,
    rent, share, or transfer your mobile number or messaging consent to third
    parties for marketing purposes.
  </p>

  <h2>6. Contact</h2>
  <p>
    Questions about this SMS program should be directed to the Operator who
    configured the Twilio integration for this AI Studio instance.
  </p>

  <p class="meta">
    This page is provided for transparency and provider compliance for a private
    automation deployment. It is not a substitute for legal advice.
  </p>
</body>
</html>
"""


def render_sms_opt_in() -> str:
    return SMS_OPT_IN_HTML
