"""
agent.py — the Bima Farm claims-detection agent, district-wide (spec §2B).

run_agent_cycle() is called by both the background scheduler and the
manual "Check Now" button (spec §9 — same function, two triggers). It
gives the model a GOAL ("check today's readings across all Dhanusa rivers
against active policies and prepare eligible claims"), not a script, and
lets it plan its own sequence of tool calls (spec §7). If the hackathon
model API is unreachable, it falls back to DEMO AI MODE: a plain,
deterministic, rule-based version of the exact same pipeline (spec §11).

Five tools, matching spec §7:
  get_latest_reading(station, parameter)
  get_active_policies(district)
  check_trigger(policy_id)         -- evaluates ALL of a policy's configured
                                       parameters together (spec §2B)
  check_duplicate(policy_id, hazard_type)  -- cooldown-aware memory (spec §2B)
  draft_claim(policy_id, claim_text, confidence)

Numeric truth (each parameter's value/threshold/met, borderline,
recommended_amount) always comes straight from our own database and
threshold comparisons — never from the model. The model only contributes
the human-readable claim narrative and its own confidence estimate, and
only after passing the strict schema in models.ClaimDraft (spec §12).
"""

from __future__ import annotations

import random
import time
import uuid
import zlib
from datetime import datetime, timezone

from pydantic import ValidationError

import db
import llm
from llm import UsageTracker
from models import ClaimDraft
from stations import COOLDOWN_DAYS, DISTRICT, PARAMETERS, STATION_NAMES, STATIONS

MAX_STEPS = 10
RUN_TIMEOUT_SECONDS = 45
BORDERLINE_BAND = 0.05  # within 5% of any checked parameter's threshold => borderline

# --------------------------------------------------------------------------
# Auto-drift — keeps a hands-off deployment "live" (spec §9 follow-up)
# --------------------------------------------------------------------------
# In production the DHM feed would push a fresh reading on its own; in this
# sandbox, if nobody ever touches Data Entry or the spike button, the last
# seeded numbers would otherwise sit frozen in Supabase forever and every
# 7-hour cycle would just re-check the exact same values. Auto-drift gives
# each station/parameter a small, bounded random-walk nudge once per cycle
# instead — so a deployment backed purely by pre-seeded Supabase data still
# "revises" every cycle, the way a real gauge naturally would.
DRIFT_STEP = {
    "river_level": 0.12,     # metres per cycle
    "rainfall_3day": 8,      # mm per cycle
    "rainfall_30day": 4,     # mm per cycle — a 30-day accumulation moves slower
}
DRIFT_BOUNDS = {
    "river_level": (0.5, 8.0),
    "rainfall_3day": (0, 300),
    "rainfall_30day": (0, 500),
}
# Sources that mean a human (Data Entry) or the demo spike button just set
# this value on purpose. Per the Data Entry panel's own promise ("feeds
# straight into the next check"), such a reading is protected for exactly
# one cycle — the next check evaluates it exactly as entered, unmodified —
# then auto-drift resumes from it as the new baseline on the cycle after
# that, instead of freezing that parameter forever after a single entry.
DRIFT_RESPECTS_SOURCE = {"manual entry", "simulated_spike"}

# In-process only (fine for a single hackathon instance): remembers which
# specific reading id has already been given its one protected cycle, so
# the *next* time we see that same id as "latest" we know its free pass is
# used up and drift should resume from it.
_drift_grace_consumed: dict[tuple[str, str], int] = {}


