"""Diagnostic: query every METAR source for one station and report freshness."""
import os
import sys
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

load_dotenv()
from weather_engine import (
    fetch_all_imd_regional_nodes,
    parse_amss_metar,
    parse_amss_time,
    fetch_ogimet_metar,
)

ICAO = sys.argv[1].upper() if len(sys.argv) > 1 else "VIAR"
now = datetime.now(timezone.utc)
print(f"now: {now.strftime('%Y-%m-%d %H:%M UTC')}  station: {ICAO}\n")


def report(label, raw, dt):
    if raw:
        age = f"{int((now - dt).total_seconds() / 60)}m old" if dt else "age unknown"
        print(f"[{label}] {age}: {raw[:90]}")
    else:
        print(f"[{label}] -> nothing")


# NOAA
try:
    r = requests.get(
        f"https://aviationweather.gov/api/data/metar?ids={ICAO}&format=json", timeout=10
    )
    d = r.json()
    if d:
        dt = datetime.fromtimestamp(d[0]["obsTime"], tz=timezone.utc)
        report("NOAA aviationweather", d[0]["rawOb"], dt)
    else:
        print("[NOAA aviationweather] -> empty")
except Exception as e:
    print(f"[NOAA aviationweather] ERROR: {e}")

# AMSS regional nodes
try:
    blob = fetch_all_imd_regional_nodes()
    if blob:
        m = parse_amss_metar(blob, ICAO)
        report("AMSS/IMD nodes", m, parse_amss_time(m) if m else None)
    else:
        print("[AMSS/IMD nodes] -> nothing fetched")
except Exception as e:
    print(f"[AMSS/IMD nodes] ERROR: {e}")

# Ogimet Tier 1B
try:
    raw, dt = fetch_ogimet_metar(ICAO)
    report("Ogimet", raw, dt)
except Exception as e:
    print(f"[Ogimet] ERROR: {e}")

# CheckWX
try:
    key = os.getenv("CHECKWX_API_KEY")
    r = requests.get(
        f"https://api.checkwx.com/metar/{ICAO}/decoded",
        headers={"X-API-Key": key},
        timeout=10,
    )
    d = r.json()
    if d.get("results", 0) > 0:
        item = d["data"][0]
        dt = datetime.fromisoformat(item["observed"].replace("Z", "+00:00"))
        report("CheckWX", item["raw_text"], dt)
    else:
        print(f"[CheckWX] -> no results (HTTP {r.status_code})")
except Exception as e:
    print(f"[CheckWX] ERROR: {e}")

# AVWX
try:
    key = os.getenv("AVWX_API_KEY")
    r = requests.get(
        f"https://avwx.rest/api/metar/{ICAO}",
        headers={"Authorization": f"BEARER {key}"},
        timeout=10,
    )
    d = r.json()
    if "raw" in d:
        dt = datetime.fromisoformat(d["time"]["dt"].replace("Z", "+00:00"))
        report("AVWX", d["raw"], dt)
    else:
        print(f"[AVWX] -> no raw (HTTP {r.status_code}): {str(d)[:120]}")
except Exception as e:
    print(f"[AVWX] ERROR: {e}")
