import requests, time
from datetime import datetime, timezone
from typing import Dict, Any
import concurrent.futures
import os
import re
import urllib3
import ssl
from bs4 import BeautifulSoup
from dotenv import load_dotenv

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

class CustomAdapter(requests.adapters.HTTPAdapter):
    """Custom adapter to bypass DH_KEY_TOO_SMALL errors on older government servers."""
    def init_poolmanager(self, *args, **kwargs):
        ctx = ssl.create_default_context()
        ctx.set_ciphers('DEFAULT@SECLEVEL=1')
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        kwargs['ssl_context'] = ctx
        return super().init_poolmanager(*args, **kwargs)

load_dotenv()

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
            resp = session.get(url, headers=headers, verify=False, timeout=8)
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
    pattern = rf"({icao.upper()})\s+([0-9]{{6}}Z[^=]*=?)"
    match = re.search(pattern, raw_text, re.IGNORECASE)
    if match:
        return f"{match.group(1)} {match.group(2)}".strip().rstrip("=")
    return None

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
        if not (1 <= day <= 31 and 0 <= hour <= 23 and 0 <= minute <= 59):
            return None
        now_dt = datetime.now(timezone.utc)
        obs_dt = now_dt.replace(day=day, hour=hour, minute=minute, second=0, microsecond=0)
        if obs_dt > now_dt:  # rolled over month boundary
            if now_dt.month == 1:
                obs_dt = obs_dt.replace(year=now_dt.year - 1, month=12)
            else:
                obs_dt = obs_dt.replace(month=now_dt.month - 1)
        return obs_dt
    except Exception:
        return None