def _auto_drift_readings(run_id: str) -> int:
    """Nudge every station/parameter whose latest reading isn't a manual/
    spike entry still on its one protected cycle. Returns how many were
    nudged."""
    nudged = 0
    for station in STATION_NAMES:
        for parameter in PARAMETERS:
            latest = db.get_latest_reading(station, parameter)
            key = (station, parameter)
            if latest and latest.get("source") in DRIFT_RESPECTS_SOURCE:
                if _drift_grace_consumed.get(key) != latest["id"]:
                    # First time we've seen this exact reading as "latest" —
                    # protect it for this cycle so the check below evaluates
                    # the operator's own value, untouched.
                    _drift_grace_consumed[key] = latest["id"]
                    continue
                # Same reading again on a later cycle — its one free pass is
                # spent, so drift resumes, using it as the new baseline.
            lo, hi = DRIFT_BOUNDS[parameter]
            base = latest["value"] if latest and latest.get("value") is not None else (lo + hi) / 4
            step = DRIFT_STEP[parameter]
            new_value = round(min(hi, max(lo, base + random.uniform(-step, step))), 2)
            trend = "rising" if new_value > base else ("falling" if new_value < base else "steady")
            unit = "m" if parameter == "river_level" else "mm"
            db.add_reading({
                "station": station,
                "parameter": parameter,
                "value": new_value,
                "unit": unit,
                "trend": trend,
                "source": "auto_drift",
                "raw_text": f"AUTO-DRIFT — {station} {parameter} {base}{unit} -> {new_value}{unit} "
                            f"(no manual input since last cycle; stands in for a live DHM feed)",
            })
            nudged += 1
    if nudged:
        db.log_trace(run_id, "DRIFT", f"auto-drifted {nudged} reading(s) with no manual input since last cycle")
    return nudged

