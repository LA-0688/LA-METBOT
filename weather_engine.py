import requests, time
from datetime import datetime, timezone
from typing import Dict, Any

# ---------- Helper: robust GET with retries ----------
def safe_get(url: str, *, timeout: int = 12, retries: int = 2) -> Any:
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

# ---------- In-memory cache (1-minute TTL) ----------
weather_cache: Dict[str, tuple[str, float]] = {}
sun_cache: Dict[str, tuple[Dict[str, str], float]] = {}
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
    
    try:
        # 1. Fetch Data with safe_get
        metar_url = f"https://aviationweather.gov/api/data/metar?ids={clean_stations}&format=json"
        taf_url = f"https://aviationweather.gov/api/data/taf?ids={clean_stations}&format=json"
        
        try:
            metar_response = safe_get(metar_url) or []
        except Exception:
            metar_response = []
            
        try:
            taf_response = safe_get(taf_url) or []
        except Exception:
            taf_response = []
        
        metars_by_station = {}
        if isinstance(metar_response, list):
            for m in metar_response:
                metars_by_station[m.get('icaoId', 'Unknown')] = m
                
        tafs_by_station = {}
        if isinstance(taf_response, list):
            for t in taf_response:
                tafs_by_station[t.get('icaoId', 'Unknown')] = t
        
        for station in stations_list:
            lat, lon = None, None
            header_info = ""
            
            if station in metars_by_station:
                m = metars_by_station[station]
                lat = m.get('lat')
                lon = m.get('lon')
                if lat is not None and lon is not None:
                    lat_dir = 'N' if lat >= 0 else 'S'
                    lon_dir = 'E' if lon >= 0 else 'W'
                    coords = f"{abs(lat):.2f}°{lat_dir} {abs(lon):.2f}°{lon_dir}"
                    sun = get_sun_times(lat, lon)
                    header_info = f" | **{coords}** | **🌅 {sun['sunrise']} 🌇 {sun['sunset']}**"

            result_text += f"### 📍 **{station}**{header_info}\n\n"
            
            # --- METAR ---
            if station in metars_by_station:
                m = metars_by_station[station]
                raw_metar = m.get('rawOb', 'N/A')
                name = m.get('name', 'Unknown Station')
                obs_time_raw = m.get('obsTime', 'N/A')
                obs_time = str(obs_time_raw).replace('T', ' ').replace('Z', ' UTC')
                
                # Check if stale (older than 1 hour)
                is_stale = False
                if obs_time_raw != 'N/A':
                    try:
                        obs_dt = datetime.fromisoformat(obs_time_raw.replace('Z', '+00:00'))
                        now_dt = datetime.now(timezone.utc)
                        if (now_dt - obs_dt).total_seconds() > 3600:
                            is_stale = True
                    except Exception:
                        pass
                
                # Fallback to NOAA if stale
                if is_stale:
                    try:
                        noaa_m = requests.get(f"https://tgftp.nws.noaa.gov/data/observations/metar/stations/{station}.TXT", timeout=3)
                        if noaa_m.status_code == 200:
                            lines = noaa_m.text.strip().split('\n')
                            if len(lines) >= 2:
                                raw_metar = lines[1]
                    except Exception:
                        pass
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
                
                result_text += f"✈️ *METAR*\n`{raw_metar}`\n\n"
                if is_stale:
                    result_text += "⚠️ *Warning:* Decoded data below may be stale (source delayed). Showing latest raw METAR from NOAA if available.\n\n"
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
                # Try Cache first as fallback
                cached = weather_cache.get(station)
                if cached and (time.time() - cached[1] < CACHE_TTL):
                    result_text += cached[0]
                else:
                    # NOAA Fallback
                    raw_metar = None
                    try:
                        noaa_m = requests.get(f"https://tgftp.nws.noaa.gov/data/observations/metar/stations/{station}.TXT", timeout=3)
                        if noaa_m.status_code == 200:
                            lines = noaa_m.text.strip().split('\n')
                            if len(lines) >= 2:
                                raw_metar = lines[1]
                    except Exception:
                        pass
                    
                    if raw_metar:
                        result_text += f"✈️ *METAR*\n`{raw_metar}`\n\n"
                    else:
                        result_text += f"✈️ *METAR*\n_No METAR data available._\n\n"
                
            # --- TAF ---
            if station in tafs_by_station:
                t = tafs_by_station[station]
                raw_taf = t.get('rawTAF', 'N/A')
                result_text += f"📅 **TAF**\n_{raw_taf}_\n\n"
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
                # TAF Fallback
                raw_taf = None
                try:
                    noaa_t = requests.get(f"https://tgftp.nws.noaa.gov/data/forecasts/taf/stations/{station}.TXT", timeout=3)
                    if noaa_t.status_code == 200:
                        lines = noaa_t.text.strip().split('\n')
                        if len(lines) >= 2:
                            raw_taf = " ".join(lines[1:])
                except Exception:
                    pass
                
                if raw_taf:
                    clean_taf = raw_taf.replace('\n', ' ')
                    result_text += f"📅 **TAF**\n_{clean_taf}_\n\n"
                else:
                    result_text += f"📅 **TAF**\n_No TAF forecast available._\n\n"
                
            # --- ATIS ---
            try:
                atis_url = f"https://datis.clowd.io/api/{station}"
                atis_resp = requests.get(atis_url, timeout=3)
                if atis_resp.status_code == 200:
                    atis_data = atis_resp.json()
                    if isinstance(atis_data, list) and atis_data:
                        for datis in atis_data:
                            atis_type = datis.get('type', 'combined').title()
                            atis_text = datis.get('datis', 'N/A')
                            result_text += f"📻 *D-ATIS ({atis_type})*\n_{atis_text}_\n\n"
                    elif isinstance(atis_data, dict) and 'error' not in atis_data:
                        atis_text = atis_data.get('datis', 'N/A')
                        result_text += f"📻 *D-ATIS*\n_{atis_text}_\n\n"
                else:
                    result_text += f"📻 *D-ATIS*\n_Not available online._\n\n"
            except Exception:
                result_text += f"📻 *D-ATIS*\n_Could not connect to ATIS server._\n\n"
                
        return result_text
        
    except Exception as e:
        return f"Error fetching weather data: {str(e)}"

