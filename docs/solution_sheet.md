# Bema Farm — Solution Sheet
### *The Claim That Files Itself*

**One sentence:** Bema Farm helps the claims officer at an insurer/cooperative find and file eligible
flood- and drought-insurance payouts automatically, without the farmer needing to ask.

**Named user (who Bema Farm acts for):** Sita Tamang — rice farmer, Ward 6, Janakpurdham Sub-Metropolitan
City, Dhanusa district. Holds a NPR 500/season weather-index flood policy paying NPR 5,000 if the Kamala
river gauge crosses 4.5m **and** 3-day cumulative rainfall exceeds 100mm.

**Named user (the human checkpoint):** Prakash Rai — Claims Officer. The agent may detect a trigger and
draft a claim on its own; it may never mark a claim "Approved" without Prakash clicking Approve. This is
enforced in the backend endpoint itself, not just hidden in the UI.

---

## The Bad Day

It is 2 a.m. during monsoon. The Kamala has risen 1.2 metres in six hours across Dhanusa district. Mobile
networks are degraded to 2G. Power has been out for four hours. Forty policyholders across several Dhanusa
villages cross their trigger on the same night. Prakash cannot manually check forty policy files against a
spreadsheet before people stop trusting that a payout is coming.

---

## What the agent does, unasked

A background scheduler wakes the agent on an interval (no human starts it). It reads every active policy
across all of Dhanusa district's four gauges (Kamala, Aurahi, Jhim/Jhanjh Khola, Ratu), and for each one
checks whether *all* of its required parameters — e.g. river level AND 3-day rainfall for a flood policy —
clear their thresholds together, not just one noisy reading. It checks whether a claim already exists for
that policy's hazard type within a 14-day cooldown (never files the same event twice), and — for every
policy that clears cooldown — drafts a claim with the evidence attached. All of this happens *before* Sita
or Prakash has done anything. The claim then waits, `Pending Review`, for Prakash.

## Architecture

```
Scheduler (starts itself)
        │
        ▼
  Agent loop (gpt-5.5 plans; DeepSeek-V4-Flash parses raw feed text)
        │  tools: get_latest_reading · get_active_policies ·
        │         check_trigger · check_duplicate · draft_claim
        ▼
  Claim saved — status: Pending Review
        │
        ▼
  Prakash Rai reviews on the officer dashboard  ── Reject ──► audit log entry
        │
     Approve (human checkpoint — enforced server-side)
        │
        ▼
  Azure TTS: real Nepali voice file + SMS text  ──► "Sandbox Dispatch"
        │                                            (not a real phone call)
        ▼
  Claim status: Confirmed · audit log entry written
```

DEMO AI MODE: if the model API is unreachable, the identical pipeline runs on a plain rule-based
multi-parameter threshold check instead — the loop, the human gate, and the trace all still work.

## Tools & models

- **DeepSeek-V4-Flash** — cheap/fast parsing of the raw station feed text into a structured reading.
- **gpt-5.5** — the one call per run that plans the multi-step, district-wide check and drafts the
  human-readable claim narrative. Numeric truth (each parameter's value/threshold/met status, the payout,
  the borderline flag) always comes from our own database and threshold comparisons — never from the model.
- **Azure TTS (`ne-NP`)** — generates a genuine Nepali audio confirmation on approval.
- Hand-rolled agent loop (no LangGraph/CrewAI) — 5 tools, `MAX_STEPS = 10`, retries with backoff+jitter.

## Human checkpoint

`POST /api/claims/{id}/approve` and `/reject` are the *only* two ways a claim's status can change. There
is no generic "update claim" endpoint. A request that tries to set `status=Approved` directly is ignored —
the field isn't part of the request schema at all.

## What's real vs. mocked

**Real:** the agent's tool-calling loop and DB writes, the claim/audit database, the approval gate, the
Nepali TTS audio itself. **Mocked, and labelled as such (a small "Sandbox" badge, not a warning banner):**
the river/rainfall data (synthetic, Dhanusa-shaped), and message *delivery* — no real SMS gateway or phone
call was built; "simulate the channel and say plainly that it is simulated" per the brief's own guidance.

## Known limitation: basis risk

A known property of *all* parametric insurance, not a flaw specific to Bema Farm: the trigger occasionally
fires without matching real damage on one specific plot, or real damage happens without quite crossing the
line. Mitigated two ways: **multi-parameter triggers** (a single noisy reading can't fire a claim alone —
river level and rainfall must agree) and a **Borderline Confidence Flag** on any claim where a checked
parameter is within 5% of its threshold, rather than pretending the gap doesn't exist.
