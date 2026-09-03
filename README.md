# Bema Farm — *The Claim That Files Itself*

Flood and drought insurance that pays a farmer before she asks — a shared river gauge, not an inspector,
decides.

Built in 24 hours for **Startup Innovation Hackathon Vol. III — Agentic AI in Nepal's Digital Age
Transformation** (03–04 Sept 2026, Embark College). (This is a development note for the team, not
something the product itself mentions — see [Product framing](#product-framing) below.)

> **One sentence:** Bema Farm helps the claims officer at an insurer/cooperative find and file eligible
> flood- and drought-insurance payouts automatically, without the farmer needing to ask.

Named users: **Sita Tamang** (rice farmer, Ward 6, Janakpurdham Sub-Metropolitan City, Dhanusa district —
holds a NPR 500/season weather-index flood policy paying NPR 5,000 if the Kamala river gauge crosses 4.5m
**and** 3-day cumulative rainfall exceeds 100mm) and **Prakash Rai** (claims officer, the one human
checkpoint — nothing gets marked "Approved" without him clicking the button).

---

## Product framing

The public site (`index.html`) and dashboard are written to read like a real, live product an insurer's
team would log into — no event name, dates, or "built for a hackathon" language anywhere in the UI. The
one honest disclosure that's always kept, deliberately, is a small **"Sandbox"** badge in the nav bar (the
same pattern real fintech products use for a test/illustrative environment) plus per-claim **"Sandbox
Dispatch"** tags — see [What's real vs. mocked](#whats-real-vs-mocked). This repo, its README, and this
codebase are of course allowed to talk about the hackathon; the *product surface* just doesn't.

---

## What this is (and isn't)

Bema Farm watches river/rainfall data across **all of Dhanusa district** (four gauges: Kamala, Aurahi,
Jhim/Jhanjh Khola, Ratu), checks it against a set of parametric ("weather-index") flood and drought
policies, drafts claims for the ones whose full set of conditions is met, and waits for a human claims
officer to approve before anything is marked as paid. One district, one agent, one policy type — still a
narrow build, just with a fuller, more credible dataset than a single village.

It does **not** underwrite policies, move or hold money, replace the claims officer, manage shelters or
rescue teams, or send a real SMS/phone call. See [Known limitations](#known-limitations) below.

---

## Quick start

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate        # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt

copy ..\.env.example .env     # then fill in whatever keys you have — all of them are optional
uvicorn main:app --reload
```

Open **http://127.0.0.1:8000** — that's the public site. Click **Enter Platform** for the claims officer
dashboard (`/command.html`), or go straight to `/audit.html` for the audit log.

On first launch the app seeds itself with ~35 synthetic policies spread across Dhanusa's four gauges and
a representative spread of its municipalities, plus a short reading history — no manual seeding step
required. To reseed by hand: `python seed.py` (add `--force` to wipe and reseed the local store).

**No keys required to run it.** With `.env` empty, Bema Farm runs entirely in local-JSON-store +
DEMO AI MODE (rule-based threshold checks instead of model calls) + text-only voice fallback — the whole
loop, including the human-approval gate, works end to end with zero external dependencies. Fill in the
keys below to switch on the real thing piece by piece.

### Environment variables (`.env`, see `.env.example`)

| Variable | What it does if set | What happens if blank |
|---|---|---|
| `SUPABASE_URL` / `SUPABASE_KEY` | Reads/writes go to your Supabase Postgres project (run `backend/schema.sql` in its SQL editor first) | Falls back to a local JSON file (`backend/data/local_store.json`) — the app still works, just not shared across machines |
| `HACKATHON_KEY` (+ `HACKATHON_BASE_URL`) | The agent plans multi-step checks and drafts claim narratives with `gpt-5.5`, and parses raw station feed text with `DeepSeek-V4-Flash` | Every run uses **DEMO AI MODE**: a plain Python threshold check (all of a policy's parameters must clear their thresholds) drafts the same claims, just without the model |
| `AZURE_TTS_KEY` (+ `AZURE_TTS_REGION`, `AZURE_TTS_VOICE`) | Claim approval generates a real Nepali (`ne-NP`) voice confirmation MP3 | Approval still works — an SMS-length text confirmation is generated and saved, just no audio file (labelled "TTS unavailable" in the trace) |
| `AGENT_INTERVAL_SECONDS` | How often the background scheduler checks readings against policies (default 180s) | — |

---

## Architecture

```
frontend/ (plain HTML/CSS/JS, no build step)
  index.html      public site — what Bema Farm is
  command.html    officer dashboard — KPIs, AI brief, claims queue, trace panel, data entry
  audit.html      audit log
  static/app.js   fetch() calls + DOM rendering
  static/style.css

backend/ (FastAPI)
  main.py       API routes, scheduler startup, static file serving
  agent.py      the agent loop — 5 tools, MAX_STEPS=10, DEMO AI MODE fallback
  llm.py        hackathon model API wrapper — retries, backoff+jitter, token/cost tracking
  tts.py        Azure TTS + SMS text generation, "Sandbox Dispatch" labelling
  db.py         Supabase client with local-JSON-cache fallback ("degrade, don't die")
  models.py     Pydantic request models + the strict ClaimDraft schema
  stations.py   single source of truth for Dhanusa's 4 gauges + municipality spread
  seed.py       synthetic demo data (all of Dhanusa district, mixed flood/drought policies)
  schema.sql    Supabase table definitions
```

**Pipeline:** scheduler or "Check Now" triggers a district-wide run → agent calls `get_active_policies` →
for each policy, `check_trigger` (evaluates every one of that policy's required parameters together) →
`check_duplicate` (cooldown-aware memory) → `draft_claim` (planned by the model, or the rule-based
fallback) → claim saved as `Pending Review` → officer clicks Approve → Azure TTS generates a Nepali voice
line + SMS text → claim becomes `Confirmed` → audit log entry written. An operator can also stand in for
the real DHM feed via the Data Entry panel (`POST /api/readings/manual`), and every such reading is
tagged `source: manual entry` so it's distinguishable from the seeded/scheduled feed.

**Multi-parameter triggers (spec §2B):** a single reading can be a sensor glitch or a one-off spike, so
each policy checks 2–3 signals together instead of one number — flood policies require `river_level` AND
`rainfall_3day` to both clear their thresholds; drought policies require `rainfall_30day` AND `river_level`
to both fall *below* theirs. `check_trigger` logs every parameter it checked and its individual value, not
just the final yes/no, so the trace shows the cross-checking happening.

**Memory / cooldown (spec §2B):** once a claim exists for a policy's hazard type, that policy is in a
14-day cooldown and cannot generate a second claim for the same hazard until it expires. A trigger crossing
during cooldown is logged as `SKIP … already covered by existing claim #X` — not silently ignored — so the
memory signal is visibly doing more than a bare duplicate check.

**Tools used**: DeepSeek-V4-Flash (cheap/fast — parses the raw station feed text), gpt-5.5 (the one
planning + claim-drafting call per run), Azure TTS (Nepali voice confirmation). Model routing lives in
`llm.py`; see `agent.py` for how the plan/tool-call loop is built by hand (no LangGraph/CrewAI) so every
line of the agent's behaviour is something we can explain in Q&A.

**Human checkpoint**: enforced in the backend, not just the UI. `POST /api/claims/{id}/approve` and
`/reject` are the *only* two ways a claim's status can change — there is no generic "update claim" endpoint,
and both require the claim to currently be `Pending Review`. A client cannot set `status=Approved` directly;
sending that field in the request body is simply ignored (see `models.ApproveRequest`).

**Trace**: every agent run writes to a `trace_log` table *and* `backend/trace.log` (belt-and-braces —
if Supabase is briefly unreachable mid-demo, the flat file is still there). Nothing is sanitized out —
retries, fallbacks, cooldown skips and step-limit escalations are logged exactly as they happened. View it
live in the Agent Trace panel on `/command.html`.

---

## API endpoints

```
GET  /api/status               current readings (all 4 stations × 3 parameters) + KPI numbers
GET  /api/stations              Dhanusa's 4 gauges
GET  /api/policies               list of policies (optional ?station=)
GET  /api/claims                 list of claims (optional ?status=)
POST /api/claims/{id}/approve    human checkpoint — triggers TTS + audit
POST /api/claims/{id}/reject     human checkpoint — writes audit entry
POST /api/agent/run               manual "Check Now" — same function the scheduler calls
GET  /api/trace                   latest trace lines
GET  /api/audit                   audit log entries
POST /api/simulate/spike          demo helper: bump one station/parameter upward
POST /api/readings/manual         operator stands in for the real DHM feed
```

---

## Agentic signals (spec checklist)

- **Goal, not script** — the model gets "check today's readings across all Dhanusa rivers against active
  policies and prepare eligible claims," and chooses its own sequence of tool calls (`agent.py:
  SYSTEM_PROMPT`, `_run_llm_loop`).
- **Real tools** — five tools backed by the real database, not stubs (`agent.py: TOOLS`).
- **Plans multi-step, retries on failure** — policies → per-policy trigger check → duplicate/cooldown
  check → draft, with exponential-backoff-with-jitter retries around every model call
  (`llm.py: _with_retries`).
- **Remembers** — `check_duplicate(policy_id, hazard_type)` is checked (defensively, server-side, even if
  the model forgets to call it) against a 14-day cooldown before any claim is saved, and a blocked attempt
  is logged with which claim already covers it — not silently dropped.
- **Starts itself** — a background scheduler (`main.py`, APScheduler) fires `run_agent_cycle()`
  automatically on an interval; "Check Now" calls the identical function for demo pacing.
- **Real consequence** — approval flips the claim's status in the database, generates a real Nepali voice
  file, and writes an audit entry — all inside one backend request handler, never faked.

---

## Fair-use guardrails

`MAX_STEPS = 10` hard cap per run · wall-clock timeout (45s) per run · exponential backoff with jitter on
every model call · identical `(model, messages)` pairs are served from an in-run cache instead of
re-sent · the scheduler interval is configurable via `AGENT_INTERVAL_SECONDS` specifically so it can be
slowed or paused before leaving it running unattended overnight.

---

## What's real vs. mocked

| | Real | Mocked / simulated |
|---|---|---|
| River/rainfall data | — | Synthetic, seeded to resemble Dhanusa geography and DHM's public format; a small "Sandbox" badge discloses this everywhere, without a big banner |
| Agent reasoning | Real tool-calling loop against the hackathon's `gpt-5.5` + `DeepSeek-V4-Flash` endpoint, when a key is configured | DEMO AI MODE: deterministic rule-based multi-parameter threshold check, when no key / API unreachable |
| Claim storage, audit log, human approval gate | Real — a genuine database write path, enforced server-side | — |
| Nepali voice confirmation | Real audio generated by Azure TTS, when a key is configured | Text-only fallback when no key |
| SMS/phone delivery | — | **Always simulated ("Sandbox Dispatch").** No telecom integration was built (see spec Q&A: "simulate the channel and say plainly that it is simulated"). Real delivery would need a gateway like Sparrow SMS (Nepal) or Twilio — scoped out to spend the 24 hours on the agent's decision-making instead. |
| Payout / money movement | — | Never touched by Bema Farm at all — the insurer's own systems handle actual payout once a claim is approved |

---

## Known limitations

**Basis risk** — a known property of *all* parametric insurance, not a flaw specific to Bema Farm: a
gauge crossing its trigger occasionally won't match real damage on one farmer's specific plot, and real
damage can happen without quite crossing the line. Bema Farm mitigates this two ways: **multi-parameter
triggers** (river level AND rainfall must both agree before a flood claim fires — a single noisy reading
can't fire one alone) and a **Borderline Confidence Flag** on any claim where a checked parameter is within
5% of its threshold ("Borderline — recommend manual double-check"), rather than pretending the gap doesn't
exist.

**No underwriting or fraud detection** — explicitly out of scope. Bema Farm works only with policies that
already exist.

**One district** — by design (see "Scope discipline" in the build spec). All of Dhanusa, but only
Dhanusa — this is a narrow proof of one real workflow, not a platform.

**Simulated delivery channel** — see the table above. The claims officer sees a real Nepali audio file
generate and play; nothing is dialled or texted to an actual phone number.

---

## Repo layout

```
.
├── backend/            FastAPI app, agent loop, seed data, schema.sql
├── frontend/            static HTML/CSS/JS — no framework, no build step
├── docs/solution_sheet.md
├── .env.example
└── README.md            you are here
```
