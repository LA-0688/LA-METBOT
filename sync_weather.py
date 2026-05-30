import time
import re
import requests
from datetime import datetime, timezone
from db_manager import upsert_weather, bulk_upsert_weather
from weather_engine import get_station_details

# =====================================================================
# GLOBAL BULK INGESTION ENGINE
# Downloads ALL ~4,600 airport METARs from NOAA in a single HTTP call,
# parses them into the decoded_data JSON schema, and bulk-upserts
# everything into Supabase in one transaction.
# =====================================================================

# Indian airports that need the specialized AAI/AMSS/Ogimet scraper
# because NOAA's global feed is notoriously delayed for Indian stations.
INDIAN_PRIORITY_STATIONS = ["VIDP", "VABB", "VECC", "VOBL", "VAAH", "VOMM"]

# Batch size for database writes (avoids overwhelming the connection)
BULK_BATCH_SIZE = 500


def parse_raw_metar_to_bulk_record(raw_metar: str) -> dict:
    """Parses a raw METAR string into the decoded_data JSON structure
    matching the exact schema used by get_station_details() output.
    This ensures the frontend modal renders identically whether
    the data came from bulk ingestion or from the live scraper.
    """
    model = {
        "temp": "N/A",
        "dew": "N/A",
        "windDir": 0,
        "windSpeed": 0,
        "windStr": "N/A",
        "visibility": "N/A",
        "clouds": "CLEAR",
        "weather": "NONE",
        "altimeter": "N/A"
    }

    # 1. Wind: e.g. 22010KT or VRB02KT or 22010G25KT
    wind_match = re.search(r'\b([0-9]{3}|VRB)([0-9]{2,3})(?:G([0-9]{2,3}))?KT\b', raw_metar)
    if wind_match:
        wdir_str = wind_match.group(1)
        wspd = int(wind_match.group(2))
        wgst = wind_match.group(3)
        wdir = 0 if wdir_str == "VRB" else int(wdir_str)
        model["windDir"] = wdir
        model["windSpeed"] = wspd
        wind_str = f"{wdir_str}° / {wspd}"
        if wgst:
            wind_str += f"G{wgst}"
        wind_str += " KT"
        model["windStr"] = wind_str

    # 2. Visibility: 9999, 6000, or 10SM, 3SM etc.
    if "CAVOK" in raw_metar:
        model["visibility"] = "10+ Kms"
        model["weather"] = "CAVOK"
        model["clouds"] = "CAVOK"
    else:
        vis_match = re.search(r'\b([0-9]{4})\b', raw_metar)
        if vis_match:
            vis_val = int(vis_match.group(1))
            if vis_val == 9999:
                model["visibility"] = "10+ Kms"
            else:
                model["visibility"] = f"{vis_val}m"
        else:
            vis_sm = re.search(r'\b([0-9]+)SM\b', raw_metar)
            if vis_sm:
                sm_val = int(vis_sm.group(1))
                km_val = round(sm_val * 1.60934, 1)
                if km_val >= 9.9:
                    model["visibility"] = "10+ Kms"
                else:
                    model["visibility"] = f"{km_val} Kms"

    # 3. Temperature/Dewpoint: e.g. 32/25 or M02/M05
    temp_match = re.search(r'\b(M?[0-9]{2})/(M?[0-9]{2})\b', raw_metar)
    if temp_match:
        def convert_temp(t_str):
            return f"{int(t_str.replace('M', '-'))}°C"
        model["temp"] = convert_temp(temp_match.group(1))
        model["dew"] = convert_temp(temp_match.group(2))

    # 4. Altimeter: Q1013 or A2992
    alt_q = re.search(r'\bQ([0-9]{4})\b', raw_metar)
    if alt_q:
        model["altimeter"] = f"Q{alt_q.group(1)} hPa"
    else:
        alt_a = re.search(r'\bA([0-9]{4})\b', raw_metar)
        if alt_a:
            try:
                inhg = float(alt_a.group(1)) / 100.0
                hpa = int(round(inhg * 33.8639))
                model["altimeter"] = f"Q{hpa} hPa"
            except:
                pass

    # 5. Clouds
    cloud_groups = re.findall(r'\b(FEW|SCT|BKN|OVC|CLR|SKC|NSC|CAVOK)([0-9]{3})?\b', raw_metar)
    if cloud_groups:
        cloud_strs = []
        for cvr, base in cloud_groups:
            if cvr in ["CLR", "SKC", "NSC", "CAVOK"]:
                continue
            base_str = base if base else ""
            cloud_strs.append(f"{cvr}{base_str}")
        if cloud_strs:
            model["clouds"] = " ".join(cloud_strs)

    # 6. Weather phenomena
    if "NOSIG" in raw_metar:
        model["weather"] = "NOSIG"
    else:
        wx_match = re.search(
            r'\b(-|\+|VC)?(TS|SH|DZ|RA|SN|SG|IC|PL|GR|GS|UP|BR|FG|FU|VA|DU|SA|HZ|PY|PO|SQ|FC|SS|DS)+\b',
            raw_metar
        )
        if wx_match and model["weather"] != "CAVOK":
            model["weather"] = wx_match.group(0)

    return model


