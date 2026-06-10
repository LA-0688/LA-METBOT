import requests, time
from datetime import datetime, timezone, timedelta
from typing import Dict, Any
import concurrent.futures
import os
import re
import urllib3
import ssl
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from urllib3.poolmanager import PoolManager

def _legacy_ssl_context():
    """SSL context for older government servers that negotiate small DH keys /
    legacy ciphers. We relax the cipher SECLEVEL (the actual reason these
    adapters exist) but KEEP certificate and hostname verification on, so the
    connection is still authenticated and not open to MITM.
    """
    ctx = ssl.create_default_context()
    ctx.set_ciphers('DEFAULT@SECLEVEL=1')
    return ctx

class CustomAdapter(requests.adapters.HTTPAdapter):
    """Adapter to bypass DH_KEY_TOO_SMALL errors on older government servers,
    while still verifying certificates."""
    def init_poolmanager(self, *args, **kwargs):
        kwargs['ssl_context'] = _legacy_ssl_context()
        return super().init_poolmanager(*args, **kwargs)

class TLSAdapter(requests.adapters.HTTPAdapter):
    """Adapter to handle older Indian gov server SSL handshakes, while still
    verifying certificates."""
    def init_poolmanager(self, connections, maxsize, block=False):
        self.poolmanager = PoolManager(num_pools=connections, maxsize=maxsize, block=block, ssl_context=_legacy_ssl_context())


load_dotenv()

def resolve_obs_time(day, hour, minute):
    """Resolves a DDHHMM observation group to a real UTC datetime.

    Tries the current month then the previous month, skipping impossible dates
    (e.g. day 31 in a 30-day month) instead of raising, and returns the most
    recent candidate that is not in the future. Returns None if none is valid.
    """
    if not (1 <= day <= 31 and 0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    now = datetime.now(timezone.utc)
    for months_back in (0, 1):
        month = now.month - months_back
        year = now.year
        if month < 1:
            month += 12
            year -= 1
        try:
            cand = datetime(year, month, day, hour, minute, tzinfo=timezone.utc)
        except ValueError:
            continue
        if cand <= now + timedelta(minutes=5):
            return cand
    return None

# ---------- Helper: AMSS Delhi Scraper ----------
def fetch_all_imd_regional_nodes() -> str:
    """Fetches the raw AMSS feed and regional IMD nodes in parallel to bypass SSL restrictions."""
    headers = {"User-Agent": "Mozilla/5.0"}
    urls = [
        "https://amssdelhi.gov.in/Palam1.php",
        "https://amssdelhi.gov.in/Palam2.php",
        "https://amssdelhi.gov.in/Palam3.php",
        "https://amssdelhi.gov.in/Palam4.php",
        "https://amssdelhi.gov.in/Palam5.php",
        "https://mwokolkata.imd.gov.in/",
        "https://mausam.imd.gov.in/mumbai/",
        "https://mausam.imd.gov.in/chennai/"
    ]
    
    def _fetch(url):
        try:
            session = requests.Session()
            session.mount('https://', CustomAdapter())
            resp = session.get(url, headers=headers, timeout=8)
            if resp.status_code == 200:
                return BeautifulSoup(resp.text, 'html.parser').get_text() + "\n"
        except Exception:
            pass
        return ""
        
    global _imd_node_cache
    cached_text, cached_ts = _imd_node_cache
    if cached_text and (time.time() - cached_ts) < 300:  # 5-minute TTL
        return cached_text

    combined_text = ""
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        for text in executor.map(_fetch, urls):
            if text:
                combined_text += text
    _imd_node_cache = (combined_text, time.time())
    return combined_text

def parse_amss_metar(raw_text: str, icao: str) -> str:
    if not raw_text or not icao.upper().startswith('V'):
        return None
    # Stop at the line end so we never merge two stacked reports into one.
    pattern = rf"({icao.upper()})\s+([0-9]{{6}}Z[^=\n]*=?)"
    # The combined feed stacks several regional pages, each of which may carry
    # a different cycle of the same station. Scan every match and keep the
    # newest observation rather than whichever page happens to appear first.
    best_metar = None
    best_dt = None
    for match in re.finditer(pattern, raw_text, re.IGNORECASE):
        candidate = f"{match.group(1)} {match.group(2)}".strip().rstrip("=")
        # Skip TAF lines (they carry a validity period like 1000/1106).
        if re.search(r'\b[0-9]{4}/[0-9]{4}\b', candidate):
            continue
        obs_dt = parse_amss_time(candidate)
        if best_metar is None or (obs_dt and (best_dt is None or obs_dt > best_dt)):
            best_metar = candidate
            best_dt = obs_dt
    return best_metar

def parse_amss_time(metar_str: str) -> datetime | None:
    """Robustly parse the day/hour/minute timestamp from an AMSS METAR string.
    Returns a UTC datetime or None on any failure.
    """
    try:
        # Find the 6-digit timestamp group (DDHHMMZ) anywhere in the string
        m = re.search(r'\b([0-9]{6})Z\b', metar_str)
        if not m:
            return None
        ts = m.group(1)
        day = int(ts[0:2])
        hour = int(ts[2:4])
        minute = int(ts[4:6])
        return resolve_obs_time(day, hour, minute)
    except Exception:
        return None

def fetch_live_aai_weather(icao: str) -> tuple[str | None, datetime | None]:
    """Tier 1A: Scrapes the live Airports Authority of India (AAI) portal
    to fetch VOGO/VAPO METAR weather directly from the domestic stream.
    """
    if not icao.upper().startswith('V'):
        return None, None
        
    url = "https://aim-india.aai.aero/eaip/metar_taf.php"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    }
    
    try:
        session = requests.Session()
        session.mount('https://', TLSAdapter())
        # AAI requires hitting root first to establish session cookies
        session.get("https://aim-india.aai.aero/", headers=headers, timeout=4)
        
        # Now pull the raw domestic metar grid
        response = session.get(url, headers=headers, timeout=5)
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, "html.parser")
            page_text = soup.get_text()
            
            # Find any report starting with ICAO
            pattern = rf"\b({icao.upper()})\s+([0-9]{{6}}Z[^=\n]*)"
            matches = re.finditer(pattern, page_text, re.IGNORECASE)
            for m in matches:
                raw = m.group(0).strip().rstrip('=')
                # Exclude TAF lines (which have validity period e.g. 2218/2400)
                if re.search(r'\b[0-9]{4}/[0-9]{4}\b', raw):
                    continue
                obs_dt = parse_amss_time(raw)
                if obs_dt:
                    return raw, obs_dt
    except Exception:
        pass
    return None, None

def fetch_aai_taf(icao: str) -> tuple[str | None, datetime | None]:
    """Tier 1A: Scrapes the live Airports Authority of India (AAI) portal
    to fetch VOGO/VAPO TAF weather directly from the domestic stream.
    """
    if not icao.upper().startswith('V'):
        return None, None
        
    url = "https://aim-india.aai.aero/eaip/metar_taf.php"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    }
    
    try:
        session = requests.Session()
        session.mount('https://', TLSAdapter())
        session.get("https://aim-india.aai.aero/", headers=headers, timeout=4)
        response = session.get(url, headers=headers, timeout=5)
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, "html.parser")
            page_text = soup.get_text()
            
            pattern = rf"\b({icao.upper()})\s+([0-9]{{6}}Z[^=\n]*)"
            matches = re.finditer(pattern, page_text, re.IGNORECASE)
            for m in matches:
                raw = m.group(0).strip().rstrip('=')
                # Only match TAF lines (must contain validity period e.g. 2218/2400)
                if re.search(r'\b[0-9]{4}/[0-9]{4}\b', raw):
                    obs_dt = parse_amss_time(raw)
                    return raw, obs_dt
    except Exception:
        pass
    return None, None

