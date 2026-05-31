import time
import re
import requests
import json
import io
import csv
import gzip
from datetime import datetime, timezone, timedelta
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


def parse_taf_time(raw_taf: str) -> datetime:
    """Helper to extract a true UTC datetime from a DDHHMMZ string in a TAF."""
    match = re.search(r'\b([0-9]{6})Z\b', raw_taf)
    if not match:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        ts = match.group(1)
        day, hour, minute = int(ts[0:2]), int(ts[2:4]), int(ts[4:6])
        now = datetime.now(timezone.utc)
        from datetime import timedelta
        obs_dt = now.replace(day=day, hour=hour, minute=minute, second=0, microsecond=0)
        # If the day seems like it's from late last month (e.g. today is 1st, TAF is 31st)
        if obs_dt > now + timedelta(days=2):
            if now.month == 1:
                obs_dt = obs_dt.replace(year=now.year - 1, month=12)
            else:
                obs_dt = obs_dt.replace(month=now.month - 1)
        return obs_dt
    except:
        return datetime.min.replace(tzinfo=timezone.utc)

def fetch_global_tafs() -> dict:
    """Downloads the live global TAF XML cache from Aviation Weather Center.
    This replaces the flawed NOAA cycles feed which drops off-cycle updates.
    Returns a dictionary mapping ICAO to raw_taf.
    """
    taf_records = {}
    url = "https://aviationweather.gov/data/cache/tafs.cache.xml.gz"
    print(f"[GLOBAL SYNC] Downloading global TAF XML cache from {url}...", flush=True)
    try:
        import io, gzip
        import xml.etree.ElementTree as ET
        
        headers = {"User-Agent": "MetBotLayer2Engine/2.0 (contact@metbot.render)"}
        resp = requests.get(url, headers=headers, timeout=30)
        if resp.status_code == 200:
            with gzip.open(io.BytesIO(resp.content), 'rt', encoding='utf-8') as f:
                xml_data = f.read()
            
            root = ET.fromstring(xml_data)
            data = root.find('data')
            if data is not None:
                for taf in data.findall('TAF'):
                    station = taf.findtext('station_id')
                    raw_text = taf.findtext('raw_text')
                    if station and raw_text:
                        clean_taf = raw_text.strip()
                        if clean_taf.upper().startswith('TAF'):
                            clean_taf = clean_taf[3:].strip()
                        taf_records[station.upper()] = clean_taf
                        
        print(f"[GLOBAL SYNC] Successfully parsed {len(taf_records)} active global TAFs from AWC XML cache.")
    except Exception as e:
        print(f"[GLOBAL SYNC] Failed to fetch global TAF XML cache: {e}")
    
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
    """Downloads the NOAA global METAR CSV cache file.
    Returns a list of tuples: (icao, raw_metar, raw_taf, decoded_data_dict)
    """
    now = datetime.now(timezone.utc)
    records = []
    seen_icaos = set()
    
    print("Fetching global TAF cycle...")
    global_tafs = fetch_global_tafs()
    
    print("Fetching global airports metadata...")
    global_airports = fetch_global_airports()
    
    url = "https://aviationweather.gov/data/cache/metars.cache.csv.gz"
    print(f"[GLOBAL SYNC] Downloading global METAR CSV from {url}...", flush=True)
    try:
        headers = {"User-Agent": "MetBotLayer2Engine/2.0 (contact@metbot.render)"}
        resp = requests.get(url, headers=headers, timeout=30)
        if resp.status_code == 200:
            with gzip.open(io.BytesIO(resp.content), 'rt', encoding='utf-8') as f:
                reader = csv.reader(f)
                # Skip 6 lines of NOAA metadata headers
                for _ in range(6): 
                    next(reader, None)
                    
                for row in reader:
                    if len(row) < 3:
                        continue
                        
                    raw_metar = row[0].strip()
                    icao = row[1].strip().upper()
                    obs_time_str = row[2].strip()
                    
                    if not icao or not raw_metar:
                        continue
                        
                    if icao in seen_icaos:
                        continue
                    seen_icaos.add(icao)
                    
                    try:
                        obs_dt = datetime.fromisoformat(obs_time_str.replace('Z', '+00:00'))
                        time_str = obs_dt.strftime('%Y-%m-%d %H:%M UTC')
                    except:
                        time_str = "N/A"
                        
                    model = parse_raw_metar_to_bulk_record(raw_metar)
                    
                    # Inject airport metadata
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
        else:
            print(f"[GLOBAL SYNC] NOAA returned status {resp.status_code}")
    except Exception as e:
        print(f"[GLOBAL SYNC] Download failed: {e}", flush=True)

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

    # ---------- Source 2: AAI Portal (DEAD) ----------
    # The aim-india.aai.aero portal is dead (404), so we skip it.
    # We will use Ogimet below to fetch TAFs for the ICAOs we extracted from AMSS.

    # ---------- Extract ALL Indian METARs and TAFs from combined text ----------
    # Indian ICAO prefixes: VA (West), VE (East), VI (North), VO (South)
    pattern = r'\b(V[AEIO][A-Z]{2})\s+([0-9]{6}Z[^=\n]*)'
    matches = list(re.finditer(pattern, combined_text))

    indian_tafs = {}

    for m in matches:
        raw = m.group(0).strip().rstrip('=')
        icao = m.group(1)

        # Distinguish TAF lines (contain validity periods like 2218/2400)
        if re.search(r'\b[0-9]{4}/[0-9]{4}\b', raw):
            if icao not in indian_tafs:
                clean_taf = raw if raw.startswith('TAF') else 'TAF ' + raw
                indian_tafs[icao] = clean_taf
            continue

        # Keep only the first (newest) occurrence of each METAR
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

    print(f"[INDIA SYNC] Extracted {len(indian_records)} unique Indian METARs.", flush=True)

    if not indian_records:
        print("[INDIA SYNC] No Indian METARs found. Skipping bulk insert.", flush=True)
        return

    # ---------- Source 3: Ogimet Bulk TAF Ingestion ----------
    # The US AWC global feed drops intermediate updates for Indian regional airports (e.g. VOCB).
    # To guarantee 100% live data, we fetch ALL Indian TAFs directly from Ogimet.
    # To prevent IP bans, we chunk the ~100 ICAOs into groups of 25 and do 4 HTTP requests.
    print("[INDIA SYNC] Fetching ALL domestic TAFs directly from Ogimet in chunks to bypass stale global feeds...", flush=True)
    
    indian_icaos_list = list(indian_records.keys())
    chunk_size = 25
    chunks = [indian_icaos_list[i:i + chunk_size] for i in range(0, len(indian_icaos_list), chunk_size)]
    
    def fetch_ogimet_chunk(icao_chunk):
        try:
            now = datetime.now(timezone.utc)
            from datetime import timedelta
            yesterday = now - timedelta(days=1)
            lugar_str = ",".join(icao_chunk)
            url = (
                f"https://ogimet.com/display_metars2.php?lang=en&lugar={lugar_str}"
                f"&tipo=ALL&ord=REV&nil=SI&fmt=html"
                f"&ano={yesterday.year}&mes={yesterday.month:02d}&day={yesterday.day:02d}"
                f"&hora=00&anof={now.year}&mesf={now.month:02d}"
                f"&dayf={now.day:02d}&horaf={now.hour:02d}&minf=59&send=send"
            )
            r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code == 200:
                text = BeautifulSoup(r.text, 'html.parser').get_text()
                # Ogimet is ord=REV, so the first time we see an ICAO, it's the newest
                found_tafs = {}
                for m in re.finditer(r'\b(V[A-Z]{3})\s+([0-9]{6}Z[^=\n]*)', text, re.IGNORECASE):
                    icao = m.group(1).upper()
                    if icao not in icao_chunk: continue
                    raw = m.group(0).strip().rstrip('=')
                    if re.search(r'\b[0-9]{4}/[0-9]{4}\b', raw):
                        if icao not in found_tafs:
                            clean_taf = raw if raw.startswith('TAF') else 'TAF ' + raw
                            found_tafs[icao] = clean_taf
                return found_tafs
        except Exception as e:
            print(f"[INDIA SYNC] Failed to fetch Ogimet chunk: {e}")
        return {}

    if chunks:
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            for chunk_result in executor.map(fetch_ogimet_chunk, chunks):
                for icao, taf_str in chunk_result.items():
                    if taf_str:
                        indian_tafs[icao] = taf_str

    print(f"[INDIA SYNC] Successfully retrieved {len(indian_tafs)} live domestic TAFs from Ogimet.", flush=True)

    # ---------- Build bulk records and insert ----------
    bulk_records = []
    global_airports = fetch_global_airports()
    now = datetime.now(timezone.utc)
    for icao in indian_records.keys():
        raw_metar, time_str = indian_records.get(icao, ("", "N/A"))
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
            "history": [raw_metar] if raw_metar else [],
            "station_name": station_name,
            "coords_str": coords_str,
            "sun_str": sun_str
        }
        
        local_taf = indian_tafs.get(icao, "")
        bulk_records.append((icao, raw_metar, local_taf, decoded_data))

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


def get_current_ist_hour():
    """Calculates current hour in Indian Standard Time to guide sleep metrics"""
    return (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).hour

if __name__ == "__main__":
    print("[SYNC PROCESS] Background worker initialized. Starting permanent loop...", flush=True)
    while True:
        try:
            full_sync_cycle()
        except Exception as e:
            print(f"[SYNC PROCESS] Fatal error in loop: {e}", flush=True)

        current_hour = get_current_ist_hour()
        
        # TACTICAL INTERVAL CONFIGURATION:
        # At night (21:00 to 06:00 IST), scan aggressively every 5 minutes to bypass delays.
        # During peak hours, scan every 10 minutes to save server compute cycles.
        if current_hour >= 21 or current_hour < 6:
            sleep_minutes = 5
        else:
            sleep_minutes = 10

        print(f"[SYNC PROCESS] Entering restful sleep window for {sleep_minutes} minutes...", flush=True)
        time.sleep(sleep_minutes * 60)