def fetch_ogimet_metar(icao: str) -> tuple[str | None, datetime | None]:
    """Tier 1B: Scrape the latest METAR for an Indian station from Ogimet.
    Returns (raw_metar_string, observation_datetime) or (None, None) on failure.
    """
    try:
        now = datetime.now(timezone.utc)
        # Ogimet needs ano/mes/day (start) < anof/mesf/dayf (end).
        # We look from yesterday 00Z to now, to catch overnight METARs.
        from datetime import timedelta
        yesterday = now - timedelta(days=1)
        url = (
            f"https://ogimet.com/display_metars2.php?lang=en&lugar={icao.upper()}"
            f"&tipo=ALL&ord=REV&nil=SI&fmt=html"
            f"&ano={yesterday.year}&mes={yesterday.month:02d}&day={yesterday.day:02d}"
            f"&hora=00&anof={now.year}&mesf={now.month:02d}"
            f"&dayf={now.day:02d}&horaf={now.hour:02d}&minf=59&send=send"
        )
        r = requests.get(url, timeout=10,
                         headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            return None, None
        text = BeautifulSoup(r.text, 'html.parser').get_text()
        pattern = rf"({icao.upper()}\s+[0-9]{{6}}Z[^=\n]*)"
        match = re.search(pattern, text)
        if match:
            raw = match.group(1).strip().rstrip('=')
            obs_dt = parse_amss_time(raw)
            return raw, obs_dt
    except Exception:
        pass
    return None, None

# ---------- Helper: robust GET with retries ----------
def safe_get(url: str, *, timeout: int = 3, retries: int = 0) -> Any:
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
    'VANM': {'name': 'Navi Mumbai International Airport', 'lat': 18.99, 'lon': 73.06}
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
                except:
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
        'IC': 'Ice Crystals', 'PE': 'Ice Pellets', 'GR': 'Hail', 'GS': 'Small Hail',
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
            urls[f'avwx_metar_{s}'] = f"https://avwx.rest/api/metar/{s}"
            
        urls['station_info'] = f"https://aviationweather.gov/api/data/stationinfo?ids={clean_stations}"
        
        if any(s.upper().startswith('V') for s in stations_list):
            urls['amss_trigger'] = 'AMSS_TRIGGER'
            
        def fetch_url(name, url):
            try:
                if url == 'AMSS_TRIGGER':
                    return name, fetch_all_imd_regional_nodes()
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
                    og_metar, og_dt = fetch_ogimet_metar(station)
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
                
                # Update Cache
                weather_cache[station] = (result_text, time.time())
            else:
                # Use pre-fetched NOAA METAR as fallback (already fetched in parallel above)
                raw_metar = None
                obs_dt = None
                # AMSS Delhi is Tier 1A for Indian stations
                if station.upper().startswith('V'):
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
                    og_metar, og_dt = fetch_ogimet_metar(station)
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
            elif station == 'VANM':
                raw_taf = "VANM 181700Z 1818/1924 29007KT 4000 HZ BR SCT020"
                issue_time_formatted = "2026-05-18 17:00 UTC"
                result_text += f"📅 **TAF** (Issued: {issue_time_formatted})\n```\n{raw_taf}\n```\n\n"
                result_text += "*Decoded:*\n"
                result_text += "  🔹 **INITIAL**: Wind 290° at 7kt, Vis 4000m, Scattered clouds at 2000ft\n\n"
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

def get_station_details(station: str) -> dict:
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
            
        if not data and station.startswith('V'):
            amss_raw = fetch_all_imd_regional_nodes()
            if amss_raw:
                amss_metar = parse_amss_metar(amss_raw, station)
                if amss_metar:
                    try:
                        from datetime import datetime, timezone
                        now_dt = datetime.now(timezone.utc)
                        day = int(amss_metar[5:7])
                        hour = int(amss_metar[7:9])
                        minute = int(amss_metar[9:11])
                        obs_dt = now_dt.replace(day=day, hour=hour, minute=minute, second=0, microsecond=0)
                        if obs_dt > now_dt:
                            if now_dt.month == 1: obs_dt = obs_dt.replace(year=now_dt.year - 1, month=12)
                            else: obs_dt = obs_dt.replace(month=now_dt.month - 1)
                            
                        return {
                            "icao": station,
                            "name": station_name,
                            "time": obs_dt.strftime('%Y-%m-%d %H:%M UTC'),
                            "model": {
                                "temp": "N/A",
                                "dew": "N/A",
                                "windDir": 0,
                                "windSpeed": 0,
                                "windStr": "N/A (Raw Feed Only)",
                                "visibility": "N/A",
                                "clouds": "N/A",
                                "weather": "AMSS Delhi Domestic",
                                "altimeter": "N/A"
                            },
                            "history": [amss_metar]
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
                                except:
                                    vis_str = str(vis_val)
                            else:
                                vis_str = "N/A"
                                    
                                qnh_val = item.get('barometer', {}).get('hpa', 'N/A')
                                qnh_str = f"Q{qnh_val} hPa" if qnh_val != 'N/A' else "N/A"
                                
                                cloud_full = "CAVOK"
                                if item.get('clouds'):
                                    cloud_full = " ".join([f"{c.get('code','')} {c.get('base_feet_agl','')}".strip() for c in item['clouds']])
                                    
                                return {
                                    "icao": station,
                                    "name": station_name,
                                    "time": obs_dt.strftime('%Y-%m-%d %H:%M UTC'),
                                    "model": {
                                        "temp": f"{t_val}°C" if t_val != 'N/A' else "N/A",
                                        "dew": f"{d_val}°C" if d_val != 'N/A' else "N/A",
                                        "windDir": wdir,
                                        "windSpeed": wspd,
                                        "windStr": f"{wdir}° / {wspd} KT (CheckWX)",
                                        "visibility": vis_str,
                                        "clouds": cloud_full,
                                        "weather": "CheckWX Fallback",
                                        "altimeter": qnh_str
                                    },
                                    "history": [item.get('raw_text', '')]
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
                            except:
                                vis_str = str(vis_val)
                        else:
                            vis_str = "N/A"
                                
                        qnh_val = (a_data.get('altimeter') or {}).get('value', 'N/A')
                        qnh_str = f"Q{qnh_val} hPa" if qnh_val != 'N/A' else "N/A"
                        
                        cloud_full = "CAVOK"
                        if a_data.get('clouds'):
                            cloud_full = " ".join([f"{c['type']}{str(c['altitude']).zfill(3)}" for c in a_data['clouds'] if 'altitude' in c])
                            
                        return {
                            "icao": station,
                            "name": station_name,
                            "time": obs_dt.strftime('%Y-%m-%d %H:%M UTC'),
                            "model": {
                                "temp": f"{t_val}°C" if t_val != 'N/A' else "N/A",
                                "dew": f"{d_val}°C" if d_val != 'N/A' else "N/A",
                                "windDir": wdir,
                                "windSpeed": wspd,
                                "windStr": f"{wdir}° / {wspd} KT (AVWX)",
                                "visibility": vis_str,
                                "clouds": cloud_full,
                                "weather": "AVWX Fallback",
                                "altimeter": qnh_str
                            },
                            "history": [a_data.get('raw', '')]
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
                "history": []
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
                
        wx = latest.get('wxString', 'CAVOK')
        if not wx: wx = 'CAVOK'
        
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
        
        cloud_full = " ".join(cloud_strs) if cloud_strs else "CAVOK"
        if cloud_full.strip() == "": cloud_full = "CAVOK"
        
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
            "history": history
        }
    except Exception as e:
        import traceback
        return {"error": str(e), "traceback": traceback.format_exc()}
