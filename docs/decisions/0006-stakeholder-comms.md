# 0006 — Stakeholder communication taxonomy & Spokesman

- **Status:** Accepted
- **Date:** 2026-07-21

## Context

The stakeholder spends **< 4 hrs/day** on the project. Upward communication must
be **high-signal and aggregated** to minimize churn, while still allowing
immediate reaction to genuine emergencies. A single interface must represent the
state of *all* agents/workstreams.

## Decision

A **Spokesman** service aggregates all-workstream state from the event log and is
the human interface. Messages are classified:

| Class | What | Behavior |
| --- | --- | --- |
| 🛑 **Approve (blocks)** | redefining product/workstream **objective**; requesting **additional budget** | Blocks that item; **batched into a periodic digest** (default daily). |
| 📣 **Inform (non-blocking)** | major milestone; major mistake + recovery; spend change **within** approved budget | Written to the feed; work continues. |
| 🚨 **Alarm (interrupt)** | active attack, PR disaster, major security breach | **Immediate; repeats until acknowledged.** The genuine few only. |

Channels: **both** —

- a **local-hosted dashboard** (deep, full-state console; remote via tunnel), and
- **WhatsApp** (live push + quick approvals).

The Spokesman posts 🛑/🚨 to WhatsApp and maintains the dashboard; stakeholder
replies re-enter the system as events/tasks. This is the human-facing projection
of the 🔴 action tier ([architecture §5](../architecture.md)).

## Consequences

- Approvals are batched by default → fewer interruptions; only 🚨 interrupts.
- The PM must classify outputs into these tiers; misclassification is a bug to
  catch in review/retro.
- Requires an inbound webhook path (tunnel) and a WhatsApp Business number, or a
  bridge — provisioning TBD with the stakeholder.
- WhatsApp Business Cloud API is a Meta product and is an acceptable channel.

## Open items

- Confirm WhatsApp provisioning (Cloud API vs Twilio) and tunnel choice
  (cloudflared / tailscale) on the target host.
