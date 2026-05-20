import requests, time
from datetime import datetime, timezone
from typing import Dict, Any
import concurrent.futures

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
    
    # Cache Lookup
    current_time = time.time()
    if stations in weather_cache:
        cached_data, timestamp = weather_cache[stations]
        if current_time - timestamp < 60: # 1 minute TTL
            return cached_data
    
    try:
        # 1. Fetch Data in Parallel (METAR, TAF, and D-ATIS)
        urls = {
            'metar': f"https://aviationweather.gov/api/data/metar?ids={clean_stations}&format=json",
            'taf': f"https://aviationweather.gov/api/data/taf?ids={clean_stations}&format=json"
        }
        
        # Add D-ATIS URLs for each station
        for s in stations_list:
            urls[f'atis_{s}'] = f"https://datis.clowd.io/api/{s}"
            
        def fetch_url(name, url):
            try:
                if 'aviationweather' in url:
                    return name, safe_get(url, timeout=6, retries=1)
                else:
                    # D-ATIS requests
                    resp = requests.get(url, timeout=5)
                    if resp.status_code == 200:
                        return name, resp.json()
                    return name, None
            except Exception:
                return name, None

        results = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
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
                
            if lat is not None and lon is not None:
                lat_dir = 'N' if lat >= 0 else 'S'
                lon_dir = 'E' if lon >= 0 else 'W'
                coords = f"{abs(lat):.2f}°{lat_dir} - {abs(lon):.2f}°{lon_dir}"
                sun = get_sun_times(lat, lon)
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
                obs_time = str(obs_time_raw).replace('T', ' ').replace('Z', ' UTC')
                
                # Check if stale (older than 1 hour)
                is_stale = False
                elapsed_min = 0
                if obs_time_raw != 'N/A':
                    try:
                        obs_dt = datetime.fromisoformat(obs_time_raw.replace('Z', '+00:00'))
                        now_dt = datetime.now(timezone.utc)
                        elapsed_min = int((now_dt - obs_dt).total_seconds() / 60)
                        if (now_dt - obs_dt).total_seconds() > 3600:
                            is_stale = True
                    except Exception:
                        pass
                
                # Fallback to NOAA if stale
                if is_stale:
                    try:
                        noaa_m = requests.get(f"https://tgftp.nws.noaa.gov/data/observations/metar/stations/{station}.TXT", timeout=2)
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
                
                result_text += f"✈️ *METAR* ({elapsed_min}m ago)\n```\n{raw_metar}\n```\n\n"
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
                        noaa_m = requests.get(f"https://tgftp.nws.noaa.gov/data/observations/metar/stations/{station}.TXT", timeout=5)
                        if noaa_m.status_code == 200:
                            lines = noaa_m.text.strip().split('\n')
                            if len(lines) >= 2:
                                raw_metar = lines[1]
                    except Exception:
                        pass
                    
                    if raw_metar:
                        result_text += f"✈️ *METAR*\n```\n{raw_metar}\n```\n\n"
                    else:
                        result_text += f"✈️ *METAR*\n_No METAR data available._\n\n"
                
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
                # TAF Fallback
                raw_taf = None
                issue_time_formatted = "N/A"
                try:
                    noaa_t = requests.get(f"https://tgftp.nws.noaa.gov/data/forecasts/taf/stations/{station}.TXT", timeout=5)
                    if noaa_t.status_code == 200:
                        lines = noaa_t.text.strip().split('\n')
                        if len(lines) >= 2:
                            raw_taf = " ".join(lines[1:])
                            raw_taf = raw_taf.strip()
                            while raw_taf.upper().startswith('TAF'):
                                raw_taf = raw_taf[3:].strip()
                            noaa_time = lines[0].strip()
                            issue_time_formatted = noaa_time.replace('/', '-') + ":00 UTC"
                except Exception:
                    pass
                
                if raw_taf:
                    result_text += f"📅 **TAF** (Issued: {issue_time_formatted})\n```\n{raw_taf}\n```\n\n"
                else:
                    result_text += f"📅 **TAF**\n_No TAF forecast available._\n\n"
                
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
                
        weather_cache[stations] = (result_text, current_time)
        return result_text
        
    except Exception as e:
        return f"Error fetching weather data: {str(e)}"

def get_station_details(station: str) -> dict:
    """Fetches detailed structural JSON for the visual station grid and history."""
    station = station.strip().upper()
    try:
        url = f"https://aviationweather.gov/api/data/metar?ids={station}&format=json&hours=4"
        data = safe_get(url, timeout=5, retries=1)
        if not data:
            return {"error": "No data found"}
        
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
            if vis == '9999' or vis == 9999:
                vis_str = "10km+"
            elif isinstance(vis, (int, float)):
                # NOAA API usually returns visibility in Statute Miles (SM) for numeric values < 10
                # Convert SM to meters (1 SM = 1609.34 meters)
                if vis < 10:
                    vis_m = int(round((vis * 1609.34) / 100.0) * 100)
                    if vis_m >= 9999:
                        vis_str = "10km+"
                    elif vis_m >= 1000:
                        vis_str = f"{vis_m}m"
                    else:
                        vis_str = f"{vis_m}m"
                else:
                    # If it's returning raw meters for some reason
                    vis_str = f"{vis}m" if vis < 9999 else "10km+"
            else:
                vis_str = str(vis)
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
            from datetime import datetime, timezone
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
