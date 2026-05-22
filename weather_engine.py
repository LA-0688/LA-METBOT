import requests, time
from datetime import datetime, timezone
from typing import Dict, Any
import concurrent.futures
import math

# ---------- Helper: robust GET with retries ----------
def safe_get(url: str, *, timeout: int = 3, retries: int = 0) -> Any:
    """Fetch JSON with exponential back-off to prevent random timeout errors."""
    backoff = 1
    for attempt in range(retries + 1):
        try:
            resp = requests.get(url, timeout=timeout)
            resp.raise_for_status()
            if not resp.text.strip():
                return []
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

def get_dew_point(t, rh):
    """Calculates dew point given temperature in C and relative humidity in %."""
    a = 17.27
    b = 237.7
    if t == 'N/A' or rh == 'N/A': return 10
    alpha = ((a * t) / (b + t)) + math.log(rh / 100.0)
    td = (b * alpha) / (a - alpha)
    return round(td)

def generate_synthetic_metar(station: str, lat: float, lon: float) -> str:
    """Generates a synthetic METAR string using Open-Meteo data when official feeds are missing."""
    try:
        url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current=temperature_2m,relative_humidity_2m,pressure_msl,wind_speed_10m,wind_direction_10m,cloud_cover,visibility&wind_speed_unit=kn"
        resp = safe_get(url, timeout=5, retries=1)
        if not resp or 'current' not in resp:
            return None
        
        c = resp['current']
        temp = round(c.get('temperature_2m', 15))
        rh = c.get('relative_humidity_2m', 50)
        dew = get_dew_point(temp, rh)
        
        qnh = round(c.get('pressure_msl', 1013))
        
        wind_dir = c.get('wind_direction_10m', 0)
        wind_spd = round(c.get('wind_speed_10m', 0))
        
        vis_m = c.get('visibility', 9999)
        if vis_m >= 9999:
            vis_m = 9999
        vis_str = f"{int(vis_m):04d}"
        
        cloud_cov = c.get('cloud_cover', 0)
        if cloud_cov < 10:
            clouds = "CAVOK"
        elif cloud_cov < 30:
            clouds = "FEW030"
        elif cloud_cov < 70:
            clouds = "SCT030"
        elif cloud_cov < 90:
            clouds = "BKN030"
        else:
            clouds = "OVC030"
            
        wind_str = f"{int(wind_dir):03d}{int(wind_spd):02d}KT"
        
        def fmt_temp(t):
            return f"M{abs(t):02d}" if t < 0 else f"{t:02d}"
            
        temp_str = f"{fmt_temp(temp)}/{fmt_temp(dew)}"
        
        now = datetime.now(timezone.utc)
        time_str = now.strftime("%d%H%M") + "Z"
        
        synth_metar = f"{station} {time_str} {wind_str} {vis_str} {clouds} {temp_str} Q{qnh} SYNTH"
        return synth_metar
    except Exception:
        return None

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
            urls[f'info_{s}'] = f"https://aviationweather.gov/api/data/stationinfo?ids={s}&format=json"
            
        def fetch_url(name, url):
            try:
                if 'aviationweather' in url:
                    return name, safe_get(url, timeout=6, retries=1)
                elif 'tgftp.nws.noaa.gov' in url:
                    # NOAA plain-text endpoints
                    resp = requests.get(url, timeout=4)
                    if resp.status_code == 200:
                        return name, resp.text
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
        
        # Pre-fetch sun times in parallel for all stations that have coordinates
        sun_futures = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as sun_executor:
            for station in stations_list:
                lat_tmp, lon_tmp = None, None
                
                # Check station info first
                info_data = results.get(f'info_{station}')
                if info_data and isinstance(info_data, list) and len(info_data) > 0:
                    lat_tmp = info_data[0].get('lat', lat_tmp)
                    lon_tmp = info_data[0].get('lon', lon_tmp)
                    
                if station in KNOWN_STATIONS:
                    lat_tmp = KNOWN_STATIONS[station].get('lat', lat_tmp)
                    lon_tmp = KNOWN_STATIONS[station].get('lon', lon_tmp)
                m_tmp = metars_by_station.get(station)
                if m_tmp:
                    lat_tmp = m_tmp.get('lat', lat_tmp)
                    lon_tmp = m_tmp.get('lon', lon_tmp)
                if lat_tmp is not None and lon_tmp is not None:
                    sun_futures[station] = sun_executor.submit(get_sun_times, lat_tmp, lon_tmp)
            # Collect results
            sun_results = {s: f.result() for s, f in sun_futures.items()}

        for station in stations_list:
            lat, lon = None, None
            header_info = ""
            name = "Unknown Station"
            
            # Check station info first
            info_data = results.get(f'info_{station}')
            if info_data and isinstance(info_data, list) and len(info_data) > 0:
                name = info_data[0].get('site', name)
                lat = info_data[0].get('lat', lat)
                lon = info_data[0].get('lon', lon)
            
            # Apply hardcoded fallback if available
            if station in KNOWN_STATIONS:
                fallback = KNOWN_STATIONS[station]
                name = fallback.get('name', name)
                lat = fallback.get('lat', lat)
                lon = fallback.get('lon', lon)
            
            if station in metars_by_station:
                m = metars_by_station[station]
                name = m.get('name', name) # Keep fallback name if API returns empty
                lat = m.get('lat', lat)
                lon = m.get('lon', lon)
                
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
                obs_time = str(obs_time_raw).replace('T', ' ').replace('Z', ' UTC')
                
                # Check if stale (older than 2 hours)
                is_stale = False
                elapsed_min = 0
                if obs_time_raw != 'N/A':
                    try:
                        obs_dt = datetime.fromisoformat(obs_time_raw.replace('Z', '+00:00'))
                        now_dt = datetime.now(timezone.utc)
                        elapsed_min = int((now_dt - obs_dt).total_seconds() / 60)
                        if (now_dt - obs_dt).total_seconds() > 7200: # 2 hours
                            is_stale = True
                    except Exception:
                        pass
                
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
                                    raw_metar = lines[1]
                                    is_stale = False
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
                noaa_text = results.get(f'noaa_metar_{station}')
                if noaa_text:
                    lines = noaa_text.strip().split('\n')
                    if len(lines) >= 2:
                        noaa_time_str = lines[0].strip()
                        try:
                            noaa_dt = datetime.strptime(noaa_time_str, "%Y/%m/%d %H:%M").replace(tzinfo=timezone.utc)
                            now_dt = datetime.now(timezone.utc)
                            if (now_dt - noaa_dt).total_seconds() <= 3600 * 2: # Max 2 hours for fallback
                                raw_metar = lines[1]
                        except Exception:
                            pass
                    
                    
                if not raw_metar and lat is not None and lon is not None:
                    # Final fallback: Synthesize METAR using open-meteo
                    synth = generate_synthetic_metar(station, lat, lon)
                    if synth:
                        raw_metar = synth

                if raw_metar:
                    synth_warning = "\n⚠️ *This is a SYNTHETIC METAR generated from meteorological models because no official ATC data was found.*" if "SYNTH" in raw_metar else ""
                    result_text += f"✈️ **METAR**\n```\n{raw_metar}\n```\n{synth_warning}\n\n"
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
        url = f"https://aviationweather.gov/api/data/metar?ids={station}&format=json&hours=4"
        data = safe_get(url, timeout=5, retries=1)
        
        info_url = f"https://aviationweather.gov/api/data/stationinfo?ids={station}&format=json"
        info_data = safe_get(info_url, timeout=5, retries=1)
        
        station_name = "Unknown Station"
        if info_data and isinstance(info_data, list) and len(info_data) > 0:
            station_name = info_data[0].get('site', 'Unknown Station')
            
        if not data:
            synth_time = "N/A"
            synth_temp = "N/A"
            synth_dew = "N/A"
            synth_wdir = 0
            synth_wspd = 0
            synth_vis = "N/A"
            synth_cloud = "CAVOK"
            synth_qnh = "N/A"
            
            lat, lon = None, None
            if info_data and isinstance(info_data, list) and len(info_data) > 0:
                lat = info_data[0].get('lat')
                lon = info_data[0].get('lon')
            elif station in KNOWN_STATIONS:
                lat = KNOWN_STATIONS[station].get('lat')
                lon = KNOWN_STATIONS[station].get('lon')
                
            if lat is not None and lon is not None:
                try:
                    url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current=temperature_2m,relative_humidity_2m,pressure_msl,wind_speed_10m,wind_direction_10m,cloud_cover,visibility&wind_speed_unit=kn"
                    resp = safe_get(url, timeout=5, retries=1)
                    if resp and 'current' in resp:
                        c = resp['current']
                        t_val = round(c.get('temperature_2m', 15))
                        rh_val = c.get('relative_humidity_2m', 50)
                        d_val = get_dew_point(t_val, rh_val)
                        synth_temp = f"{t_val}°C"
                        synth_dew = f"{d_val}°C"
                        
                        synth_wdir = int(c.get('wind_direction_10m', 0))
                        synth_wspd = int(round(c.get('wind_speed_10m', 0)))
                        
                        vis_m = c.get('visibility', 9999)
                        synth_vis = "10+ Kms" if vis_m >= 9999 else f"{int(vis_m)}m"
                        
                        cc = c.get('cloud_cover', 0)
                        if cc < 10: synth_cloud = "CAVOK"
                        elif cc < 30: synth_cloud = "FEW030"
                        elif cc < 70: synth_cloud = "SCT030"
                        elif cc < 90: synth_cloud = "BKN030"
                        else: synth_cloud = "OVC030"
                        
                        synth_qnh = f"Q{int(round(c.get('pressure_msl', 1013)))} hPa"
                        
                        now = datetime.now(timezone.utc)
                        synth_time = now.strftime('%Y-%m-%d %H:%M UTC')
                except Exception:
                    pass

            return {
                "icao": station,
                "name": station_name,
                "time": synth_time,
                "model": {
                    "temp": synth_temp,
                    "dew": synth_dew,
                    "windDir": synth_wdir,
                    "windSpeed": synth_wspd,
                    "windStr": f"{synth_wdir}° / {synth_wspd} KT (SYNTH)",
                    "visibility": synth_vis,
                    "clouds": synth_cloud,
                    "weather": "SYNTHETIC",
                    "altimeter": synth_qnh
                },
                "history": ["No official METAR history available. Showing synthetic weather data."] if synth_time != "N/A" else []
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
