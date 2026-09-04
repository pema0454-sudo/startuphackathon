"""
main.py — Bima Farm FastAPI app.

Run:  uvicorn main:app --reload   (from inside backend/, with .env loaded)

Serves the API (spec §13) plus the static frontend (index.html,
command.html, audit.html, /static/*). Starts a background scheduler on
launch so the agent "starts by itself" (spec §9) — POST /api/agent/run
("Check Now") calls the exact same function for demo pacing.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

from dotenv import load_dotenv

load_dotenv()

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import agent
import db
import llm
import tts
from models import ApproveRequest, ManualReadingRequest, RejectRequest, SpikeRequest
from stations import DISTRICT, STATION_NAMES, STATIONS

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
FRONTEND_DIR = os.path.join(BACKEND_DIR, "..", "frontend")

AGENT_INTERVAL_SECONDS = int(os.getenv("AGENT_INTERVAL_SECONDS", "25200"))  # 7 hours — matches a
# realistic DHM-style gauge refresh cadence rather than a demo-fast tick. The very first run still
# fires almost immediately (AGENT_FIRST_RUN_DELAY_SECONDS below), so a scheduler-triggered run is
# still captured quickly for the trace/demo; "Check Now" covers on-demand pacing during a live demo.
AGENT_FIRST_RUN_DELAY_SECONDS = int(os.getenv("AGENT_FIRST_RUN_DELAY_SECONDS", "15"))

scheduler = BackgroundScheduler(timezone="UTC")


def _scheduled_tick():
    agent.run_agent_cycle(trigger_source="scheduled")


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not db.is_seeded():
        import seed
        seed.seed_all()
    scheduler.add_job(
        _scheduled_tick,
        "interval",
        seconds=AGENT_INTERVAL_SECONDS,
        id="agent_cycle",
        next_run_time=datetime.utcnow() + timedelta(seconds=AGENT_FIRST_RUN_DELAY_SECONDS),
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    if AGENT_INTERVAL_SECONDS % 3600 == 0:
        interval_label = f"{AGENT_INTERVAL_SECONDS // 3600}h"
    elif AGENT_INTERVAL_SECONDS % 60 == 0:
        interval_label = f"{AGENT_INTERVAL_SECONDS // 60}m"
    else:
        interval_label = f"{AGENT_INTERVAL_SECONDS}s"
    db.log_trace("startup", "BOOT", f"scheduler started · every {interval_label} · "
                                     f"mode={'live-AI' if llm.HACKATHON_KEY else 'rule-based (no key)'}")
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="Bima Farm", lifespan=lifespan)


# --------------------------------------------------------------------------
# API — spec §13
# --------------------------------------------------------------------------

def _mask_phone(phone: str | None) -> str | None:
    if not phone:
        return phone
    digits = "".join(c for c in phone if c.isdigit())
    if len(digits) < 4:
        return phone
    return phone[:-6] + "••••••" + digits[-2:]


@app.get("/api/status")
def api_status():
    meta = db.get_sync_meta()
    primary = next((s["name"] for s in STATIONS if s["primary"]), STATIONS[0]["name"])

    readings: dict[str, dict[str, dict | None]] = {}
    for s in STATIONS:
        readings[s["name"]] = {
            param: db.get_latest_reading(s["name"], param)
            for param in ("river_level", "rainfall_3day", "rainfall_30day")
        }

    kpis = agent.compute_kpis()
    nearest = agent.nearest_flood_triggers(primary)

    stale_minutes = None
    if meta["last_sync"]:
        try:
            delta = datetime.utcnow() - datetime.fromisoformat(meta["last_sync"]).replace(tzinfo=None)
            stale_minutes = round(delta.total_seconds() / 60, 1)
        except ValueError:
            pass

    return {
        "district": DISTRICT,
        "primary_station": primary,
        "river_level": readings.get(primary, {}).get("river_level"),
        "rainfall_3day": readings.get(primary, {}).get("rainfall_3day"),
        "readings": readings,
        "nearest_trigger": nearest,  # e.g. {"river_level": 4.3, "rainfall_3day": 90} — closest policy on this station
        "policies_at_risk": kpis["policies_at_risk"],
        "policies_total": kpis["policies_total"],
        "demo_ai_mode": not bool(llm.HACKATHON_KEY),
        "sync": {**meta, "stale_minutes": stale_minutes},
    }


@app.get("/api/stations")
def api_stations():
    return {"district": DISTRICT, "stations": STATIONS}


@app.get("/api/policies")
def api_policies(station: str | None = None, active_only: bool = False):
    return db.list_policies(station=station, district=DISTRICT, active_only=active_only)


@app.get("/api/claims")
def api_claims(status: str | None = None):
    return db.list_claims(status=status)


@app.post("/api/claims/{claim_id}/approve")
def api_approve_claim(claim_id: int, body: ApproveRequest):
    claim = db.get_claim(claim_id)
    if not claim:
        raise HTTPException(404, "claim not found")
    if claim["status"] != "Pending Review":
        raise HTTPException(409, f"claim is '{claim['status']}', not 'Pending Review' — cannot approve")

    run_id = claim.get("run_id") or f"claim-{claim_id}"
    db.update_claim(claim_id, {"status": "Approved"})
    db.log_trace(run_id, "APPROVED", f"claim #{claim_id} approved by {body.officer_name}")

    voice = tts.generate_voice(claim_id, claim["farmer_name"], claim["recommended_amount"])
    db.update_claim(claim_id, {
        "status": "Confirmed",
        "sms_text": voice["sms_text"],
        "voice_file": voice["voice_file"],
        "dispatch_status": "Sandbox Dispatch",
        "voice_engine": voice["engine"],
    })
    engine_note = "voice + SMS text generated" if voice["engine"] == "azure-tts" else "SMS text only (TTS unavailable)"
    db.log_trace(
        run_id, "ACTION",
        f"voice_alert -> {claim['farmer_name']} ({_mask_phone(claim.get('phone'))}) · ne-NP · "
        f"{engine_note} · Sandbox Dispatch (no real telecom integration in this environment)",
    )

    db.add_audit({
        "claim_id": claim_id,
        "action": "approve",
        "officer_name": body.officer_name,
        "ai_recommendation": {
            "confidence": claim["confidence"],
            "recommended_amount": claim["recommended_amount"],
            "claim_text": claim["claim_text"],
            "source": claim.get("source"),
        },
    })
    return db.get_claim(claim_id)


@app.post("/api/claims/{claim_id}/reject")
def api_reject_claim(claim_id: int, body: RejectRequest):
    claim = db.get_claim(claim_id)
    if not claim:
        raise HTTPException(404, "claim not found")
    if claim["status"] != "Pending Review":
        raise HTTPException(409, f"claim is '{claim['status']}', not 'Pending Review' — cannot reject")

    run_id = claim.get("run_id") or f"claim-{claim_id}"
    db.update_claim(claim_id, {"status": "Rejected"})
    db.log_trace(run_id, "REJECTED", f"claim #{claim_id} rejected by {body.officer_name}: {body.reason}")

    db.add_audit({
        "claim_id": claim_id,
        "action": "reject",
        "officer_name": body.officer_name,
        "reason": body.reason,
        "ai_recommendation": {
            "confidence": claim["confidence"],
            "recommended_amount": claim["recommended_amount"],
            "claim_text": claim["claim_text"],
            "source": claim.get("source"),
        },
    })
    return db.get_claim(claim_id)


@app.post("/api/agent/run")
def api_agent_run():
    return agent.run_agent_cycle(trigger_source="manual")


@app.get("/api/trace")
def api_trace(limit: int = 200):
    return db.list_trace(limit=limit)


@app.get("/api/audit")
def api_audit():
    return db.list_audit()


@app.post("/api/simulate/spike")
def api_simulate_spike(body: SpikeRequest):
    if body.station not in STATION_NAMES:
        raise HTTPException(400, f"unknown station '{body.station}' — must be one of {STATION_NAMES}")
    last = db.get_latest_reading(body.station, body.parameter)
    if body.value is not None:
        new_value = body.value
    else:
        base = last["value"] if last else (3.5 if body.parameter == "river_level" else 60)
        delta = body.delta if body.delta is not None else (0.8 if body.parameter == "river_level" else 40)
        new_value = round(base + delta, 2)
    unit = "m" if body.parameter == "river_level" else "mm"
    row = db.add_reading({
        "station": body.station,
        "parameter": body.parameter,
        "value": new_value,
        "unit": unit,
        "trend": body.trend,
        "source": "simulated_spike",
        "raw_text": f"SANDBOX SPIKE — {body.station} {body.parameter} manually bumped to {new_value}{unit} for demo pacing",
    })
    db.log_trace("demo", "SIMULATE", f"spike injected: {body.station} {body.parameter} -> {new_value}{unit}")
    return row


@app.post("/api/readings/manual")
def api_manual_reading(body: ManualReadingRequest):
    if body.station not in STATION_NAMES:
        raise HTTPException(400, f"unknown station '{body.station}' — must be one of {STATION_NAMES}")
    unit = "m" if body.parameter == "river_level" else "mm"
    row = db.add_reading({
        "station": body.station,
        "parameter": body.parameter,
        "value": body.value,
        "unit": unit,
        "trend": None,
        "source": "manual entry",
        "raw_text": f"Manual entry — {body.station} {body.parameter} set to {body.value}{unit}"
                    + (f" ({body.note})" if body.note else ""),
    })
    db.log_trace("demo", "MANUAL", f"operator entered {body.parameter}={body.value}{unit} for "
                                    f"{body.station} · source: manual entry")
    return row


# --------------------------------------------------------------------------
# Static frontend — mounted last so it never shadows /api/* routes
# --------------------------------------------------------------------------

@app.get("/command.html")
def serve_command():
    return FileResponse(os.path.join(FRONTEND_DIR, "command.html"))


@app.get("/audit.html")
def serve_audit():
    return FileResponse(os.path.join(FRONTEND_DIR, "audit.html"))


app.mount("/static", StaticFiles(directory=os.path.join(FRONTEND_DIR, "static")), name="static")
app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
