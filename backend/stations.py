"""
stations.py — single source of truth for Bema Farm's geographic scope
(spec §2B): all of Dhanusa district, not one village.

Kept as static reference data (not a DB table) since it rarely changes and
both seed.py and agent.py need to agree on exactly the same station names.
"""

DISTRICT = "Dhanusa"

STATIONS = [
    {"name": "Kamala", "type": "river", "primary": True},
    {"name": "Aurahi", "type": "river", "primary": False},
    {"name": "Jhim/Jhanjh Khola", "type": "khola", "primary": False},
    {"name": "Ratu", "type": "river", "primary": False},
]

STATION_NAMES = [s["name"] for s in STATIONS]

# Representative spread of Dhanusa local units — a synthetic, clearly
# labelled spread (spec §2B), not a claim to reproduce every real ward.
MUNICIPALITIES = [
    "Janakpurdham Sub-Metropolitan City",
    "Chhireshwarnath Municipality",
    "Ganeshman Charnath Municipality",
    "Nagarain Municipality",
    "Bideha Municipality",
    "Mithila Municipality",
    "Dhanauji Rural Municipality",
    "Janaknandini Rural Municipality",
    "Mukhiyapatti Musaharniya Rural Municipality",
    "Lakshminiya Rural Municipality",
    "Bateshwar Rural Municipality",
    "Aurahi Rural Municipality",
]

PARAMETERS = ["river_level", "rainfall_3day", "rainfall_30day"]

COOLDOWN_DAYS = 14