SYSTEM_PROMPT = (
    "You are a claims-detection agent for Dhanusa district. Check today's readings across "
    "ALL Dhanusa rivers and active policies. Each policy may require multiple parameters to "
    "agree (e.g. river level AND rainfall) before it counts as triggered — use check_trigger, "
    "not a single reading, to decide. For each policy that fires and has no existing claim in "
    "its cooldown window, draft a claim with evidence. Be brief and stick to the tools provided."
)

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_latest_reading",
            "description": "Get the most recent reading for one station and one parameter "
                            "(river_level, rainfall_3day, or rainfall_30day).",
            "parameters": {
                "type": "object",
                "properties": {
                    "station": {"type": "string"},
                    "parameter": {"type": "string", "enum": ["river_level", "rainfall_3day", "rainfall_30day"]},
                },
                "required": ["station", "parameter"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_active_policies",
            "description": "List active parametric insurance policies across the district (all stations).",
            "parameters": {
                "type": "object",
                "properties": {"district": {"type": "string"}},
                "required": ["district"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_trigger",
            "description": "Evaluate ALL of a policy's configured parameters together and report "
                            "whether it fired, plus each parameter's value/threshold/met status.",
            "parameters": {
                "type": "object",
                "properties": {"policy_id": {"type": "string"}},
                "required": ["policy_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_duplicate",
            "description": "Check whether a claim already exists for this policy and hazard type "
                            "within the cooldown window.",
            "parameters": {
                "type": "object",
                "properties": {
                    "policy_id": {"type": "string"},
                    "hazard_type": {"type": "string"},
                },
                "required": ["policy_id", "hazard_type"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "draft_claim",
            "description": (
                "Draft a claim for a policy that has fired and is not in cooldown. Provide a short "
                "factual claim_text citing each checked parameter, and a confidence 0-1 estimate — "
                "higher when every parameter clears its threshold comfortably, lower near the line."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "policy_id": {"type": "string"},
                    "claim_text": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": ["policy_id", "claim_text", "confidence"],
            },
        },
    },
]


# Per-policy reading variance — every policy checks its own effective
# number rather than reciting the exact same station figure as every
# other policy on that gauge. This is deliberately layered ON TOP of the
# shared station reading (db.get_latest_reading(station, parameter)),
# not a replacement for it: the dashboard's KPI gauges, Data Entry panel,
# and Quick Demo Spike button all still read/write one number per
# station, and that's correct — it's the whole point of a parametric,
# shared-gauge product (spec §2B, and the "What We Don't Do" boundary
# against per-farm inspection). The offset below represents real
# micro-variation a claims officer would expect anyway — distance from
# the gauge, local drainage, elevation — so two farmers on the same river
# can legitimately see slightly different effective levels without
# needing a second physical sensor per farm. It's deterministic per
# (policy, parameter) so a policy's own number stays stable between
# checks instead of jittering on every request.
_VARIANCE_PCT = {"river_level": 0.03, "rainfall_3day": 0.06, "rainfall_30day": 0.04}


def _policy_reading_offset(policy_id: int, parameter: str) -> float:
    seed = zlib.crc32(f"{policy_id}:{parameter}".encode()) & 0xFFFFFFFF
    rng = random.Random(seed)
    pct = _VARIANCE_PCT.get(parameter, 0.03)
    return rng.uniform(-pct, pct)


def _evaluate_policy(policy: dict) -> dict:
    """Fetch the latest reading for each of the policy's configured
    parameters, apply that policy's own small deterministic variance on
    top of the shared station reading, and check the result against that
    parameter's threshold/operator. Returns {fired, checked_parameters} —
    fired only if EVERY parameter is met (spec §2B: multi-parameter
    triggers, ANDed together)."""
    checked = []
    for trig in policy["triggers"]:
        reading = db.get_latest_reading(policy["station"], trig["parameter"])
        value = reading["value"] if reading else None
        if value is not None:
            offset = _policy_reading_offset(policy["id"], trig["parameter"])
            value = round(value * (1 + offset), 2)
        if value is None:
            met = False
        elif trig["operator"] == ">":
            met = value > trig["threshold"]
        else:
            met = value < trig["threshold"]
        checked.append({
            "name": trig["parameter"],
            "value": value,  # None (not NaN) when no reading exists yet — stays JSON-safe end to end
            "threshold": trig["threshold"],
            "operator": trig["operator"],
            "met": bool(met) if value is not None else False,
            "missing": value is None,
        })
    fired = len(checked) > 0 and all(c["met"] for c in checked)
    return {"fired": fired, "checked_parameters": checked}


def _is_borderline(checked_parameters: list[dict]) -> bool:
    for c in checked_parameters:
        if c.get("missing"):
            continue
        if abs(c["value"] - c["threshold"]) <= BORDERLINE_BAND * max(abs(c["threshold"]), 1e-6):
            return True
    return False


def _rule_based_confidence(checked_parameters: list[dict]) -> float:
    clearances = []
    for c in checked_parameters:
        if c.get("missing"):
            clearances.append(-0.5)
            continue
        if c["operator"] == ">":
            clearances.append((c["value"] - c["threshold"]) / max(abs(c["threshold"]), 1e-6))
        else:
            clearances.append((c["threshold"] - c["value"]) / max(abs(c["threshold"]), 1e-6))
    avg = sum(clearances) / len(clearances) if clearances else 0
    return round(min(0.98, max(0.55, 0.7 + avg * 3)), 2)


def _format_checked(checked_parameters: list[dict]) -> str:
    parts = []
    for c in checked_parameters:
        v = "no data" if c.get("missing") else c["value"]
        mark = "not met" if not c["met"] else "met"
        parts.append(f"{c['name']} {v} {c['operator']} {c['threshold']} ({mark})")
    return "; ".join(parts)


def _rule_based_claim_text(policy: dict, checked_parameters: list[dict]) -> str:
    date_str = datetime.now(timezone.utc).date().isoformat()
    land = policy.get("land_kattha")
    coverage_note = (
        f" Insured land {land:g} kattha -> sum insured NPR {policy['payout_amount']:,}."
        if land else ""
    )
    return (
        f"{policy['station']} station on {date_str}: {_format_checked(checked_parameters)} — "
        f"all conditions met for {policy['hazard_type']} Policy #{policy['id']} "
        f"({policy['farmer_name']}, Ward {policy['ward']}, {policy['municipality']})."
        f"{coverage_note}"
    )


def _save_validated_claim(run_id: str, policy: dict, checked_parameters: list[dict],
                           claim_text: str, confidence: float, source: str) -> dict | None:
    borderline = _is_borderline(checked_parameters)
    draft = {
        "policy_id": str(policy["id"]),
        "hazard_type": policy["hazard_type"],
        "checked_parameters": checked_parameters,
        "station": policy["station"],
        "confidence": confidence,
        "borderline": borderline,
        "claim_text": claim_text,
        "recommended_amount": policy["payout_amount"],
    }
    try:
        validated = ClaimDraft(**draft)
    except ValidationError as e:
        db.log_trace(run_id, "FAIL", f"draft_claim(policy={policy['id']}) failed schema validation: {e}")
        return None

    saved = db.create_claim(
        {
            "policy_id": policy["id"],
            "hazard_type": validated.hazard_type,
            "station": validated.station,
            "farmer_name": policy["farmer_name"],
            "ward": policy["ward"],
            "municipality": policy.get("municipality"),
            "phone": policy.get("phone"),
            "checked_parameters": [p.model_dump() for p in validated.checked_parameters],
            "confidence": validated.confidence,
            "borderline": validated.borderline,
            "claim_text": validated.claim_text,
            "recommended_amount": validated.recommended_amount,
            "source": source,
            "run_id": run_id,
        }
    )
    flag = " · BORDERLINE" if validated.borderline else ""
    db.log_trace(
        run_id, "STEP",
        f"draft_claim(policy={policy['id']}) -> confidence {validated.confidence}{flag} · claim #{saved['id']}",
    )
    return saved


def _check_and_draft(run_id: str, policy: dict, source: str) -> dict | None:
    """Shared core: evaluate -> cooldown check -> draft. Used by both the
    rule-based loop and defensively inside the LLM tool executor, so
    correctness never depends purely on the model remembering the right
    order of calls."""
    result = _evaluate_policy(policy)
    checked = result["checked_parameters"]
    db.log_trace(
        run_id, "STEP",
        f"check_trigger(policy={policy['id']}): {_format_checked(checked)} -> "
        f"{'FIRED' if result['fired'] else 'not fired'}",
    )
    if not result["fired"]:
        return None

    block = db.find_cooldown_block(policy["id"], policy["hazard_type"], COOLDOWN_DAYS)
    if block:
        db.log_trace(
            run_id, "SKIP",
            f"policy={policy['id']} ({policy['hazard_type']}) already covered by existing claim "
            f"#{block['id']} (cooldown {COOLDOWN_DAYS}d)",
        )
        return None

    claim_text = _rule_based_claim_text(policy, checked)
    confidence = _rule_based_confidence(checked)
    return _save_validated_claim(run_id, policy, checked, claim_text, confidence, source=source)


# --------------------------------------------------------------------------
# DEMO AI MODE — deterministic rule-based fallback (spec §11)
# --------------------------------------------------------------------------

def _run_rule_based_loop(run_id: str) -> list:
    new_claims = []
    policies = db.list_policies(district=DISTRICT)
    db.log_trace(run_id, "TOOL", f'get_active_policies("{DISTRICT}") -> {len(policies)} policies')

    for policy in policies:
        saved = _check_and_draft(run_id, policy, source="rule_based")
        if saved:
            new_claims.append(saved)

    db.log_trace(
        run_id, "STEP",
        f"{len(new_claims)} new claim(s) drafted across {len(policies)} district-wide policies",
    )
    return new_claims


# --------------------------------------------------------------------------
# Smart mode — real tool-calling loop against gpt-5.5 (spec §7)
# --------------------------------------------------------------------------

def _execute_tool(run_id: str, name: str, args: dict, usage: UsageTracker, ctx: dict) -> dict:
    if name == "get_latest_reading":
        station, parameter = args["station"], args["parameter"]
        cache_key = ("reading", station, parameter)
        if cache_key in ctx["cache"]:
            return ctx["cache"][cache_key]
        reading = db.get_latest_reading(station, parameter)
        if reading is None:
            result = {"error": f"no {parameter} readings for station {station}"}
        else:
            parsed = None
            if reading.get("raw_text"):
                parsed = llm.parse_reading_text(
                    reading["raw_text"], usage,
                    on_retry=lambda a, b, e: db.log_trace(run_id, "RETRY", f"parse_reading_text after {b:.1f}s backoff ({e})"),
                )
            result = {
                "value": (parsed or {}).get("value", reading["value"]),
                "unit": reading.get("unit"),
                "timestamp": reading.get("timestamp"),
                "trend": (parsed or {}).get("trend", reading.get("trend")),
            }
        db.log_trace(run_id, "TOOL", f'get_latest_reading("{station}", "{parameter}") -> '
                                      f'{result.get("value","?")}{result.get("unit","") or ""}')
        ctx["cache"][cache_key] = result
        return result

    if name == "get_active_policies":
        district = args.get("district", DISTRICT)
        policies = db.list_policies(district=district)
        ctx["policies"] = {str(p["id"]): p for p in policies}
        db.log_trace(run_id, "TOOL", f'get_active_policies("{district}") -> {len(policies)} policies')
        return {"policies": [
            {"policy_id": str(p["id"]), "farmer_name": p["farmer_name"], "ward": p["ward"],
             "station": p["station"], "hazard_type": p["hazard_type"],
             "payout_amount": p["payout_amount"]}
            for p in policies
        ]}

    if name == "check_trigger":
        policy_id = args["policy_id"]
        policy = ctx["policies"].get(policy_id) or db.get_policy(int(policy_id))
        if not policy:
            return {"error": f"unknown policy {policy_id}"}
        result = _evaluate_policy(policy)
        ctx["checked"][policy_id] = result
        db.log_trace(run_id, "STEP", f"check_trigger(policy={policy_id}): "
                                      f"{_format_checked(result['checked_parameters'])} -> "
                                      f"{'FIRED' if result['fired'] else 'not fired'}")
        return {"fired": result["fired"], "checked_parameters": [
            {k: v for k, v in c.items() if k != "missing"} for c in result["checked_parameters"]
        ]}

    if name == "check_duplicate":
        policy_id, hazard_type = args["policy_id"], args["hazard_type"]
        block = db.find_cooldown_block(int(policy_id), hazard_type, COOLDOWN_DAYS)
        db.log_trace(run_id, "TOOL", f'check_duplicate(policy={policy_id}, hazard={hazard_type}) -> '
                                      f'{"blocked by claim #" + str(block["id"]) if block else "clear"}')
        return {"duplicate": block is not None, "blocking_claim_id": block["id"] if block else None}

    if name == "draft_claim":
        policy_id = args["policy_id"]
        policy = ctx["policies"].get(policy_id) or db.get_policy(int(policy_id))
        if not policy:
            return {"error": f"unknown policy {policy_id} — call get_active_policies first"}

        result = ctx["checked"].get(policy_id) or _evaluate_policy(policy)
        if not result["fired"]:
            db.log_trace(run_id, "TOOL", f"draft_claim(policy={policy_id}) rejected — trigger not fired")
            return {"status": "rejected", "reason": "trigger not fired"}

        block = db.find_cooldown_block(policy["id"], policy["hazard_type"], COOLDOWN_DAYS)
        if block:
            db.log_trace(run_id, "SKIP", f"policy={policy_id} ({policy['hazard_type']}) already covered "
                                          f"by existing claim #{block['id']} (cooldown {COOLDOWN_DAYS}d)")
            return {"status": "skipped", "reason": f"already covered by claim #{block['id']}"}

        saved = _save_validated_claim(
            run_id, policy, result["checked_parameters"],
            args.get("claim_text", "").strip() or _rule_based_claim_text(policy, result["checked_parameters"]),
            float(args.get("confidence", 0.7)), source="ai",
        )
        if saved is None:
            # AI output failed schema validation for this one item — fall
            # back to a rule-based draft for just this policy (spec §12).
            saved = _save_validated_claim(
                run_id, policy, result["checked_parameters"],
                _rule_based_claim_text(policy, result["checked_parameters"]),
                _rule_based_confidence(result["checked_parameters"]), source="rule_based_fallback",
            )
        if saved:
            ctx["new_claims"].append(saved)
            return {"status": "drafted", "claim_id": saved["id"], "confidence": saved["confidence"]}
        return {"status": "error", "reason": "could not save claim"}

    return {"error": f"unknown tool {name}"}


def _run_llm_loop(run_id: str) -> tuple[list, UsageTracker]:
    usage = UsageTracker()
    ctx = {"cache": {}, "policies": {}, "checked": {}, "new_claims": []}
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"Run today's district-wide check for {DISTRICT}."},
    ]
    start = time.time()

    for step in range(1, MAX_STEPS + 1):
        if time.time() - start > RUN_TIMEOUT_SECONDS:
            db.log_trace(run_id, "ESCALATE", f"wall-clock timeout ({RUN_TIMEOUT_SECONDS}s) — handing to Prakash")
            break

        db.log_trace(run_id, "STEP", f"{step} plan: readings (all stations) -> policies -> check_trigger -> draft")
        msg = llm.chat(
            llm.GPT_MODEL, messages, usage, tools=TOOLS, tool_choice="auto",
            on_retry=lambda a, b, e: db.log_trace(run_id, "RETRY", f"gpt-5.5 call after {b:.1f}s backoff ({e})"),
        )
        messages.append({"role": "assistant", "content": msg.content,
                          "tool_calls": [tc.model_dump() for tc in (msg.tool_calls or [])] or None})

        if not msg.tool_calls:
            db.log_trace(run_id, "STEP", f"model finished: {(msg.content or '').strip()[:200]}")
            break

        for call in msg.tool_calls:
            import json as _json
            try:
                args = _json.loads(call.function.arguments or "{}")
            except _json.JSONDecodeError:
                args = {}
            result = _execute_tool(run_id, call.function.name, args, usage, ctx)
            messages.append({
                "role": "tool", "tool_call_id": call.id,
                "content": _json.dumps(result, default=str),
            })
    else:
        db.log_trace(run_id, "ESCALATE", f"MAX_STEPS ({MAX_STEPS}) reached — handing to Prakash")

    return ctx["new_claims"], usage


# --------------------------------------------------------------------------
# Public entrypoint — called by both the scheduler and "Check Now"
# --------------------------------------------------------------------------

def run_agent_cycle(trigger_source: str = "scheduled") -> dict:
    run_id = uuid.uuid4().hex[:6]
    start = time.time()
    db.log_trace(run_id, "TRIGGER", f"{trigger_source} · district={DISTRICT} · run={run_id}")
    _auto_drift_readings(run_id)

    demo_mode = False
    cost_line = "n/a"
    try:
        if not llm.HACKATHON_KEY:
            raise llm.LLMUnavailable("no HACKATHON_KEY configured")
        new_claims, usage = _run_llm_loop(run_id)
        cost_line = usage.summary()
    except llm.LLMUnavailable as e:
        demo_mode = True
        db.log_trace(run_id, "FALLBACK", f"AI unavailable ({e}) — switching to rule-based threshold check")
        new_claims = _run_rule_based_loop(run_id)
        cost_line = "rule-based fallback — no model cost"

    elapsed = time.time() - start
    db.log_trace(
        run_id, "DONE",
        f"{len(new_claims)} new claim(s) drafted · {len(new_claims)} gate(s) awaiting approval · "
        f"{cost_line} · {elapsed:.1f}s",
    )
    return {
        "run_id": run_id,
        "trigger_source": trigger_source,
        "demo_mode": demo_mode,
        "new_claims": new_claims,
        "elapsed_s": round(elapsed, 1),
    }


def nearest_flood_triggers(station: str) -> dict:
    """The lowest flood-direction threshold (operator '>') configured for
    any active policy at this station, per parameter — i.e. how close the
    *nearest* policy on this station is to firing. Powers the KPI gauge
    bars on the dashboard so the officer sees not just a number but how
    close it sits to the closest trigger line."""
    policies = db.list_policies(station=station)
    nearest: dict[str, float] = {}
    for policy in policies:
        for trig in policy["triggers"]:
            if trig["operator"] != ">":
                continue
            current = nearest.get(trig["parameter"])
            if current is None or trig["threshold"] < current:
                nearest[trig["parameter"]] = trig["threshold"]
    return nearest


def compute_kpis() -> dict:
    """District-wide 'policies at risk': active policies not currently
    blocked by a cooldown, where at least one required parameter is
    within 10% of its threshold (already met, or closing in on it)."""
    policies = db.list_policies(district=DISTRICT)
    at_risk = 0
    for policy in policies:
        if db.find_cooldown_block(policy["id"], policy["hazard_type"], COOLDOWN_DAYS):
            continue
        result = _evaluate_policy(policy)
        close = any(
            (not c["met"] and not c.get("missing") and
             abs(c["value"] - c["threshold"]) <= 0.10 * max(abs(c["threshold"]), 1e-6))
            or c["met"]
            for c in result["checked_parameters"]
        )
        if close:
            at_risk += 1
    return {"policies_at_risk": at_risk, "policies_total": len(policies)}