def fetch_global_metars() -> list:
    """Downloads the NOAA hourly METAR cycle file containing ALL global METARs.
    Returns a list of tuples: (icao, raw_metar, obs_time_str, decoded_data_dict)
    """
    hour = datetime.now(timezone.utc).hour
    url = f"https://tgftp.nws.noaa.gov/data/observations/metar/cycles/{hour:02d}Z.TXT"

    print(f"[GLOBAL SYNC] Downloading global METAR dump from {url}...", flush=True)
    try:
        resp = requests.get(url, timeout=30)
        if resp.status_code != 200:
            print(f"[GLOBAL SYNC] NOAA returned status {resp.status_code}", flush=True)
            return []
    except Exception as e:
        print(f"[GLOBAL SYNC] Download failed: {e}", flush=True)
        return []

    lines = resp.text.strip().split('\n')
    print(f"[GLOBAL SYNC] Downloaded {len(lines)} lines. Parsing...", flush=True)

    records = []
    seen_icaos = set()  # Deduplicate — NOAA file has duplicates, keep only the first (newest)
    current_time_str = ""

    for line in lines:
        line = line.strip()
        if not line:
            continue

        # Time header lines look like: 2026/05/30 17:15
        if re.match(r'^\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2}$', line):
            current_time_str = line
            continue

        # METAR lines start with a 4-letter ICAO code
        icao_match = re.match(r'^([A-Z]{4})\s', line)
        if not icao_match:
            continue

        icao = icao_match.group(1)

        # Skip duplicates — first occurrence is the newest
        if icao in seen_icaos:
            continue
        seen_icaos.add(icao)

        raw_metar = line.strip()

        # Parse the observation time
        try:
            obs_dt = datetime.strptime(current_time_str, "%Y/%m/%d %H:%M").replace(tzinfo=timezone.utc)
            time_str = obs_dt.strftime('%Y-%m-%d %H:%M UTC')
        except:
            time_str = "N/A"

        # Decode the raw METAR into the visual grid JSON
        model = parse_raw_metar_to_bulk_record(raw_metar)

        decoded_data = {
            "icao": icao,
            "name": "Unknown Station",  # Station names are filled by the frontend
            "time": time_str,
            "model": model,
            "history": [raw_metar]
        }

        records.append((icao, raw_metar, "", decoded_data))

    print(f"[GLOBAL SYNC] Parsed {len(records)} unique airports.", flush=True)
    return records


def global_bulk_sync():
    """Phase 1: Downloads and bulk-inserts ALL global METARs into Supabase."""
    records = fetch_global_metars()
    if not records:
        print("[GLOBAL SYNC] No records to insert. Skipping.", flush=True)
        return 0

    total_inserted = 0
    # Insert in batches to avoid overwhelming the DB connection
    for i in range(0, len(records), BULK_BATCH_SIZE):
        batch = records[i : i + BULK_BATCH_SIZE]
        count = bulk_upsert_weather(batch)
        total_inserted += count
        if count > 0:
            print(f"[GLOBAL SYNC] Batch {i // BULK_BATCH_SIZE + 1}: {count} records written.", flush=True)
        time.sleep(0.5)  # Brief pause between batches

    print(f"[GLOBAL SYNC] ✅ Total: {total_inserted} airports cached worldwide.", flush=True)
    return total_inserted


def indian_priority_sync():
    """Phase 2: Runs the specialized AAI/AMSS/Ogimet scraper ONLY for Indian airports
    that need higher-quality domestic data sources not available in the NOAA feed.
    """
    print(f"[INDIA SYNC] Refreshing {len(INDIAN_PRIORITY_STATIONS)} priority Indian airports...", flush=True)
    for icao in INDIAN_PRIORITY_STATIONS:
        try:
            print(f"[INDIA SYNC] Syncing: {icao}", flush=True)
            live_result = get_station_details(icao)

            raw_metar = live_result.get('history', [''])[0] if live_result.get('history') else ''
            raw_taf = ''
            decoded_payload = live_result

            upsert_weather(
                icao=icao,
                raw_metar=raw_metar,
                raw_taf=raw_taf,
                decoded_json=decoded_payload
            )
            time.sleep(1)  # Gentle cooldown between scrapes
        except Exception as e:
            print(f"[INDIA SYNC] Error syncing {icao}: {e}", flush=True)

    print(f"[INDIA SYNC] ✅ Indian priority stations refreshed.", flush=True)


def full_sync_cycle():
    """Runs a complete sync cycle: Global bulk first, then Indian priority override."""
    print(f"\n{'='*60}", flush=True)
    print(f"[SYNC] Starting full sync cycle at {datetime.now(timezone.utc).strftime('%H:%M UTC')}", flush=True)
    print(f"{'='*60}", flush=True)

    # Phase 1: Bulk-load the entire planet (~4,600 airports in ~5 seconds)
    global_bulk_sync()

    # Phase 2: Override Indian airports with specialized high-quality domestic data
    indian_priority_sync()

    print(f"[SYNC] ✅ Full cycle complete.\n", flush=True)


if __name__ == "__main__":
    print("[SYNC PROCESS] Background worker initialized. Starting permanent loop...", flush=True)
    while True:
        try:
            full_sync_cycle()
        except Exception as e:
            print(f"[SYNC PROCESS] Fatal error in loop: {e}", flush=True)

        print("[SYNC PROCESS] Sleeping for 25 minutes before next run...", flush=True)
        time.sleep(25 * 60)  # Wait 25 minutes before running again
