import time
import re
import requests
import json
from datetime import datetime, timezone
from astral.sun import sun
from astral import LocationInfo
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
    wx_matches = re.finditer(
        r'\b(-|\+|VC)?(TS|SH|DZ|RA|SN|SG|IC|PL|GR|GS|UP|BR|FG|FU|VA|DU|SA|HZ|PY|PO|SQ|FC|SS|DS)+\b',
        raw_metar
    )
    wx_list = [m.group(0) for m in wx_matches]
    if wx_list and model["weather"] != "CAVOK":
        model["weather"] = " ".join(wx_list)

    return model


def fetch_global_tafs() -> dict:
    """Downloads the NOAA 6-hour TAF cycle file containing ALL global TAFs.
    Returns a dictionary mapping ICAO to raw_taf.
    """
    now = datetime.now(timezone.utc)
    # TAF cycles are published at 00, 06, 12, 18
    cycle_hour = (now.hour // 6) * 6
    
    taf_records = {}
    url = f"https://tgftp.nws.noaa.gov/data/forecasts/taf/cycles/{cycle_hour:02d}Z.TXT"
    try:
        resp = requests.get(url, timeout=30)
        if resp.status_code == 200:
            blocks = resp.text.strip().split('\n\n')
            for block in blocks:
                lines = block.strip().split('\n')
                if len(lines) > 1:
                    raw_taf = " ".join([l.strip() for l in lines[1:] if l.strip()])
                    icao_match = re.search(r'\b([A-Z]{4})\b', raw_taf)
                    if icao_match:
                        icao = icao_match.group(1).upper()
                        clean_taf = raw_taf.strip()
                        if clean_taf.upper().startswith('TAF'):
                            clean_taf = clean_taf[3:].strip()
                        if icao not in taf_records:
                            taf_records[icao] = clean_taf
    except Exception as e:
        print(f"Failed to fetch global TAF cycle: {e}")
    
    return taf_records

def fetch_global_airports() -> dict:
    """Downloads the global list of airports and their coordinates."""
    url = "https://raw.githubusercontent.com/mwgg/Airports/master/airports.json"
    try:
        resp = requests.get(url, timeout=15)
        if resp.status_code == 200:
            return resp.json()
    except Exception as e:
        print(f"Failed to fetch global airports list: {e}")
    return {}

def fetch_global_metars() -> list:
    """Downloads the NOAA hourly METAR cycle file containing ALL global METARs.
    Fetches both the current hour and previous hour to ensure we catch airports
    that update at XX:50 (which roll into the previous hour's file).
    Returns a list of tuples: (icao, raw_metar, obs_time_str, decoded_data_dict)
    """
    now = datetime.now(timezone.utc)
    current_hour = now.hour
    prev_hour = (current_hour - 1) % 24
    
    hours_to_fetch = [current_hour, prev_hour]
    records = []
    seen_icaos = set()  # Deduplicate — keep newest only
    
    print("Fetching global TAF cycle...")
    global_tafs = fetch_global_tafs()
    
    print("Fetching global airports metadata...")
    global_airports = fetch_global_airports()
    
    for h in hours_to_fetch:
        url = f"https://tgftp.nws.noaa.gov/data/observations/metar/cycles/{h:02d}Z.TXT"
        print(f"[GLOBAL SYNC] Downloading global METAR dump from {url}...", flush=True)
        try:
            resp = requests.get(url, timeout=30)
            if resp.status_code != 200:
                print(f"[GLOBAL SYNC] NOAA returned status {resp.status_code} for {h:02d}Z", flush=True)
                continue
                
            lines = resp.text.strip().split('\n')
            current_time_str = ""
            
            for line in lines:
                line = line.strip()
                if not line:
                    continue

                if re.match(r'^\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2}$', line):
                    current_time_str = line
                    continue

                icao_match = re.match(r'^([A-Z]{4})\s', line)
                if not icao_match:
                    continue

                icao = icao_match.group(1)
                if icao in seen_icaos:
                    continue
                seen_icaos.add(icao)

                raw_metar = line.strip()

                try:
                    obs_dt = datetime.strptime(current_time_str, "%Y/%m/%d %H:%M").replace(tzinfo=timezone.utc)
                    time_str = obs_dt.strftime('%Y-%m-%d %H:%M UTC')
                except:
                    time_str = "N/A"

                model = parse_raw_metar_to_bulk_record(raw_metar)
                # Inject airport metadata if available
                station_name = "Unknown Station"
                coords_str = ""
                sun_str = ""
                
                airport_info = global_airports.get(icao)
                if airport_info:
                    try:
                        name = airport_info.get("name", "Unknown Station")
                        city = airport_info.get("city", "")
                        country = airport_info.get("country", "")
                        lat = float(airport_info.get("lat", 0.0))
                        lon = float(airport_info.get("lon", 0.0))
                        
                        location_parts = [p for p in [name, city, country] if p]
                        station_name = ", ".join(location_parts) if location_parts else "Unknown Station"
                        
                        lat_dir = "N" if lat >= 0 else "S"
                        lon_dir = "E" if lon >= 0 else "W"
                        coords_str = f"{abs(lat):.2f}°{lat_dir} - {abs(lon):.2f}°{lon_dir}"
                        
                        loc = LocationInfo(latitude=lat, longitude=lon)
                        s = sun(loc.observer, date=now.date())
                        sunrise = s['sunrise'].strftime('%H:%MZ')
                        sunset = s['sunset'].strftime('%H:%MZ')
                        sun_str = f"🌅 {sunrise} 🌇 {sunset}"
                    except Exception:
                        pass
                
                decoded_data = {
                    "icao": icao,
                    "name": station_name,
                    "coords": coords_str,
                    "sun": sun_str,
                    "time": time_str,
                    "model": model,
                    "history": [raw_metar]
                }
                
                raw_taf = global_tafs.get(icao, "")
                records.append((icao, raw_metar, raw_taf, decoded_data))
                
        except Exception as e:
            print(f"[GLOBAL SYNC] Download failed for {h:02d}Z: {e}", flush=True)

    print(f"[GLOBAL SYNC] Parsed {len(records)} unique airports globally.", flush=True)
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


def indian_bulk_sync():
    """Phase 2: Scrapes the AAI portal and AMSS regional nodes ONCE each,
    extracts ALL Indian airport METARs in bulk, parses them, and bulk-inserts
    into Supabase. This covers 50+ Indian airports in ~10 seconds instead of
    scraping them one-by-one.
    """
    import ssl
    import concurrent.futures
    from requests.adapters import HTTPAdapter
    from urllib3.poolmanager import PoolManager
    from bs4 import BeautifulSoup

    class TLSAdapter(HTTPAdapter):
        def init_poolmanager(self, connections, maxsize, block=False):
            ctx = ssl.create_default_context()
            ctx.set_ciphers('DEFAULT@SECLEVEL=1')
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            self.poolmanager = PoolManager(num_pools=connections, maxsize=maxsize, block=block, ssl_context=ctx)

    class CustomAdapter(HTTPAdapter):
        def init_poolmanager(self, *args, **kwargs):
            ctx = ssl.create_default_context()
            ctx.set_ciphers('DEFAULT@SECLEVEL=1')
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            kwargs['ssl_context'] = ctx
            return super().init_poolmanager(*args, **kwargs)

    combined_text = ""
    indian_records = {}  # icao -> (raw_metar, obs_time_str)

    # ---------- Source 1: AMSS Delhi + Regional IMD Nodes ----------
    amss_urls = [
        "https://amssdelhi.gov.in/Palam1.php",
        "https://amssdelhi.gov.in/Palam2.php",
        "https://amssdelhi.gov.in/Palam3.php",
        "https://amssdelhi.gov.in/Palam4.php",
        "https://amssdelhi.gov.in/Palam5.php",
        "https://mwokolkata.imd.gov.in/",
        "https://mausam.imd.gov.in/mumbai/",
        "https://mausam.imd.gov.in/chennai/"
    ]

    def fetch_amss(url):
        try:
            s = requests.Session()
            s.mount('https://', CustomAdapter())
            r = s.get(url, headers={"User-Agent": "Mozilla/5.0"}, verify=False, timeout=10)
            if r.status_code == 200:
                return BeautifulSoup(r.text, 'html.parser').get_text()
        except:
            pass
        return ""

    print("[INDIA SYNC] Scraping AMSS Delhi + IMD regional nodes in parallel...", flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        for text in executor.map(fetch_amss, amss_urls):
            combined_text += text

    # ---------- Source 2: AAI Portal (aim-india.aai.aero) ----------
    print("[INDIA SYNC] Scraping AAI portal...", flush=True)
    try:
        session = requests.Session()
        session.mount('https://', TLSAdapter())
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        }
        session.get("https://aim-india.aai.aero/", headers=headers, timeout=5)
        aai_resp = session.get("https://aim-india.aai.aero/eaip/metar_taf.php", headers=headers, timeout=10)
        if aai_resp.status_code == 200:
            aai_text = BeautifulSoup(aai_resp.text, "html.parser").get_text()
            combined_text += "\n" + aai_text
            print("[INDIA SYNC] AAI portal data received.", flush=True)
        else:
            print(f"[INDIA SYNC] AAI portal returned status {aai_resp.status_code}.", flush=True)
    except Exception as e:
        print(f"[INDIA SYNC] AAI portal unavailable: {e}", flush=True)

    # ---------- Extract ALL Indian METARs from combined text ----------
    # Indian ICAO prefixes: VA (West), VE (East), VI (North), VO (South)
    pattern = r'\b(V[AEIO][A-Z]{2})\s+([0-9]{6}Z[^=\n]*)'
    matches = list(re.finditer(pattern, combined_text))

    for m in matches:
        raw = m.group(0).strip().rstrip('=')
        icao = m.group(1)

        # Skip TAF lines (contain validity periods like 2218/2400)
        if re.search(r'\b[0-9]{4}/[0-9]{4}\b', raw):
            continue

        # Keep only the first (newest) occurrence of each airport
        if icao in indian_records:
            continue

        # Parse observation time from the 6-digit timestamp (DDHHMMZ)
        ts_match = re.search(r'\b([0-9]{6})Z\b', raw)
        time_str = "N/A"
        if ts_match:
            try:
                ts = ts_match.group(1)
                day, hour, minute = int(ts[0:2]), int(ts[2:4]), int(ts[4:6])
                now_dt = datetime.now(timezone.utc)
                obs_dt = now_dt.replace(day=day, hour=hour, minute=minute, second=0, microsecond=0)
                if obs_dt > now_dt:
                    if now_dt.month == 1:
                        obs_dt = obs_dt.replace(year=now_dt.year - 1, month=12)
                    else:
                        obs_dt = obs_dt.replace(month=now_dt.month - 1)
                time_str = obs_dt.strftime('%Y-%m-%d %H:%M UTC')
            except:
                pass

        indian_records[icao] = (raw, time_str)

    print(f"[INDIA SYNC] Extracted {len(indian_records)} unique Indian airport METARs.", flush=True)

    if not indian_records:
        print("[INDIA SYNC] No Indian METARs found. Skipping bulk insert.", flush=True)
        return

    # ---------- Build bulk records and insert ----------
    bulk_records = []
    global_airports = fetch_global_airports()
    now = datetime.now(timezone.utc)
    for icao, (raw_metar, time_str) in indian_records.items():
        model = parse_raw_metar_to_bulk_record(raw_metar)

        station_name = "Unknown Station"
        coords_str = ""
        sun_str = ""
        airport_info = global_airports.get(icao)
        if airport_info:
            try:
                name = airport_info.get("name", "Unknown Station")
                city = airport_info.get("city", "")
                country = airport_info.get("country", "")
                lat = float(airport_info.get("lat", 0.0))
                lon = float(airport_info.get("lon", 0.0))
                
                location_parts = [p for p in [name, city, country] if p]
                station_name = ", ".join(location_parts) if location_parts else "Unknown Station"
                
                lat_dir = "N" if lat >= 0 else "S"
                lon_dir = "E" if lon >= 0 else "W"
                coords_str = f"{abs(lat):.2f}°{lat_dir} - {abs(lon):.2f}°{lon_dir}"
                
                from astral import LocationInfo
                from astral.sun import sun
                loc = LocationInfo(latitude=lat, longitude=lon)
                s = sun(loc.observer, date=now.date())
                sunrise = s['sunrise'].strftime('%H:%MZ')
                sunset = s['sunset'].strftime('%H:%MZ')
                sun_str = f"🌅 {sunrise} 🌇 {sunset}"
            except Exception:
                pass

        decoded_data = {
            "icao": icao,
            "name": station_name,
            "time": time_str,
            "model": model,
            "history": [raw_metar],
            "station_name": station_name,
            "coords_str": coords_str,
            "sun_str": sun_str
        }
        bulk_records.append((icao, raw_metar, "", decoded_data))

    # Bulk write all Indian airports in one transaction
    count = bulk_upsert_weather(bulk_records)
    print(f"[INDIA SYNC] ✅ {count} Indian airports bulk-cached with domestic AMSS/AAI data.", flush=True)


def full_sync_cycle():
    """Runs a complete sync cycle: Global bulk first, then Indian bulk override."""
    print(f"\n{'='*60}", flush=True)
    print(f"[SYNC] Starting full sync cycle at {datetime.now(timezone.utc).strftime('%H:%M UTC')}", flush=True)
    print(f"{'='*60}", flush=True)

    # Phase 1: Bulk-load the entire planet (~4,600 airports in ~5 seconds)
    global_bulk_sync()

    # Phase 2: Bulk-override ALL Indian airports with domestic AMSS/AAI data (~10 seconds)
    indian_bulk_sync()

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
