# Competitive Landscape

A starter map of who else plays in or near AutoSDR's territory and what to mine from them. **Use this as a launchpad, not a final answer** — refresh by re-running the research playbook ([research-playbook.md](research-playbook.md) → "Competitor scan") at least quarterly. Date the last refresh in the header when you do.

**Last refreshed:** 2026-04 (initial draft, pre-research).

---

## How to use this file

When forecasting or brainstorming, work like this:

1. Pick the 2–3 most-similar tools from the segments below.
2. For each, ask: "What is the one thing this tool gets *right* that AutoSDR doesn't?" and "What is the one thing it gets *wrong* that AutoSDR is already better at?"
3. Translate the wins into candidate features. **Apply the principle filter** ([product-context.md § 3](product-context.md)) — most enterprise SDR features will fail it. That's fine; the goal is to mine ideas, not copy roadmaps.
4. Cite the source and the date you observed the behaviour. Tools change.

---

## Segments AutoSDR competes (or doesn't) in

### Direct: SMB outbound for owner-operators

The closest fit. Tools designed for someone who is doing their own sales.

- **Lemlist** — Cold email + SMS + LinkedIn for SMBs. Strengths: persona-based personalization, "liquid" template variables, deliverability tooling. Weak fit for AutoSDR persona: paid SaaS, multi-tenant, email-first. Mine: their personalization variables UX, their warmup flows, their reply-handling inbox.
- **Smartlead / Instantly** — Email-first sequencers with mailbox-rotation infra. Mine: their reply categorization, sender-rotation patterns. Don't mine: their "infinite mailboxes" angle (off-strategy for SMS).
- **Reply.io** — Multi-channel cadence platform. Mine: their cadence builder UX, channel orchestration. Don't mine: their CRM-centric assumptions.

### Adjacent: AI-native SDR platforms

Newer cohort positioning AI agents as the SDR.

- **11x.ai (Alice)** — Closed-source AI SDR-as-a-service. Mine: how they frame the agent in the UI, what handoff moments they highlight. Don't mine: anything black-box; their model fine-tuning angle is off-strategy.
- **Artisan (Ava)** — Similar pitch. Mine: their persona-research surfacing.
- **Regie.ai** — AI cadence + content platform. Mine: their content QA loop (relevant to AutoSDR's evaluator).

### Enterprise SDR platforms (mostly NOT for us)

These exist but are wrong-shape for AutoSDR's persona. Mine carefully — most features will fail the principle filter.

- **Outreach.io / Salesloft** — Enterprise sequencers. Mine: their reporting/funnel views (lightly). Don't mine: anything around team workflows, manager dashboards, RBAC.
- **Apollo.io** — Data + outreach combined. Mine: their lead-data presentation patterns. Don't mine: their enrichment-as-feature (it's a non-goal for the POC).

### Open-source / self-hosted comparables

The closest philosophical neighbours.

- **Mautic** — Open-source marketing automation. Strength: self-hosted, extensible. Mine: their extension/plugin architecture, their connector model. Different shape: marketing-batch, not 1:1 SDR.
- **listmonk** — Self-hosted newsletter sender. Mine: their UX choices for an opinionated single-purpose tool.
- **Chatwoot** — Open-source support inbox. Mine: their HITL inbox UX, their conversation-routing primitives.

### SMS-specific gateways and tooling

Not competitors, but the ecosystem AutoSDR plugs into.

- **TextBee, SMSGate** — Already supported. Track their roadmaps for new APIs (push inbound, delivery receipts, MMS).
- **Twilio Messaging** — Cloud SMS. Mine: their conversation-API ergonomics. Don't add as a connector unless an operator explicitly asks (cloud SMS has trust/cost shape that the Android-gateway design deliberately avoids).
- **Signal-CLI / Matrix bridges** — Niche but on-brand for self-hosted operators.

---

## Recurring "valuable" features (across multiple competitors)

