"""
seed.py — populate Bema Farm with synthetic, clearly-labelled sandbox data
for Dhanusa district (spec §2B, §15).

Run with:  python seed.py          (from inside backend/, venv active)

Idempotent: if policies already exist, does nothing unless --force is
passed. Writes through db.py, so it works identically whether the backend
is running in Supabase mode or local-JSON-fallback mode.

All data below is FICTIONAL, structured to resemble the shape of DHM
(Department of Hydrology and Meteorology) public river-watch readings and
Dhanusa's local administrative geography — it is not real telemetry or a
verified list of real policyholders, and must never be presented as such.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

import db
from stations import DISTRICT, MUNICIPALITIES, PARAMETERS, STATION_NAMES

# (farmer_name, ward, municipality, station, hazard_type)
# Sita Tamang is first on purpose — she is the named policyholder in the
# pitch and this keeps her policy id stable and easy to demo.
FARMERS = [
    ("Sita Tamang", 6, "Janakpurdham Sub-Metropolitan City", "Kamala", "flood"),
    ("Ram Bahadur Yadav", 3, "Janakpurdham Sub-Metropolitan City", "Kamala", "flood"),
    ("Kanchhi Mandal", 9, "Chhireshwarnath Municipality", "Kamala", "flood"),
    ("Bishnu Chaudhary", 2, "Ganeshman Charnath Municipality", "Kamala", "flood"),
    ("Sunita Rai", 6, "Janakpurdham Sub-Metropolitan City", "Kamala", "flood"),
    ("Mohan Sah", 4, "Nagarain Municipality", "Kamala", "flood"),
    ("Radha Devi Mahato", 6, "Janakpurdham Sub-Metropolitan City", "Kamala", "flood"),
    ("Hari Prasad Karki", 5, "Bideha Municipality", "Kamala", "flood"),
    ("Gita Kumari Yadav", 6, "Janakpurdham Sub-Metropolitan City", "Kamala", "flood"),
    ("Dilip Chaudhary", 8, "Mithila Municipality", "Kamala", "flood"),
    ("Anita Tamang", 3, "Janakpurdham Sub-Metropolitan City", "Kamala", "flood"),
    ("Suresh Mandal", 6, "Chhireshwarnath Municipality", "Kamala", "flood"),
    ("Kamala Rai", 1, "Nagarain Municipality", "Kamala", "flood"),
    ("Binod Sah", 6, "Janakpurdham Sub-Metropolitan City", "Kamala", "drought"),
    ("Parbati Chaudhary", 7, "Ganeshman Charnath Municipality", "Kamala", "drought"),
    ("Naresh Yadav", 6, "Janakpurdham Sub-Metropolitan City", "Kamala", "drought"),
    ("Sarita Mahato", 4, "Dhanauji Rural Municipality", "Aurahi", "flood"),
    ("Dipak Rai", 2, "Janaknandini Rural Municipality", "Aurahi", "flood"),
    ("Manisha Karki", 6, "Dhanauji Rural Municipality", "Aurahi", "flood"),
    ("Ramesh Chaudhary", 9, "Mukhiyapatti Musaharniya Rural Municipality", "Aurahi", "flood"),
    ("Laxmi Tamang", 3, "Janaknandini Rural Municipality", "Aurahi", "flood"),
    ("Ganesh Yadav", 6, "Dhanauji Rural Municipality", "Aurahi", "drought"),
    ("Puja Mandal", 5, "Mukhiyapatti Musaharniya Rural Municipality", "Aurahi", "drought"),
    ("Shyam Sah", 2, "Lakshminiya Rural Municipality", "Jhim/Jhanjh Khola", "flood"),
    ("Rita Chaudhary", 6, "Bateshwar Rural Municipality", "Jhim/Jhanjh Khola", "flood"),
    ("Bikash Karki", 4, "Lakshminiya Rural Municipality", "Jhim/Jhanjh Khola", "flood"),
    ("Sabitri Mahato", 7, "Bateshwar Rural Municipality", "Jhim/Jhanjh Khola", "flood"),
    ("Nabin Rai", 6, "Lakshminiya Rural Municipality", "Jhim/Jhanjh Khola", "flood"),
    ("Kalpana Yadav", 3, "Bateshwar Rural Municipality", "Jhim/Jhanjh Khola", "drought"),
    ("Rajendra Chaudhary", 6, "Lakshminiya Rural Municipality", "Ratu", "flood"),
    ("Sanju Tamang", 8, "Aurahi Rural Municipality", "Ratu", "flood"),
    ("Bimal Sah", 5, "Aurahi Rural Municipality", "Ratu", "flood"),
    ("Devi Maya Rai", 6, "Aurahi Rural Municipality", "Ratu", "flood"),
    ("Ashok Mandal", 2, "Janaknandini Rural Municipality", "Ratu", "flood"),
    ("Renu Karki", 6, "Aurahi Rural Municipality", "Ratu", "drought"),
]

# Baselines per station (river_level metres, rainfall in mm). Individual
# policies vary a little around these so a spike doesn't cross every
# policy at once — see build_policies().
STATION_BASELINE = {
    "Kamala": {"river_level": 4.5, "rainfall_3day": 100, "rainfall_30day": 220},
    "Aurahi": {"river_level": 4.0, "rainfall_3day": 90, "rainfall_30day": 200},
    "Jhim/Jhanjh Khola": {"river_level": 3.2, "rainfall_3day": 80, "rainfall_30day": 180},
    "Ratu": {"river_level": 4.2, "rainfall_3day": 95, "rainfall_30day": 210},
}
STATION_CURRENT = {
    "Kamala": {"river_level": 3.82, "rainfall_3day": 62, "rainfall_30day": 205},
    "Aurahi": {"river_level": 3.40, "rainfall_3day": 55, "rainfall_30day": 190},
    "Jhim/Jhanjh Khola": {"river_level": 2.70, "rainfall_3day": 48, "rainfall_30day": 170},
    "Ratu": {"river_level": 3.55, "rainfall_3day": 58, "rainfall_30day": 198},
}

PAYOUT = {"flood": 5000, "drought": 4200}
PREMIUM = {"flood": 500, "drought": 420}


def _phone(i: int) -> str:
    return f"+977-98{40000000 + i * 137:08d}"[:16]


def _flood_triggers(station: str, i: int) -> list[dict]:
    base = STATION_BASELINE[station]
    level_t = round(base["river_level"] + ((i % 5) - 2) * 0.1, 2)
    rain_t = round(base["rainfall_3day"] + ((i % 3) - 1) * 10, 0)
    return [
        {"parameter": "river_level", "operator": ">", "threshold": level_t},
        {"parameter": "rainfall_3day", "operator": ">", "threshold": rain_t},
    ]


def _drought_triggers(station: str, i: int) -> list[dict]:
    base = STATION_BASELINE[station]
    rain30_t = round(base["rainfall_30day"] * 0.75 + ((i % 3) - 1) * 10, 0)
    level_t = round(base["river_level"] * 0.6 + ((i % 4) - 1.5) * 0.05, 2)
    return [
        {"parameter": "rainfall_30day", "operator": "<", "threshold": rain30_t},
        {"parameter": "river_level", "operator": "<", "threshold": level_t},
    ]


def build_policies() -> list[dict]:
    rows = []
    for i, (name, ward, municipality, station, hazard) in enumerate(FARMERS):
        if name == "Sita Tamang":
            # Exact figures from the pitch (spec §1) — kept fixed on purpose.
            triggers = [
                {"parameter": "river_level", "operator": ">", "threshold": 4.5},
                {"parameter": "rainfall_3day", "operator": ">", "threshold": 100},
            ]
        elif hazard == "flood":
            triggers = _flood_triggers(station, i)
        else:
            triggers = _drought_triggers(station, i)

        rows.append(
            {
                "farmer_name": name,
                "ward": ward,
                "municipality": municipality,
                "district": DISTRICT,
                "station": station,
                "hazard_type": hazard,
                "triggers": triggers,
                "payout_amount": PAYOUT[hazard],
                "premium": PREMIUM[hazard],
                "phone": _phone(i),
                "active": True,
            }
        )
    return rows


def build_readings() -> list[dict]:
    """A short recent history per station/parameter, ending just below the
    typical trigger so the demo's /api/simulate/spike and manual-entry
    form are what push it over live."""
    rows = []
    now = datetime.now(timezone.utc)
    for station in STATION_NAMES:
        for parameter in PARAMETERS:
            current = STATION_CURRENT[station][parameter]
            step = current * 0.01 if parameter == "river_level" else current * 0.03
            for hours_ago in (6, 4, 2, 0):
                value = round(current - hours_ago * step / 6, 2)
                ts = (now - timedelta(hours=hours_ago)).isoformat(timespec="seconds")
                unit = "m" if parameter == "river_level" else "mm"
                rows.append(
                    {
                        "station": station,
                        "parameter": parameter,
                        "value": value,
                        "unit": unit,
                        "trend": "rising" if parameter != "rainfall_30day" else "steady",
                        "timestamp": ts,
                        "source": "seeded",
                        "raw_text": (
                            f"DHM {station} gauge - {ts[11:16]} NPT - {parameter} {value}{unit}"
                        ),
                    }
                )
    return rows


def seed_all(force: bool = False) -> None:
    if db.is_seeded() and not force:
        print(f"Already seeded ({len(db.list_policies(active_only=False))} policies). "
              f"Pass --force to wipe and reseed (local mode only).")
        return

    if force and db.SUPABASE_CONFIGURED:
        print("Note: --force does not delete rows from Supabase automatically; "
              "truncate the tables yourself in the Supabase SQL editor if you need a clean reseed.")
    elif force:
        db.wipe_local()

    policies = build_policies()
    readings = build_readings()
    for row in policies:
        db.insert("policies", row)
    for row in readings:
        db.insert("readings", row)

    print(f"Seeded {len(policies)} policies across {len(STATION_NAMES)} {DISTRICT} stations "
          f"and {len(MUNICIPALITIES)} municipalities ({db.get_sync_meta()['mode']} mode).")
    print("Sita Tamang -> Ward 6, Janakpurdham Sub-Metropolitan City, station=Kamala, "
          "flood trigger: river_level>4.5m AND rainfall_3day>100mm, payout=NPR 5,000.")


if __name__ == "__main__":
    seed_all(force="--force" in sys.argv)
