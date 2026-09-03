# Bema Farm — Pitch Deck Outline (max 8 slides)

Starting content for the team to drop into slides. Swap in screenshots from a live run of the officer
dashboard and a captured `trace.log` excerpt before presenting — a live demo beats static slides every
time.

**1. The One Sentence**
Bema Farm helps the claims officer at an insurer/cooperative find and file eligible flood- and
drought-insurance payouts automatically, without the farmer needing to ask.
*Named users: Sita Tamang (policyholder) · Prakash Rai (claims officer).*

**2. The Problem**
Weather-index crop insurance already exists in Nepal — it's supposed to pay out automatically when
official rain/river data crosses an agreed line. In practice, nobody manually checks forty policies
against a spreadsheet fast enough. The subsidy exists. The data exists. Nobody claims it.

**3. The Bad Day**
2 a.m., monsoon. Kamala up 1.2m in six hours across Dhanusa district. 2G only. Power out four hours. Forty
policyholders across several villages cross their trigger the same night. One officer, one spreadsheet,
no chance.

**4. The Solution**
WATCH river/rainfall data across all of Dhanusa → CHECK every active policy's full set of parameters
together → DRAFT the claim with evidence → HUMAN approves → FARMER gets a Nepali voice call.
*(screenshot: officer dashboard claim card, parameter checklist visible)*

**5. Architecture & Agentic Signals**
Goal not script · real tools (5, hand-rolled loop, no framework) · plans multi-step · remembers (14-day
cooldown, not just a bare duplicate check) · starts itself (scheduler) · real consequence (DB write + real
TTS audio + audit log). *(screenshot: trace panel showing a scheduled TRIGGER line and a cooldown SKIP)*

**6. The Human Checkpoint**
Never skipped, enforced server-side — not a UI convention. Approve/Reject are the only two ways a claim's
status can change.

**7. What's Real vs. Mocked**
Real: agent loop, database, approval gate, Nepali TTS audio. Mocked & labelled with a small "Sandbox"
badge: synthetic river/rainfall data, message *delivery* ("Sandbox Dispatch" — no telecom gateway built,
simulated per the brief's own guidance). Known limitation: basis risk, mitigated with multi-parameter
triggers and the Borderline Confidence Flag.

**8. Where This Goes**
Licensed to an insurer/cooperative's claims team — not a farmer-facing app. Farmers keep getting a simple
phone call; everything upstream of that call is what we built in 24 hours.