def fetch_ogimet_metar(icao: str) -> tuple[str | None, datetime | None]:
    """Tier 1B: Scrape the latest METAR for an Indian station from Ogimet.
    Returns (raw_metar_string, observation_datetime) or (None, None) on failure.
    """
    try:
        now = datetime.now(timezone.utc)
        from datetime import timedelta
        yesterday = now - timedelta(days=1)
        url = (
            f"https://ogimet.com/display_metars2.php?lang=en&lugar={icao.upper()}"
            f"&tipo=ALL&ord=REV&nil=SI&fmt=html"
            f"&ano={yesterday.year}&mes={yesterday.month:02d}&day={yesterday.day:02d}"
            f"&hora=00&anof={now.year}&mesf={now.month:02d}"
            f"&dayf={now.day:02d}&horaf={now.hour:02d}&minf=59&send=send"
        )
        r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            return None, None
        text = BeautifulSoup(r.text, 'html.parser').get_text()
        
        # Regex to find any report starting with ICAO
        pattern = rf"\b({icao.upper()})\s+([0-9]{{6}}Z[^=\n]*)"
        matches = re.finditer(pattern, text, re.IGNORECASE)
        for m in matches:
            raw = m.group(0).strip().rstrip('=')
            
            # Exclude TAF lines (which have validity period e.g. 2218/2400)
            if re.search(r'\b[0-9]{4}/[0-9]{4}\b', raw):
                continue
                
            obs_dt = parse_amss_time(raw)
            if obs_dt:
                return raw, obs_dt
    except Exception:
        pass
    return None, None

def fetch_ogimet_taf(icao: str) -> tuple[str | None, datetime | None]:
    """Tier 1B: Scrape the latest TAF for an Indian station from Ogimet.
    Returns (raw_taf_string, observation_datetime) or (None, None) on failure.
    """
    try:
        now = datetime.now(timezone.utc)
        from datetime import timedelta
        yesterday = now - timedelta(days=1)
        url = (
            f"https://ogimet.com/display_metars2.php?lang=en&lugar={icao.upper()}"
            f"&tipo=ALL&ord=REV&nil=SI&fmt=html"
            f"&ano={yesterday.year}&mes={yesterday.month:02d}&day={yesterday.day:02d}"
            f"&hora=00&anof={now.year}&mesf={now.month:02d}"
            f"&dayf={now.day:02d}&horaf={now.hour:02d}&minf=59&send=send"
        )
        r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            return None, None
        text = BeautifulSoup(r.text, 'html.parser').get_text()
        
        # Regex to find any report starting with ICAO
        pattern = rf"\b({icao.upper()})\s+([0-9]{{6}}Z[^=\n]*)"
        matches = re.finditer(pattern, text, re.IGNORECASE)
        for m in matches:
            raw = m.group(0).strip().rstrip('=')
            
            # Only match TAF lines (must contain validity period e.g. 2218/2400)
            if re.search(r'\b[0-9]{4}/[0-9]{4}\b', raw):
                obs_dt = parse_amss_time(raw)
                if obs_dt:
                    return raw, obs_dt
    except Exception:
        pass
    return None, None


