"""
db.py — Bema Farm data layer.

Primary store: Supabase (Postgres), used when SUPABASE_URL / SUPABASE_KEY
are set in the environment. Every Supabase call is wrapped in try/except;
on failure (or when Supabase isn't configured at all) we fall back to a
local JSON-file-backed store and mark the in-memory cache as "stale" so the
API can tell the frontend how old the data is ("degrade, don't die").

This module is the ONLY place that touches storage. Everything else
(agent.py, main.py) calls the functions below and never talks to Supabase
or the filesystem directly.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

# --------------------------------------------------------------------------
# Config / mode detection
# --------------------------------------------------------------------------

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "").strip()
SUPABASE_CONFIGURED = bool(SUPABASE_URL and SUPABASE_KEY)

BACKEND_DIR = Path(__file__).parent
DATA_DIR = BACKEND_DIR / "data"
LOCAL_STORE_PATH = DATA_DIR / "local_store.json"
TRACE_LOG_PATH = BACKEND_DIR / "trace.log"

DATA_DIR.mkdir(parents=True, exist_ok=True)

_lock = threading.RLock()
_supabase_client = None

# meta.mode: "supabase" once we have successfully talked to Supabase at least
# once, otherwise "local". meta.stale: True whenever the last attempted
# Supabase call failed and we served/kept the cached copy instead.
_meta = {
    "mode": "supabase" if SUPABASE_CONFIGURED else "local",
    "stale": False,
    "last_sync": None,  # ISO timestamp of last successful Supabase round-trip
    "last_error": None,
}

TABLES = ("policies", "readings", "claims", "audit_log", "trace_log")


# --------------------------------------------------------------------------
# Local JSON store (default backend, and the Supabase fallback cache)
# --------------------------------------------------------------------------

def _empty_store() -> dict:
    return {t: [] for t in TABLES} | {"_counters": {t: 0 for t in TABLES}}


def _load_local() -> dict:
    if LOCAL_STORE_PATH.exists():
        try:
            with open(LOCAL_STORE_PATH, "r", encoding="utf-8") as f:
                store = json.load(f)
            for t in TABLES:
                store.setdefault(t, [])
            store.setdefault("_counters", {t: 0 for t in TABLES})
            return store
        except (json.JSONDecodeError, OSError):
            pass
    return _empty_store()


_store = _load_local()


def _save_local() -> None:
    # Windows occasionally throws a transient WinError 5 ("Access is
    # denied") on os.replace() right after a file is closed — typically an
    # AV/indexer holding a brief handle on it. A short retry clears it
    # without ever risking a half-written store (the write itself is to a
    # throwaway .tmp file; only the atomic rename is retried).
    tmp = LOCAL_STORE_PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_store, f, indent=2, default=str)
    for attempt in range(5):
        try:
            tmp.replace(LOCAL_STORE_PATH)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.05 * (attempt + 1))


def _next_id(table: str) -> int:
    _store["_counters"][table] = _store["_counters"].get(table, 0) + 1
    return _store["_counters"][table]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# Supabase client (lazy)
# --------------------------------------------------------------------------

def _sb():
    global _supabase_client
    if not SUPABASE_CONFIGURED:
        return None
    if _supabase_client is None:
        from supabase import create_client  # imported lazily so local mode
        _supabase_client = create_client(SUPABASE_URL, SUPABASE_KEY)
    return _supabase_client


def _mark_synced() -> None:
    _meta["mode"] = "supabase"
    _meta["stale"] = False
    _meta["last_sync"] = _now_iso()
    _meta["last_error"] = None


def _mark_stale(err: Exception) -> None:
    _meta["stale"] = True
    _meta["last_error"] = str(err)


def get_sync_meta() -> dict:
    with _lock:
        return dict(_meta)


# --------------------------------------------------------------------------
# Generic helpers that try Supabase first, then fall back to local cache.
# A successful Supabase read also refreshes the local cache mirror so a
# later outage still has something recent to degrade to.
# --------------------------------------------------------------------------

def _sb_select(table: str, filters: Optional[dict] = None, order: Optional[str] = None) -> Optional[list]:
    client = _sb()
    if client is None:
        return None
    try:
        q = client.table(table).select("*")
        for k, v in (filters or {}).items():
            q = q.eq(k, v)
        if order:
            q = q.order(order)
        resp = q.execute()
        rows = resp.data or []
        _mark_synced()
        return rows
    except Exception as e:  # noqa: BLE001 - any network/client failure degrades gracefully
        _mark_stale(e)
        return None


def _sb_insert(table: str, row: dict) -> Optional[dict]:
    client = _sb()
    if client is None:
        return None
    try:
        resp = client.table(table).insert(row).execute()
        _mark_synced()
        return (resp.data or [row])[0]
    except Exception as e:  # noqa: BLE001
        _mark_stale(e)
        return None


def _sb_update(table: str, row_id: Any, fields: dict) -> Optional[dict]:
    client = _sb()
    if client is None:
        return None
    try:
        resp = client.table(table).update(fields).eq("id", row_id).execute()
        _mark_synced()
        data = resp.data or []
        return data[0] if data else None
    except Exception as e:  # noqa: BLE001
        _mark_stale(e)
        return None


def _local_select(table: str, filters: Optional[dict] = None) -> list:
    rows = _store[table]
    if filters:
        rows = [r for r in rows if all(r.get(k) == v for k, v in filters.items())]
    return list(rows)


def _local_insert(table: str, row: dict) -> dict:
    with _lock:
        row = dict(row)
        row.setdefault("id", _next_id(table))
        _store[table].append(row)
        _save_local()
        return row


def _local_update(table: str, row_id: Any, fields: dict) -> Optional[dict]:
    with _lock:
        for r in _store[table]:
            if r.get("id") == row_id:
                r.update(fields)
                _save_local()
                return r
        return None


def select(table: str, filters: Optional[dict] = None, order: Optional[str] = None) -> list:
    """Read rows. Tries Supabase (if configured) and refreshes the local
    mirror on success; on any failure (or local mode) serves the local
    cache instead, marking the response as stale via get_sync_meta()."""
    with _lock:
        if SUPABASE_CONFIGURED:
            rows = _sb_select(table, filters, order)
            if rows is not None:
                _store[table] = rows  # refresh cache mirror
                _save_local()
                return list(rows)
            # fall through to stale cache
        return _local_select(table, filters)


def insert(table: str, row: dict) -> dict:
    """Write a row. Tries Supabase first (if configured); always also
    mirrors the write into the local cache so state survives a Supabase
    hiccup and demo pacing never depends on network latency."""
    with _lock:
        if SUPABASE_CONFIGURED:
            saved = _sb_insert(table, row)
            if saved is not None:
                _store[table].append(saved)
                _save_local()
                return saved
        return _local_insert(table, row)


def update(table: str, row_id: Any, fields: dict) -> Optional[dict]:
    with _lock:
        if SUPABASE_CONFIGURED:
            saved = _sb_update(table, row_id, fields)
            if saved is not None:
                _local_update(table, row_id, saved)
                return saved
        return _local_update(table, row_id, fields)


# --------------------------------------------------------------------------
# Domain-specific accessors
# --------------------------------------------------------------------------

def list_policies(station: Optional[str] = None, district: Optional[str] = None,
                   active_only: bool = True) -> list:
    filters = {}
    if station:
        filters["station"] = station
    if district:
        filters["district"] = district
    if active_only:
        filters["active"] = True
    return select("policies", filters or None)


def get_policy(policy_id: int) -> Optional[dict]:
    rows = select("policies", {"id": policy_id})
    return rows[0] if rows else None


def get_latest_reading(station: str, parameter: str = "river_level") -> Optional[dict]:
    rows = select("readings", {"station": station, "parameter": parameter})
    if not rows:
        return None
    return sorted(rows, key=lambda r: r.get("timestamp", ""))[-1]


def list_readings(station: Optional[str] = None, parameter: Optional[str] = None) -> list:
    filters = {}
    if station:
        filters["station"] = station
    if parameter:
        filters["parameter"] = parameter
    return select("readings", filters or None)


def add_reading(row: dict) -> dict:
    row.setdefault("timestamp", _now_iso())
    return insert("readings", row)


def list_claims(status: Optional[str] = None) -> list:
    rows = select("claims", {"status": status} if status else None)
    return sorted(rows, key=lambda r: r.get("created_at", ""), reverse=True)


def get_claim(claim_id: int) -> Optional[dict]:
    rows = select("claims", {"id": claim_id})
    return rows[0] if rows else None


def find_cooldown_block(policy_id: int, hazard_type: str, cooldown_days: int = 14) -> Optional[dict]:
    """Spec §2B — a policy that already has a claim for this hazard_type
    within the cooldown window cannot generate another one, regardless of
    whether that claim was approved, rejected, or is still pending: an
    officer decision (or a pending one) already covers this event. Returns
    the blocking claim (so callers can log which one), or None if clear."""
    rows = select("claims", {"policy_id": policy_id, "hazard_type": hazard_type})
    if not rows:
        return None
    cutoff = datetime.now(timezone.utc) - timedelta(days=cooldown_days)
    recent = [r for r in rows if _parse_ts(r.get("created_at")) and _parse_ts(r.get("created_at")) >= cutoff]
    if not recent:
        return None
    return sorted(recent, key=lambda r: r.get("created_at", ""))[-1]


def _parse_ts(ts: Optional[str]):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def create_claim(row: dict) -> dict:
    row.setdefault("status", "Pending Review")
    row.setdefault("created_at", _now_iso())
    row.setdefault("updated_at", row["created_at"])
    row.setdefault("dispatch_status", None)
    row.setdefault("sms_text", None)
    row.setdefault("voice_file", None)
    return insert("claims", row)


def update_claim(claim_id: int, fields: dict) -> Optional[dict]:
    fields = dict(fields)
    fields["updated_at"] = _now_iso()
    return update("claims", claim_id, fields)


def add_audit(entry: dict) -> dict:
    entry.setdefault("timestamp", _now_iso())
    return insert("audit_log", entry)


def list_audit() -> list:
    rows = select("audit_log")
    return sorted(rows, key=lambda r: r.get("timestamp", ""), reverse=True)


# --------------------------------------------------------------------------
# Trace log — belt-and-braces: Supabase/local table AND a flat trace.log
# --------------------------------------------------------------------------

def log_trace(run_id: str, step_type: str, message: str) -> dict:
    ts = datetime.now()
    line = f"[{ts.strftime('%H:%M:%S')}] {step_type:<9} {message}"
    with _lock:
        with open(TRACE_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    entry = {
        "run_id": run_id,
        "ts": ts.isoformat(timespec="seconds"),
        "step_type": step_type,
        "message": message,
        "line": line,
    }
    return insert("trace_log", entry)


def list_trace(limit: int = 200) -> list:
    rows = select("trace_log")
    rows = sorted(rows, key=lambda r: r.get("ts", ""))
    return rows[-limit:]


def read_trace_log_file(limit_lines: int = 200) -> list[str]:
    if not TRACE_LOG_PATH.exists():
        return []
    with open(TRACE_LOG_PATH, "r", encoding="utf-8") as f:
        lines = f.readlines()
    return [l.rstrip("\n") for l in lines[-limit_lines:]]


# --------------------------------------------------------------------------
# Seeding
# --------------------------------------------------------------------------

def is_seeded() -> bool:
    return len(select("policies")) > 0


def wipe_local() -> None:
    """Local-mode only helper for a clean re-seed during development."""
    with _lock:
        global _store
        _store = _empty_store()
        _save_local()