Things multiple tools do that operators consistently rate as valuable. Each one is a forecast candidate; verify against the principle filter before scoping.

| Feature | Seen in | Why operators value it | AutoSDR fit |
| --- | --- | --- | --- |
| Reply categorization (positive / objection / OOO / neg) | Lemlist, Reply, Smartlead | Triage in seconds | **Strong** — already shipped; consider expanding categories. |
| AI-drafted reply suggestions in inbox | Reply, Front, Lemlist | Don't write from scratch | **Strong** — already shipped (HITL); refine ranking + personalization carry-over. |
| Sender warmup / deliverability | Lemlist, Smartlead, Instantly | Email is dying without it | **Off-strategy** — SMS doesn't have warmup. Note: if email connector ships, this becomes relevant. |
| LinkedIn + Email + SMS in one cadence | Lemlist, Reply | Channel diversity beats fatigue | **Conditional** — second connector (email) plausibly fits; LinkedIn likely off-strategy (TOS, single-operator). |
| Lead enrichment from URL/domain | Apollo, Clay | Personalization fuel | **Non-goal** for POC. Re-evaluate post-MVP. |
| A/B testing of openers / subject lines | Lemlist, Smartlead | Operator wants to learn | **Strong** — fits the audit-log / iterate philosophy. Cheap to add. |
| Tone "voice clone" from past messages | Lavender, Lemlist (newer) | Sounds-like-me at scale | **Strong** — aligns with tone-calibration spec in doc4. |
| Conversation handoff to a human | Chatwoot, most CRMs | Trust + escalation | **Already strong** — HITL is core. Mine for UX patterns. |
| Multi-mailbox / multi-device rotation | Smartlead, Instantly | Sender-cap mitigation | **Conditional** — Android gateway is one device. Multi-device is a real operator pain at volume; consider as a connector-layer feature. |
| Workflow automation (e.g. "if reply, do X") | Reply, Outreach | Low-code branching | **Caution** — risks violating "human always wins" if branches auto-send. Could fit as HITL-only side effects (mark as won, schedule follow-up). |
| Reporting / pipeline dashboards | Apollo, Outreach | "Am I winning?" | **Light fit** — single-operator doesn't need enterprise dashboards, but a clean per-campaign funnel view is plausible. |
| Inbox-style HITL UI | Chatwoot, Front | Speed | **Already shipped** (Inbox route) — refine, don't rebuild. |
| Audit log of every AI decision | (Rare; AutoSDR is unusual here) | Trust, debuggability | **AutoSDR's moat** — already shipped; consider exposing more of it in the UI. |

---

## What AutoSDR does that competitors *don't*

These are the strategic moats. Protect them when forecasting.

- **First-message-only by default.** Almost everyone else pushes auto-reply. AutoSDR makes HITL the path of least resistance.
- **Full audit log of every LLM call**, prompts and all, in the UI and on disk. Most closed tools won't or can't.
- **Self-hosted, BYO LLM, BYO connector.** No vendor lock-in.
- **Single-operator-shaped UI.** No teams, no RBAC, no manager dashboards.
- **Android-phone gateway.** No tunnels / public URL / Twilio costs.
- **Killswitch with three redundant layers.** Pause is genuinely <1s.

When forecasting, **prefer features that compound on these moats** over features that close a parity gap with enterprise tools.

---

## Watchlist

Sources to check during a quarterly refresh:

- Each tool's changelog or "what's new" page (cite URL when scanning).
- Hacker News threads on "I built / used $tool". The comments are gold for operator pain.
- r/sales, r/Entrepreneur, r/coldemail (skim, don't dwell — high noise).
- GitHub issues for Mautic, Chatwoot, listmonk (open-source operator pain).
- Y Combinator's posts on AI sales tooling (positioning shifts).
- TextBee + SMSGate release notes (gateway capability changes).

When you see something noteworthy, add it to this file with a date.