# ---------- Helper: robust GET with retries ----------
def safe_get(url: str, *, timeout: int = 3, retries: int = 1) -> Any:
    """Fetch JSON with exponential back-off to prevent random timeout errors."""
    backoff = 1
    for attempt in range(retries + 1):
        try:
            resp = requests.get(url, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            if attempt == retries:
                raise
            time.sleep(backoff)
            backoff *= 2 # 1 -> 2 -> 4 seconds

# ---------- Hardcoded Fallbacks for Missing Stations ----------
KNOWN_STATIONS = {
    'VANM': {'name': 'Navi Mumbai International Airport', 'lat': 18.99, 'lon': 73.06},
    'VEAB': {'name': 'Allahabad / Prayagraj Airport', 'lat': 25.4390, 'lon': 81.7339}
}

# ---------- In-memory caches ----------
weather_cache: Dict[str, tuple[str, float]] = {}
sun_cache: Dict[str, tuple[Dict[str, str], float]] = {}
_imd_node_cache: tuple[str, float] = ("", 0.0)  # (text, timestamp)
CACHE_TTL = 60

def get_sun_times(lat, lon):
    """Fetch sunrise and sunset times for given coordinates."""
    cache_key = f"{lat},{lon}"
    if cache_key in sun_cache:
        data, ts = sun_cache[cache_key]
        if time.time() - ts < 3600: # Cache sun times for 1 hour
            return data
            
    try:
        url = f"https://api.sunrise-sunset.org/json?lat={lat}&lng={lon}&formatted=0"
        resp = requests.get(url, timeout=5)
        if resp.status_code == 200:
            results = resp.json().get('results', {})
            # Extract and format times to HH:MM UTC
            def format_time(t_str):
                try:
                    dt = datetime.fromisoformat(t_str.replace('Z', '+00:00'))
                    return dt.strftime('%H:%M') + "Z"
                except Exception:
                    return "N/A"
            
            data = {
                'sunrise': format_time(results.get('sunrise')),
                'sunset': format_time(results.get('sunset'))
            }
            sun_cache[cache_key] = (data, time.time())
            return data
    except Exception:
        pass
    return {'sunrise': 'N/A', 'sunset': 'N/A'}

def format_visibility(vis_sm, station_code):
    if vis_sm == 'N/A' or vis_sm is None:
        return 'N/A'
    
    vis_str = str(vis_sm)
    if station_code.startswith('K'):
        return f"{vis_str} SM"
        
    try:
        val = float(vis_str.replace('+', '').replace('<', '').replace('>', '').strip())
        km_val = round(val * 1.60934, 1)
        if km_val >= 9.9:
            return "10+ km"
        prefix = '<' if '<' in vis_str else ('>' if '>' in vis_str else '')
        return f"{prefix}{km_val} km"
    except ValueError:
        return f"{vis_str}"

def decode_wx(wx_string):
    if not wx_string or wx_string == 'N/A': return ""
    wx_map = {
        '-': 'Light ', '+': 'Heavy ', 'VC': 'Vicinity ',
        'MI': 'Shallow ', 'PR': 'Partial ', 'BC': 'Patches ', 'DR': 'Low Drifting ',
        'BL': 'Blowing ', 'SH': 'Showers ', 'TS': 'Thunderstorm ', 'FZ': 'Freezing ',
        'DZ': 'Drizzle', 'RA': 'Rain', 'SN': 'Snow', 'SG': 'Snow Grains',
        'IC': 'Ice Crystals', 'PL': 'Ice Pellets', 'GR': 'Hail', 'GS': 'Small Hail',
        'UP': 'Unknown Precip', 'BR': 'Mist', 'FG': 'Fog', 'FU': 'Smoke',
        'VA': 'Volcanic Ash', 'DU': 'Widespread Dust', 'SA': 'Sand', 'HZ': 'Haze',
        'PO': 'Dust/Sand Whirls', 'SQ': 'Squalls', 'FC': 'Funnel Cloud',
        'SS': 'Sandstorm', 'DS': 'Duststorm'
    }
    decoded = []
    for code in wx_string.split():
        desc = ""
        while code:
            found = False
            for k in sorted(wx_map.keys(), key=len, reverse=True):
                if code.startswith(k):
                    desc += wx_map[k]
                    code = code[len(k):]
                    found = True
                    break
            if not found:
                desc += code + " "
                code = ""
        decoded.append(desc.strip().title())
    return ", ".join(decoded)

def decode_clouds(clouds_list):
    if not clouds_list: return "Clear"
    cover_map = {'FEW': 'Few', 'SCT': 'Scattered', 'BKN': 'Broken', 'OVC': 'Overcast', 'CAVOK': 'Clear', 'CLR': 'Clear', 'SKC': 'Clear', 'VV': 'Vertical Visibility'}
    res = []
    for c in clouds_list:
        cover = cover_map.get(c.get('cover'), c.get('cover'))
        base = c.get('base')
        if base:
            res.append(f"{cover} at {base} ft")
        else:
            res.append(f"{cover}")
    return ", ".join(res)

def get_instant_weather(stations: str) -> str:
    """Fetches and decodes live METAR, TAF, and D-ATIS instantly."""
    stations_list = [s.strip().upper() for s in stations.replace(",", " ").split() if s.strip()]
    if not stations_list:
        return "Please provide at least one station code."
        
    clean_stations = ",".join(stations_list)
    result_text = ""
    
    # Cache Lookup — use a normalized key so single-station calls hit the same cache
    cache_key = clean_stations
    current_time = time.time()
    if cache_key in weather_cache:
        cached_data, timestamp = weather_cache[cache_key]
        if current_time - timestamp < 60: # 1 minute TTL
            return cached_data
    
    try:
        # 1. Fetch ALL data in parallel: METAR, TAF, D-ATIS, sun times, and NOAA fallbacks
        urls = {
            'metar': f"https://aviationweather.gov/api/data/metar?ids={clean_stations}&format=json",
            'taf': f"https://aviationweather.gov/api/data/taf?ids={clean_stations}&format=json"
        }
        
        # Add D-ATIS URLs and NOAA fallback URLs for each station
        for s in stations_list:
            urls[f'atis_{s}'] = f"https://datis.clowd.io/api/{s}"
            urls[f'noaa_metar_{s}'] = f"https://tgftp.nws.noaa.gov/data/observations/metar/stations/{s}.TXT"
            urls[f'noaa_taf_{s}'] = f"https://tgftp.nws.noaa.gov/data/forecasts/taf/stations/{s}.TXT"
            urls[f'checkwx_metar_{s}'] = f"https://api.checkwx.com/metar/{s}/decoded"
            urls[f'checkwx_taf_{s}'] = f"https://api.checkwx.com/taf/{s}/decoded"
            urls[f'avwx_metar_{s}'] = f"https://avwx.rest/api/metar/{s}"
            urls[f'avwx_taf_{s}'] = f"https://avwx.rest/api/taf/{s}"
            if s.upper().startswith('V'):
                urls[f'aai_metar_{s}'] = f"AAI_TRIGGER_{s}"
                urls[f'aai_taf_{s}'] = f"AAI_TAF_TRIGGER_{s}"
                urls[f'ogimet_metar_{s}'] = f"OGIMET_METAR_TRIGGER_{s}"
                urls[f'ogimet_taf_{s}'] = f"OGIMET_TAF_TRIGGER_{s}"
            
        urls['station_info'] = f"https://aviationweather.gov/api/data/stationinfo?ids={clean_stations}"
        
        if any(s.upper().startswith('V') for s in stations_list):
            urls['amss_trigger'] = 'AMSS_TRIGGER'
            
        def fetch_url(name, url):
            try:
                if url == 'AMSS_TRIGGER':
                    return name, fetch_all_imd_regional_nodes()
                elif url.startswith('AAI_TRIGGER_'):
                    station_code = url.replace('AAI_TRIGGER_', '')
                    return name, fetch_live_aai_weather(station_code)
                elif url.startswith('AAI_TAF_TRIGGER_'):
                    station_code = url.replace('AAI_TAF_TRIGGER_', '')
                    return name, fetch_aai_taf(station_code)
                elif url.startswith('OGIMET_METAR_TRIGGER_'):
                    station_code = url.replace('OGIMET_METAR_TRIGGER_', '')
                    return name, fetch_ogimet_metar(station_code)
                elif url.startswith('OGIMET_TAF_TRIGGER_'):
                    station_code = url.replace('OGIMET_TAF_TRIGGER_', '')
                    return name, fetch_ogimet_taf(station_code)
                elif 'aviationweather' in url:
                    return name, safe_get(url, timeout=6, retries=1)
                elif 'tgftp.nws.noaa.gov' in url:
                    # NOAA plain-text endpoints
                    resp = requests.get(url, timeout=4)
                    if resp.status_code == 200:
                        return name, resp.text
                    return name, None
                elif 'api.checkwx.com' in url:
                    api_key = os.environ.get('CHECKWX_API_KEY')
                    if not api_key: return name, None
                    resp = requests.get(url, headers={'X-API-Key': api_key}, timeout=5)
                    if resp.status_code == 200:
                        return name, resp.json()
                    return name, None
                elif 'avwx.rest' in url:
                    api_key = os.environ.get('AVWX_API_KEY')
                    if not api_key: return name, None
                    resp = requests.get(url, headers={'Authorization': f'Token {api_key}'}, timeout=5)
                    if resp.status_code == 200:
                        return name, resp.json()
                    return name, None
                else:
                    # D-ATIS requests
                    resp = requests.get(url, timeout=5)
                    if resp.status_code == 200:
                        return name, resp.json()
                    return name, None
            except Exception:
                return name, None

        results = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
            future_to_name = {executor.submit(fetch_url, name, url): name for name, url in urls.items()}
            for future in concurrent.futures.as_completed(future_to_name):
                name, data = future.result()
                results[name] = data

        metar_response = results.get('metar') or []
        taf_response = results.get('taf') or []
        
        metars_by_station = {}
        if isinstance(metar_response, list):
            for m in metar_response:
                metars_by_station[m.get('icaoId', 'Unknown')] = m
                
        tafs_by_station = {}
        if isinstance(taf_response, list):
            for t in taf_response:
                tafs_by_station[t.get('icaoId', 'Unknown')] = t
                
        station_info_response = results.get('station_info') or []
        station_info_by_icao = {}
        if isinstance(station_info_response, list):
            for info in station_info_response:
                station_info_by_icao[info.get('id', 'Unknown')] = info
        
        # Pre-fetch sun times in parallel for all stations that have coordinates
        # We pass lat/lon from METAR data; sun_cache handles repeat lookups
        sun_futures = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as sun_executor:
            for station in stations_list:
                lat_tmp, lon_tmp = None, None
                if station in KNOWN_STATIONS:
                    lat_tmp = KNOWN_STATIONS[station].get('lat')
                    lon_tmp = KNOWN_STATIONS[station].get('lon')
                m_tmp = metars_by_station.get(station)
                if m_tmp:
                    lat_tmp = m_tmp.get('lat', lat_tmp)
                    lon_tmp = m_tmp.get('lon', lon_tmp)
                elif station in station_info_by_icao:
                    lat_tmp = station_info_by_icao[station].get('lat', lat_tmp)
                    lon_tmp = station_info_by_icao[station].get('lon', lon_tmp)
                if lat_tmp is not None and lon_tmp is not None:
                    sun_futures[station] = sun_executor.submit(get_sun_times, lat_tmp, lon_tmp)
            # Collect results
            sun_results = {s: f.result() for s, f in sun_futures.items()}

        for station in stations_list:
            lat, lon = None, None
            header_info = ""
            name = "Unknown Station"
            
            # Apply hardcoded fallback if available
            if station in KNOWN_STATIONS:
                fallback = KNOWN_STATIONS[station]
                name = fallback.get('name', name)
                lat = fallback.get('lat')
                lon = fallback.get('lon')
            
            if station in metars_by_station:
                m = metars_by_station[station]
                name = m.get('name', name) # Keep fallback name if API returns empty
                lat = m.get('lat', lat)
                lon = m.get('lon', lon)
            elif station in station_info_by_icao:
                info = station_info_by_icao[station]
                name = info.get('site', name)
                lat = info.get('lat', lat)
                lon = info.get('lon', lon)
                
            if lat is not None and lon is not None:
                lat_dir = 'N' if lat >= 0 else 'S'
                lon_dir = 'E' if lon >= 0 else 'W'
                coords = f"{abs(lat):.2f}°{lat_dir} - {abs(lon):.2f}°{lon_dir}"
                sun = sun_results.get(station, {'sunrise': 'N/A', 'sunset': 'N/A'})
                header_info = f" | **{coords}** | **🌅 {sun['sunrise']} 🌇 {sun['sunset']}**"

            result_text += f"### 📍 **{station}** | {name}{header_info}\n\n"
            
            # --- METAR ---
            if station in metars_by_station:
                m = metars_by_station[station]
                raw_metar = m.get('rawOb', 'N/A')
                raw_metar = raw_metar.strip()
                while raw_metar.upper().startswith('METAR'):
                    raw_metar = raw_metar[5:].strip()
                obs_time_raw = m.get('obsTime', 'N/A')
                
                # Check if stale (older than 2 hours)
                is_stale = True # Default to stale if we can't parse it, so fallbacks trigger
                used_avwx = False
                elapsed_min = 0
                
                if isinstance(obs_time_raw, int):
                    obs_time = datetime.fromtimestamp(obs_time_raw, tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
                    obs_dt = datetime.fromtimestamp(obs_time_raw, tz=timezone.utc)
                    now_dt = datetime.now(timezone.utc)
                    elapsed_min = int((now_dt - obs_dt).total_seconds() / 60)
                    if (now_dt - obs_dt).total_seconds() <= 7200:
                        is_stale = False
                elif obs_time_raw != 'N/A':
                    obs_time = str(obs_time_raw).replace('T', ' ').replace('Z', ' UTC')
                    try:
                        obs_dt = datetime.fromisoformat(obs_time_raw.replace('Z', '+00:00'))
                        now_dt = datetime.now(timezone.utc)
                        elapsed_min = int((now_dt - obs_dt).total_seconds() / 60)
                        if (now_dt - obs_dt).total_seconds() <= 7200: # 2 hours
                            is_stale = False
                    except Exception:
                        pass
                else:
                    obs_time = 'N/A'
                
                # Use pre-fetched NOAA data if stale (already fetched in parallel above)
                if is_stale:
                    noaa_text = results.get(f'noaa_metar_{station}')
                    if noaa_text:
                        lines = noaa_text.strip().split('\n')
                        if len(lines) >= 2:
                            noaa_time_str = lines[0].strip()
                            try:
                                noaa_dt = datetime.strptime(noaa_time_str, "%Y/%m/%d %H:%M").replace(tzinfo=timezone.utc)
                                now_dt = datetime.now(timezone.utc)
                                if (now_dt - noaa_dt).total_seconds() <= 7200: # Max 2 hours for fallback
                                    is_stale = False
                            except Exception:
                                pass
                
                # Check AAI live weather (Indian stations only) first if stale - Tier 1A
                if is_stale and station.upper().startswith('V'):
                    aai_res = results.get(f'aai_metar_{station}')
                    if aai_res:
                        aai_metar, aai_dt = aai_res
                        if aai_metar and aai_dt:
                            now_dt = datetime.now(timezone.utc)
                            aai_elapsed = int((now_dt - aai_dt).total_seconds() / 60)
                            if elapsed_min == 0 or aai_elapsed < elapsed_min:
                                raw_metar = aai_metar
                                elapsed_min = aai_elapsed
                                obs_time = aai_dt.strftime('%Y-%m-%d %H:%M UTC')
                                is_stale = aai_elapsed > 120
                                used_avwx = False

                # Check AMSS regional nodes (Indian stations only) if STILL stale
                if is_stale and station.upper().startswith('V'):
                    amss_raw = results.get('amss_trigger')
                    if amss_raw:
                        amss_metar = parse_amss_metar(amss_raw, station)
                        if amss_metar:
                            obs_dt = parse_amss_time(amss_metar)
                            if obs_dt:
                                now_dt = datetime.now(timezone.utc)
                                a_elapsed = int((now_dt - obs_dt).total_seconds() / 60)
                                if elapsed_min == 0 or a_elapsed < elapsed_min:
                                    raw_metar = amss_metar
                                    elapsed_min = a_elapsed
                                    obs_time = obs_dt.strftime('%Y-%m-%d %H:%M UTC')
                                    is_stale = elapsed_min > 120
                                    used_avwx = False

                # Ogimet Tier 1B: last resort for Indian stations still stale
                if is_stale and station.upper().startswith('V'):
                    og_res = results.get(f'ogimet_metar_{station}')
                    if og_res:
                        og_metar, og_dt = og_res
                        if og_metar and og_dt:
                            now_dt = datetime.now(timezone.utc)
                            og_elapsed = int((now_dt - og_dt).total_seconds() / 60)
                            if elapsed_min == 0 or og_elapsed < elapsed_min:
                                raw_metar = og_metar
                                elapsed_min = og_elapsed
                                obs_time = og_dt.strftime('%Y-%m-%d %H:%M UTC')
                                is_stale = og_elapsed > 120
                                used_avwx = False
                                
                # Check CheckWX if STILL stale
                if is_stale:
                    cwx_data = results.get(f'checkwx_metar_{station}')
                    if cwx_data and cwx_data.get('results', 0) > 0:
                        try:
                            item = cwx_data['data'][0]
                            obs_dt = datetime.fromisoformat(item['observed'].replace('Z', '+00:00'))
                            now_dt = datetime.now(timezone.utc)
                            
                            # Only use CheckWX if it's newer than the current AviationWeather stale data
                            c_elapsed = int((now_dt - obs_dt).total_seconds() / 60)
                            if elapsed_min == 0 or c_elapsed < elapsed_min:
                                raw_metar = item['raw_text']
                                elapsed_min = c_elapsed
                                obs_time = obs_dt.strftime('%Y-%m-%d %H:%M UTC')
                                is_stale = elapsed_min > 120
                                
                                used_avwx = True # Reuse this flag so it doesn't overwrite
                                wdir = item.get('wind', {}).get('degrees', 'VRB')
                                wspd = item.get('wind', {}).get('speed_kts', 0)
                                vis = item.get('visibility', {}).get('meters', 'N/A')
                                temp = item.get('temperature', {}).get('celsius', 'N/A')
                                dewp = item.get('dewpoint', {}).get('celsius', 'N/A')
                                altim = item.get('barometer', {}).get('hpa', 'N/A')
                                altim = f"Q{altim}" if altim != 'N/A' else 'N/A'
                                vis_formatted = format_visibility(vis, station)
                                flt_cat = item.get('flight_category', 'N/A')
                                clouds = item.get('clouds', [])
                                wx = ""
                        except Exception:
                            pass
                                
                # Check AVWX if STILL stale
                if is_stale:
                    avwx_data = results.get(f'avwx_metar_{station}')
                    if avwx_data and 'raw' in avwx_data and 'time' in avwx_data:
                        try:
                            obs_dt = datetime.fromisoformat(avwx_data['time']['dt'].replace('Z', '+00:00'))
                            now_dt = datetime.now(timezone.utc)
                            
                            a_elapsed = int((now_dt - obs_dt).total_seconds() / 60)
                            if elapsed_min == 0 or a_elapsed < elapsed_min:
                                raw_metar = avwx_data['raw']
                                elapsed_min = a_elapsed
                                obs_time = obs_dt.strftime('%Y-%m-%d %H:%M UTC')
                                is_stale = elapsed_min > 120
                                
                                used_avwx = True
                                wdir = avwx_data.get('wind_direction', {}).get('value', 'VRB')
                                if wdir is None: wdir = 'VRB'
                                wspd = avwx_data.get('wind_speed', {}).get('value', 0)
                                vis = avwx_data.get('visibility', {}).get('value', 'N/A')
                                temp = avwx_data.get('temperature', {}).get('value', 'N/A')
                                dewp = avwx_data.get('dewpoint', {}).get('value', 'N/A')
                                altim = avwx_data.get('altimeter', {}).get('value', 'N/A')
                                altim = f"Q{altim}" if altim != 'N/A' else 'N/A'
                                vis_formatted = format_visibility(vis, station)
                                flt_cat = avwx_data.get('flight_rules', 'N/A')
                                clouds = []
                                wx = ""
                        except Exception:
                            pass
                
                if not used_avwx:
                    flt_cat = m.get('fltCat', 'N/A')
                    wdir = m.get('wdir', 'VRB' if m.get('wdir') == 0 else m.get('wdir', 'N/A'))
                    wspd = m.get('wspd', 'N/A')
                    vis = m.get('visib', 'N/A')
                    vis_formatted = format_visibility(vis, station)
                    clouds = m.get('clouds', [])
                    wx = m.get('wxString', '')
                    temp = m.get('temp', 'N/A')
                    dewp = m.get('dewp', 'N/A')
                    altim = m.get('altim', 'N/A')

                cloud_str = decode_clouds(clouds)
                wx_str = decode_wx(wx)
                
                result_text += f"✈️ **METAR** ({elapsed_min}m ago)\n```\n{raw_metar}\n\n```\n\n"
                if is_stale:
                    result_text += "⚠️ *Warning:* Decoded data below may be stale (source delayed).\n\n"
                if name != 'Unknown Station':
                    result_text += f"🏢 *Facility:* {name}\n"
                if obs_time != 'N/A':
                    result_text += f"🕒 *Date/Time:* {obs_time}\n"
                if flt_cat != 'N/A':
                    result_text += f"🚦 *Flight Rules:* {flt_cat}\n"
                
                # ICAO/FAA Logical Order
                result_text += f"💨 *Wind:* {wdir}° at {wspd} knots\n"
                result_text += f"👁️ *Visibility:* {vis_formatted}\n"
                if wx_str:
                    result_text += f"🌧️ *Present Weather:* {wx_str}\n"
                result_text += f"☁️ *Sky Condition:* {cloud_str}\n"
                result_text += f"🌡️ *Temperature:* {temp}°C | *Dewpoint:* {dewp}°C\n"
                result_text += f"🛩️ *Altimeter:* {altim}\n"
                result_text += "\n"
                # NOTE: do not cache per-station here. result_text is the running
                # accumulation of ALL requested stations, so storing it under a
                # single-station key would later return another station's data.
                # The full result is cached under cache_key at the end instead.
            else:
                # Use pre-fetched NOAA METAR as fallback (already fetched in parallel above)
                raw_metar = None
                obs_dt = None
                
                # Check AAI live weather first (Indian stations only) - Tier 1A
                if station.upper().startswith('V'):
                    aai_res = results.get(f'aai_metar_{station}')
                    if aai_res:
                        aai_metar, aai_dt = aai_res
                        if aai_metar and aai_dt:
                            obs_dt = aai_dt
                            raw_metar = aai_metar
                            
                # AMSS Delhi is Tier 1A for Indian stations
                if not raw_metar and station.upper().startswith('V'):
                    amss_raw = results.get('amss_trigger')
                    if amss_raw:
                        amss_metar = parse_amss_metar(amss_raw, station)
                        if amss_metar:
                            amss_obs = parse_amss_time(amss_metar)
                            if amss_obs:
                                obs_dt = amss_obs
                                raw_metar = amss_metar
                
                # Check NOAA if AMSS failed
                if not raw_metar:
                    noaa_text = results.get(f'noaa_metar_{station}')
                    if noaa_text:
                        lines = noaa_text.strip().split('\n')
                        if len(lines) >= 2:
                            noaa_time_str = lines[0].strip()
                            try:
                                obs_dt = datetime.strptime(noaa_time_str, "%Y/%m/%d %H:%M").replace(tzinfo=timezone.utc)
                                raw_metar = lines[1]
                            except Exception:
                                pass
                
                # Check CheckWX if NOAA/AMSS failed
                if not raw_metar:
                    cwx_data = results.get(f'checkwx_metar_{station}')
                    if cwx_data and cwx_data.get('results', 0) > 0:
                        try:
                            item = cwx_data['data'][0]
                            obs_dt = datetime.fromisoformat(item['observed'].replace('Z', '+00:00'))
                            raw_metar = item['raw_text']
                        except Exception:
                            pass
                
                # Check AVWX if CheckWX failed
                if not raw_metar:
                    avwx_data = results.get(f'avwx_metar_{station}')
                    if avwx_data and 'raw' in avwx_data and 'time' in avwx_data:
                        try:
                            obs_dt = datetime.fromisoformat(avwx_data['time']['dt'].replace('Z', '+00:00'))
                            raw_metar = avwx_data['raw']
                        except Exception:
                            pass

                # Ogimet Tier 1B: last resort for Indian stations
                if not raw_metar and station.upper().startswith('V'):
                    og_res = results.get(f'ogimet_metar_{station}')
                    if og_res:
                        og_metar, og_dt = og_res
                        if og_metar:
                            raw_metar = og_metar
                            obs_dt = og_dt
                    
                if raw_metar:
                    elapsed_str = ""
                    if obs_dt:
                        now_dt = datetime.now(timezone.utc)
                        mins = int((now_dt - obs_dt).total_seconds() / 60)
                        
                        # Format nicely for very old data
                        if mins > 2880: # More than 2 days
                            days = mins // 1440
                            elapsed_str = f" ({days} days ago)"
                        elif mins > 120: # More than 2 hours
                            hours = mins // 60
                            elapsed_str = f" ({hours}h ago)"
                        else:
                            elapsed_str = f" ({mins}m ago)"
                            
                    result_text += f"✈️ **METAR**{elapsed_str}\n```\n{raw_metar}\n\n```\n\n"
                    if obs_dt and mins > 120:
                        result_text += "⚠️ *Warning:* Data is highly stale (source delayed).\n\n"
                else:
                    result_text += f"✈️ *METAR*\n_No recent METAR data available._\n\n"
                
            # --- TAF ---
            if station in tafs_by_station:
                t = tafs_by_station[station]
                raw_taf = t.get('rawTAF', 'N/A')
                raw_taf = raw_taf.strip()
                while raw_taf.upper().startswith('TAF'):
                    raw_taf = raw_taf[3:].strip()
                issue_time = t.get('issueTime', 'N/A')
                issue_time_formatted = str(issue_time).replace('T', ' ').replace('Z', ' UTC')
                result_text += f"📅 **TAF** (Issued: {issue_time_formatted})\n```\n{raw_taf}\n```\n\n"
                result_text += "*Decoded:*\n"
                
                for fcst in t.get('fcsts', []):
                    change = fcst.get('fcstChange') or 'INITIAL'
                    wdir = fcst.get('wdir', 'VRB')
                    wspd = fcst.get('wspd', 0)
                    vis = fcst.get('visib', 'N/A')
                    vis_formatted = format_visibility(vis, station)
                    clouds = fcst.get('clouds', [])
                    wx = fcst.get('wxString', '')
                    
                    cloud_str = decode_clouds(clouds)
                    wx_str = decode_wx(wx)
                    
                    result_text += f"  🔹 **{change}**: Wind {wdir}° at {wspd}kt, Vis {vis_formatted}, {cloud_str}"
                    if wx_str:
                        result_text += f", Wx: {wx_str}"
                    result_text += "\n"
                result_text += "\n"
            else:
                # Use pre-fetched NOAA TAF as fallback (already fetched in parallel above)
                raw_taf = None
                issue_time_formatted = "N/A"
                noaa_text = results.get(f'noaa_taf_{station}')
                if noaa_text:
                    lines = noaa_text.strip().split('\n')
                    if len(lines) >= 2:
                        noaa_time_str = lines[0].strip()
                        try:
                            noaa_dt = datetime.strptime(noaa_time_str, "%Y/%m/%d %H:%M").replace(tzinfo=timezone.utc)
                            now_dt = datetime.now(timezone.utc)
                            if (now_dt - noaa_dt).total_seconds() <= 3600 * 30: # Max 30 hours for TAF
                                raw_taf = " ".join(lines[1:])
                                raw_taf = raw_taf.strip()
                                while raw_taf.upper().startswith('TAF'):
                                    raw_taf = raw_taf[3:].strip()
                                issue_time_formatted = noaa_time_str.replace('/', '-') + ":00 UTC"
                        except Exception:
                            pass
                
                # Try AAI TAF (Tier 1A - Domestic Stream)
                if not raw_taf and station.upper().startswith('V'):
                    aai_taf_res = results.get(f'aai_taf_{station}')
                    if aai_taf_res:
                        a_taf, a_dt = aai_taf_res
                        if a_taf:
                            raw_taf = a_taf.strip()
                            while raw_taf.upper().startswith('TAF'):
                                raw_taf = raw_taf[3:].strip()
                            if a_dt:
                                issue_time_formatted = a_dt.strftime('%Y-%m-%d %H:%M UTC')
                            else:
                                issue_time_formatted = "N/A"

                # Try Ogimet TAF fallback (Tier 1B)
                if not raw_taf and station.upper().startswith('V'):
                    og_taf_res = results.get(f'ogimet_taf_{station}')
                    if og_taf_res:
                        o_taf, o_dt = og_taf_res
                        if o_taf:
                            raw_taf = o_taf.strip()
                            while raw_taf.upper().startswith('TAF'):
                                raw_taf = raw_taf[3:].strip()
                            if o_dt:
                                issue_time_formatted = o_dt.strftime('%Y-%m-%d %H:%M UTC')
                            else:
                                issue_time_formatted = "N/A"
                                
                # Try CheckWX TAF Fallback
                if not raw_taf:
                    cwx_data = results.get(f'checkwx_taf_{station}')
                    if cwx_data and cwx_data.get('results', 0) > 0:
                        try:
                            item = cwx_data['data'][0]
                            raw_taf = item['raw_text'].strip()
                            while raw_taf.upper().startswith('TAF'):
                                raw_taf = raw_taf[3:].strip()
                            obs_dt = datetime.fromisoformat(item['timestamp']['issued'].replace('Z', '+00:00'))
                            issue_time_formatted = obs_dt.strftime('%Y-%m-%d %H:%M UTC')
                        except Exception:
                            pass
                            
                # Try AVWX TAF Fallback
                if not raw_taf:
                    avwx_data = results.get(f'avwx_taf_{station}')
                    if avwx_data and 'raw' in avwx_data and 'time' in avwx_data:
                        try:
                            raw_taf = avwx_data['raw'].strip()
                            while raw_taf.upper().startswith('TAF'):
                                raw_taf = raw_taf[3:].strip()
                            obs_dt = datetime.fromisoformat(avwx_data['time']['dt'].replace('Z', '+00:00'))
                            issue_time_formatted = obs_dt.strftime('%Y-%m-%d %H:%M UTC')
                        except Exception:
                            pass
                
                if raw_taf:
                    result_text += f"📅 **TAF** (Issued: {issue_time_formatted})\n```\n{raw_taf}\n```\n\n"
                else:
                    result_text += f"📅 **TAF**\n_No recent TAF forecast available._\n\n"
                
            # --- ATIS ---
            atis_data = results.get(f'atis_{station}')
            if atis_data:
                try:
                    if isinstance(atis_data, list) and atis_data:
                        for datis in atis_data:
                            atis_type = datis.get('type', 'combined').title()
                            atis_text = datis.get('datis', 'N/A')
                            result_text += f"📻 *D-ATIS ({atis_type})*\n_{atis_text}_\n\n"
                    elif isinstance(atis_data, dict) and 'error' not in atis_data:
                        atis_text = atis_data.get('datis', 'N/A')
                        result_text += f"📻 *D-ATIS*\n_{atis_text}_\n\n"
                except Exception:
                    pass
            else:
                result_text += f"📻 *D-ATIS*\n_Not available or could not connect._\n\n"
                
        weather_cache[cache_key] = (result_text, current_time)
        return result_text
        
    except Exception as e:
        return f"Error fetching weather data: {str(e)}"

def parse_raw_metar_to_dict(metar: str) -> dict:
    """Helper to parse a raw METAR string into a decoded dictionary block for visual grid display."""
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
    
    # 1. Wind: e.g. 22010KT or VRB02KT
    wind_match = re.search(r'\b([0-9]{3}|VRB)([0-9]{2})(?:G([0-9]{2}))?KT\b', metar)
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
        
    # 2. Visibility: e.g. 6000 or 9999 or 10SM
    vis_match = re.search(r'\b([0-9]{4})\b', metar)
    if vis_match:
        vis_val = int(vis_match.group(1))
        if vis_val == 9999:
            model["visibility"] = "10+ Kms"
        else:
            model["visibility"] = f"{vis_val}m"
    else:
        vis_sm_match = re.search(r'\b([0-9]+)SM\b', metar)
        if vis_sm_match:
            model["visibility"] = f"{vis_sm_match.group(1)} SM"
            
    # 3. Temperature/Dewpoint: e.g. 32/25 or M02/M05
    temp_match = re.search(r'\b(M?[0-9]{2})/(M?[0-9]{2})\b', metar)
    if temp_match:
        def convert_temp(t_str):
            val = t_str.replace('M', '-')
            return f"{int(val)}°C"
        model["temp"] = convert_temp(temp_match.group(1))
        model["dew"] = convert_temp(temp_match.group(2))
        
    # 4. Altimeter: e.g. Q1009 or A2992
    alt_match = re.search(r'\bQ([0-9]{4})\b', metar)
    if alt_match:
        model["altimeter"] = f"Q{alt_match.group(1)} hPa"
    else:
        alt_a_match = re.search(r'\bA([0-9]{4})\b', metar)
        if alt_a_match:
            try:
                inhg = float(alt_a_match.group(1)) / 100.0
                hpa = int(round(inhg * 33.8639))
                model["altimeter"] = f"Q{hpa} hPa"
            except Exception:
                pass
                
    # 5. Clouds
    cloud_groups = re.findall(r'\b(FEW|SCT|BKN|OVC|CLR|SKC|NSC|CAVOK)([0-9]{3})?\b', metar)
    if cloud_groups:
        cloud_strs = []
        for cvr, base in cloud_groups:
            if cvr in ["CLR", "SKC", "NSC", "CAVOK"]:
                continue
            base_str = base if base else ""
            cloud_strs.append(f"{cvr}{base_str}")
        if cloud_strs:
            model["clouds"] = " ".join(cloud_strs)
            
    if "CAVOK" in metar:
        model["weather"] = "CAVOK"
        model["clouds"] = "CAVOK"
        model["visibility"] = "10+ Kms"
            
    # 6. Weather
    wx_matches = re.finditer(r'\b(-|\+|VC)?(TS|SH|DZ|RA|SN|SG|IC|PL|GR|GS|UP|BR|FG|FU|VA|DU|SA|HZ|PY|PO|SQ|FC|SS|DS)+\b', metar)
    wx_list = [m.group(0) for m in wx_matches]
    if wx_list:
        model["weather"] = " ".join(wx_list)
        
    return model


def get_station_details(station: str) -> dict:
    from astral.sun import sun
    from astral import LocationInfo
    from datetime import datetime, timezone
    
    raw_data = _get_station_details_raw(station)
    
    lat = None
    lon = None
    name = raw_data.get('name', 'Unknown Station')
    
    # 1. Try to get lat/lon from NOAA
    try:
        info_url = f"https://aviationweather.gov/api/data/stationinfo?ids={station}&format=json"
        from weather_engine import safe_get
        info_data = safe_get(info_url, timeout=3, retries=1)
        if info_data and isinstance(info_data, list) and len(info_data) > 0:
            info = info_data[0]
            lat = info.get('lat')
            lon = info.get('lon')
            if name == 'Unknown Station' or not name:
                name = info.get('site', 'Unknown Station')
            iata = info.get('iataId')
            if iata and str(iata).upper() not in ["", "0", "NONE"]:
                name = f"{name} ({str(iata).upper()})"
    except Exception:
        pass
        
    # 2. Try KNOWN_STATIONS fallback
    if station in KNOWN_STATIONS:
        fallback = KNOWN_STATIONS[station]
        name = fallback.get('name', name)
        if lat is None: lat = fallback.get('lat')
        if lon is None: lon = fallback.get('lon')
        
    # 3. Compute coords_str and sun_str
    coords_str = ""
    sun_str = ""
    if lat is not None and lon is not None:
        lat_dir = 'N' if lat >= 0 else 'S'
        lon_dir = 'E' if lon >= 0 else 'W'
        lat_deg = int(abs(lat))
        lat_min = (abs(lat) - lat_deg) * 60
        lon_deg = int(abs(lon))
        lon_min = (abs(lon) - lon_deg) * 60
        coords_str = f"{lat_dir}{lat_deg:02d}{lat_min:04.1f} {lon_dir}{lon_deg:03d}{lon_min:04.1f}"
        
        try:
            now = datetime.now(timezone.utc)
            loc = LocationInfo(latitude=lat, longitude=lon)
            s = sun(loc.observer, date=now.date())
            sunrise = s['sunrise'].strftime('%H:%M UTC')
            sunset = s['sunset'].strftime('%H:%M UTC')
            sun_str = f"🌅 {sunrise} 🌇 {sunset}"
        except Exception:
            pass
            
    raw_data['name'] = name
    raw_data['coords'] = coords_str
    raw_data['sun'] = sun_str
    
    return raw_data

def _get_station_details_raw(station: str) -> dict:
    """Fetches detailed structural JSON for the visual station grid and history."""
    station = station.strip().upper()
    try:
        data = []
        try:
            url = f"https://aviationweather.gov/api/data/metar?ids={station}&format=json&hours=4"
            data = safe_get(url, timeout=5, retries=1)
        except Exception:
            pass
            
        info_data = []
        try:
            info_url = f"https://aviationweather.gov/api/data/stationinfo?ids={station}&format=json"
            info_data = safe_get(info_url, timeout=5, retries=1)
        except Exception:
            pass
        
        station_name = "Unknown Station"
        if info_data and isinstance(info_data, list) and len(info_data) > 0:
            station_name = info_data[0].get('site', 'Unknown Station')
            
        raw_taf = ""
        try:
            taf_url = f"https://tgftp.nws.noaa.gov/data/forecasts/taf/stations/{station}.TXT"
            taf_text = safe_get(taf_url, timeout=3, retries=1)
            if taf_text and len(taf_text.split('\n')) >= 2:
                raw_taf = " ".join(taf_text.strip().split('\n')[1:])
                while raw_taf.upper().startswith('TAF'):
                    raw_taf = raw_taf[3:].strip()
        except Exception:
            pass
            
        if not raw_taf:
            cwx_api_key = os.environ.get('CHECKWX_API_KEY')
            if cwx_api_key:
                try:
                    cwx_taf_url = f"https://api.checkwx.com/taf/{station}/decoded"
                    cwx_taf_resp = requests.get(cwx_taf_url, headers={'X-API-Key': cwx_api_key}, timeout=3)
                    if cwx_taf_resp.status_code == 200:
                        c_data = cwx_taf_resp.json()
                        if c_data.get('results', 0) > 0:
                            raw_taf = c_data['data'][0]['raw_text']
                            while raw_taf.upper().startswith('TAF'):
                                raw_taf = raw_taf[3:].strip()
                except Exception:
                    pass
                    
        if not raw_taf:
            avwx_api_key = os.environ.get('AVWX_API_KEY')
            if avwx_api_key:
                try:
                    avwx_taf_url = f"https://avwx.rest/api/taf/{station}"
                    avwx_taf_resp = requests.get(avwx_taf_url, headers={'Authorization': f'Token {avwx_api_key}'}, timeout=3)
                    if avwx_taf_resp.status_code == 200:
                        raw_taf = avwx_taf_resp.json().get('raw', '')
                        while raw_taf.upper().startswith('TAF'):
                            raw_taf = raw_taf[3:].strip()
                except Exception:
                    pass
            
        # Determine if AviationWeather data is empty or stale (older than 2 hours)
        is_stale = True
        if data:
            data.sort(key=lambda x: x.get('obsTime', 0), reverse=True)
            latest = data[0]
            obs_time_raw = latest.get('obsTime')
            if isinstance(obs_time_raw, int):
                obs_dt = datetime.fromtimestamp(obs_time_raw, tz=timezone.utc)
                now_dt = datetime.now(timezone.utc)
                if (now_dt - obs_dt).total_seconds() <= 7200:
                    is_stale = False
            elif obs_time_raw:
                try:
                    obs_dt = datetime.fromisoformat(str(obs_time_raw).replace('Z', '+00:00'))
                    now_dt = datetime.now(timezone.utc)
                    if (now_dt - obs_dt).total_seconds() <= 7200:
                        is_stale = False
                except Exception:
                    pass

        # Indian military/civil airfields fallback flow
        if is_stale and station.startswith('V'):
            # 1. Try AAI Live Portal (Tier 1A - Domestic Stream)
            try:
                aai_metar, aai_dt = fetch_live_aai_weather(station)
                if aai_metar and aai_dt:
                    model_data = parse_raw_metar_to_dict(aai_metar)
                    return {
                        "icao": station,
                        "name": station_name,
                        "time": aai_dt.strftime('%Y-%m-%d %H:%M UTC'),
                        "model": model_data,
                        "history": [aai_metar],
                        "raw_taf": raw_taf
                    }
            except Exception:
                pass

            # 2. Try AMSS Regional Nodes
            try:
                amss_raw = fetch_all_imd_regional_nodes()
                if amss_raw:
                    amss_metar = parse_amss_metar(amss_raw, station)
                    if amss_metar:
                        obs_dt = parse_amss_time(amss_metar)
                        if obs_dt:
                            model_data = parse_raw_metar_to_dict(amss_metar)
                            return {
                                "icao": station,
                                "name": station_name,
                                "time": obs_dt.strftime('%Y-%m-%d %H:%M UTC'),
                                "model": model_data,
                                "history": [amss_metar],
                                "raw_taf": raw_taf
                            }
            except Exception:
                pass

            # 3. Try Ogimet METAR Fallback (Handles VOGO)
            try:
                og_metar, og_dt = fetch_ogimet_metar(station)
                if og_metar and og_dt:
                    model_data = parse_raw_metar_to_dict(og_metar)
                    return {
                        "icao": station,
                        "name": station_name,
                        "time": og_dt.strftime('%Y-%m-%d %H:%M UTC'),
                        "model": model_data,
                        "history": [og_metar],
                        "raw_taf": raw_taf
                    }
            except Exception:
                pass

            # 3.5. Try AAI TAF Fallback (Tier 1A - Domestic Stream TAF)
            try:
                aai_taf, aai_dt = fetch_aai_taf(station)
                if aai_taf and aai_dt:
                    # Clean validity period (e.g. 2218/2400) to prevent visibility/wind parsing corruption
                    cleaned_taf = re.sub(r'\b[0-9]{4}/[0-9]{4}\b', '', aai_taf).strip()
                    model_data = parse_raw_metar_to_dict(cleaned_taf)
                    return {
                        "icao": station,
                        "name": station_name,
                        "time": aai_dt.strftime('%Y-%m-%d %H:%M UTC'),
                        "model": model_data,
                        "history": [aai_taf],
                        "raw_taf": raw_taf
                    }
            except Exception:
                pass

            # 4. Try Ogimet TAF Fallback (Handles VAPO and stations with only TAFs)
            try:
                og_taf, og_dt = fetch_ogimet_taf(station)
                if og_taf and og_dt:
                    # Clean validity period (e.g. 2218/2400) to prevent visibility/wind parsing corruption
                    cleaned_taf = re.sub(r'\b[0-9]{4}/[0-9]{4}\b', '', og_taf).strip()
                    model_data = parse_raw_metar_to_dict(cleaned_taf)
                    return {
                        "icao": station,
                        "name": station_name,
                        "time": og_dt.strftime('%Y-%m-%d %H:%M UTC'),
                        "model": model_data,
                        "history": [og_taf],
                        "raw_taf": raw_taf
                    }
            except Exception:
                pass
                
        if not data:
            cwx_api_key = os.environ.get('CHECKWX_API_KEY')
            if cwx_api_key:
                try:
                    cwx_url = f"https://api.checkwx.com/metar/{station}/decoded"
                    cwx_resp = requests.get(cwx_url, headers={'X-API-Key': cwx_api_key}, timeout=5)
                    if cwx_resp.status_code == 200:
                        c_data = cwx_resp.json()
                        if c_data.get('results', 0) > 0:
                            item = c_data['data'][0]
                            obs_dt = datetime.fromisoformat(item['observed'].replace('Z', '+00:00'))
                            
                            t_val = item.get('temperature', {}).get('celsius', 'N/A')
                            d_val = item.get('dewpoint', {}).get('celsius', 'N/A')
                            wdir = item.get('wind', {}).get('degrees', 0)
                            wspd = item.get('wind', {}).get('speed_kts', 0)
                            
                            vis_val = item.get('visibility', {}).get('meters', 'N/A')
                            if vis_val != 'N/A':
                                try:
                                    vis_m = float(vis_val)
                                    vis_str = "10+ Kms" if vis_m >= 9999 else f"{int(vis_m)}m"
                                except Exception:
                                    vis_str = str(vis_val)
                            else:
                                vis_str = "N/A"

                            qnh_val = item.get('barometer', {}).get('hpa', 'N/A')
                            qnh_str = f"Q{qnh_val} hPa" if qnh_val != 'N/A' else "N/A"

                            cloud_full = "CAVOK"
                            if item.get('clouds'):
                                cloud_full = " ".join([f"{c.get('code','')} {c.get('base_feet_agl','')}".strip() for c in item['clouds']])

                            cwx_model = parse_raw_metar_to_dict(item.get('raw_text', ''))
                            wx_str = cwx_model.get("weather", "NONE")

                            return {
                                "icao": station,
                                "name": station_name,
                                "time": obs_dt.strftime('%Y-%m-%d %H:%M UTC'),
                                "model": {
                                    "temp": f"{t_val}°C" if t_val != 'N/A' else "N/A",
                                    "dew": f"{d_val}°C" if d_val != 'N/A' else "N/A",
                                    "windDir": wdir,
                                    "windSpeed": wspd,
                                    "windStr": f"{wdir}° / {wspd} KT",
                                    "visibility": vis_str,
                                    "clouds": cloud_full,
                                    "weather": wx_str,
                                    "altimeter": qnh_str
                                },
                                "history": [item.get('raw_text', '')],
                                "raw_taf": raw_taf
                            }
                except Exception:
                    pass
                    
            avwx_api_key = os.environ.get('AVWX_API_KEY')
            if avwx_api_key:
                try:
                    avwx_url = f"https://avwx.rest/api/metar/{station}"
                    avwx_resp = requests.get(avwx_url, headers={'Authorization': f'Token {avwx_api_key}'}, timeout=5)
                    if avwx_resp.status_code == 200:
                        a_data = avwx_resp.json()
                        obs_dt = datetime.fromisoformat(a_data['time']['dt'].replace('Z', '+00:00'))
                        
                        t_val = a_data.get('temperature', {}).get('value', 'N/A')
                        if t_val is None: t_val = 'N/A'
                        d_val = a_data.get('dewpoint', {}).get('value', 'N/A')
                        if d_val is None: d_val = 'N/A'
                        wdir = (a_data.get('wind_direction') or {}).get('value')
                        wspd = (a_data.get('wind_speed') or {}).get('value', 0)
                        if wdir is None: wdir = 0
                        
                        vis_val = (a_data.get('visibility') or {}).get('value', 'N/A')
                        if vis_val != 'N/A':
                            try:
                                vis_m = float(vis_val)
                                vis_str = "10+ Kms" if vis_m >= 9999 else f"{int(vis_m)}m"
                            except Exception:
                                vis_str = str(vis_val)
                        else:
                            vis_str = "N/A"
                                
                        qnh_val = (a_data.get('altimeter') or {}).get('value', 'N/A')
                        qnh_str = f"Q{qnh_val} hPa" if qnh_val != 'N/A' else "N/A"
                        
                        cloud_full = "CAVOK"
                        if a_data.get('clouds'):
                            cloud_full = " ".join([f"{c['type']}{str(c['altitude']).zfill(3)}" for c in a_data['clouds'] if 'altitude' in c])
                            
                        avwx_model = parse_raw_metar_to_dict(a_data.get('raw', ''))
                        wx_str = avwx_model.get("weather", "NONE")
                            
                        return {
                            "icao": station,
                            "name": station_name,
                            "time": obs_dt.strftime('%Y-%m-%d %H:%M UTC'),
                            "model": {
                                "temp": f"{t_val}°C" if t_val != 'N/A' else "N/A",
                                "dew": f"{d_val}°C" if d_val != 'N/A' else "N/A",
                                "windDir": wdir,
                                "windSpeed": wspd,
                                "windStr": f"{wdir}° / {wspd} KT",
                                "visibility": vis_str,
                                "clouds": cloud_full,
                                "weather": wx_str,
                                "altimeter": qnh_str
                            },
                            "history": [a_data.get('raw', '')],
                            "raw_taf": raw_taf
                        }
                except Exception as e:
                    import traceback
                    traceback.print_exc()
            
            return {
                "icao": station,
                "name": station_name,
                "time": "N/A",
                "model": {
                    "temp": "N/A",
                    "dew": "N/A",
                    "windDir": 0,
                    "windSpeed": 0,
                    "windStr": "N/A",
                    "visibility": "N/A",
                    "clouds": "N/A",
                    "weather": "N/A",
                    "altimeter": "N/A"
                },
                "history": [],
                "raw_taf": raw_taf
            }
        
        # Sort by observation time descending
        data.sort(key=lambda x: x.get('obsTime', 0), reverse=True)
        
        latest = data[0]
        
        # Extract fields
        temp = latest.get('temp', 'N/A')
        dew = latest.get('dewp', 'N/A')
        wdir = latest.get('wdir', 'VRB')
        wspd = latest.get('wspd', 0)
        wgst = latest.get('wgst')
        
        wind_str = f"{wdir}° / {wspd}"
        if wgst:
            wind_str += f"G{wgst}"
        wind_str += " KT"
        
        vis = latest.get('visib', 'N/A')
        if vis != 'N/A':
            vis_str_val = str(vis).strip().upper()
            if vis_str_val == '9999':
                vis_str = "10+ Kms"
            elif vis_str_val == '6+':
                vis_str = "10+ Kms"
            else:
                try:
                    vis_num = float(vis)
                    # Convert SM to meters if it looks like SM (API returns < 20 for SM)
                    if vis_num < 40:
                        vis_m = vis_num * 1609.34
                    else:
                        vis_m = vis_num
                        
                    if vis_m > 5000:
                        vis_km = vis_m / 1000.0
                        if vis_km >= 9.9:
                            vis_str = "10+ Kms"
                        else:
                            vis_str = f"{int(round(vis_km))} Kms"
                    else:
                        vis_rounded = int(round(vis_m / 50.0) * 50)
                        vis_str = f"{vis_rounded}m"
                except ValueError:
                    if '+' in vis_str_val:
                        vis_str = f"{vis_str_val.replace('SM', '').strip()} Kms"
                    else:
                        vis_str = vis_str_val
        else:
            vis_str = "N/A"
            
        altim = latest.get('altim', 'N/A')
        altim_str = "N/A"
        if altim != 'N/A':
            if altim > 150:
                altim_str = f"Q{int(altim)} hPa"
            else:
                # Convert inHg to hPa
                hpa = int(round(altim * 33.8639))
                altim_str = f"Q{hpa} hPa"
                
        wx = latest.get('wxString', '')
        if not wx: wx = 'NONE'
        
        clouds_list = latest.get('clouds', [])
        cloud_strs = []
        for c in clouds_list:
            cvr = c.get('cover', '')
            base = c.get('base', '')
            typ = c.get('type', '')
            if base and str(base).isdigit():
                base_str = f"{int(base):03d}"
            else:
                base_str = str(base)
            cloud_strs.append(f"{cvr}{base_str}{typ}")
        
        cloud_full = " ".join(cloud_strs) if cloud_strs else "CLEAR"
        if cloud_full.strip() == "": cloud_full = "CLEAR"
        
        raw_ob = latest.get('rawOb', '')
        if 'CAVOK' in raw_ob:
            cloud_full = 'CAVOK'
            wx = 'CAVOK'
            vis_str = '10+ Kms'
        
        # History (up to 3)
        history = [m.get('rawOb', '').replace('\n', ' ').strip() for m in data[:3]]
        
        obs_time = latest.get('obsTime', 0)
        if isinstance(obs_time, int):
            time_str = datetime.fromtimestamp(obs_time, tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
        else:
            time_str = str(obs_time).replace('T', ' ').replace('Z', ' UTC')

        return {
            "icao": station,
            "name": latest.get('name', 'Unknown Station'),
            "time": time_str,
            "model": {
                "temp": f"{temp}°C" if temp != 'N/A' else "N/A",
                "dew": f"{dew}°C" if dew != 'N/A' else "N/A",
                "windDir": wdir if wdir != 'VRB' else 0, # for rotation
                "windSpeed": wspd,
                "windStr": wind_str,
                "visibility": vis_str,
                "clouds": cloud_full,
                "weather": wx,
                "altimeter": altim_str
            },
            "history": history,
            "raw_taf": raw_taf
        }
    except Exception as e:
        import traceback
        return {"error": str(e), "traceback": traceback.format_exc()}
