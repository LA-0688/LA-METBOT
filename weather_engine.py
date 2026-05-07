import requests

def format_visibility(vis_sm, station_code):
    if vis_sm == 'N/A' or vis_sm is None:
        return 'N/A'
    
    vis_str = str(vis_sm)
    if station_code.startswith('K'):
        return f"{vis_str} SM"
        
    has_plus = '+' in vis_str
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
    """Fetches and decodes live METAR, TAF, and D-ATIS instantly using pure Python."""
    stations_list = [s.strip().upper() for s in stations.replace(",", " ").split() if s.strip()]
    if not stations_list:
        return "Please provide at least one station code."
        
    clean_stations = ",".join(stations_list)
    result_text = ""
    
    try:
        metar_url = f"https://aviationweather.gov/api/data/metar?ids={clean_stations}&format=json"
        metar_resp = requests.get(metar_url, timeout=5)
        try:
            metar_response = metar_resp.json() if metar_resp.text.strip() else []
        except Exception:
            metar_response = []
        
        taf_url = f"https://aviationweather.gov/api/data/taf?ids={clean_stations}&format=json"
        taf_resp = requests.get(taf_url, timeout=5)
        try:
            taf_response = taf_resp.json() if taf_resp.text.strip() else []
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
            result_text += f"━━━━━━━━━━━━━━━━━━━━\n"
            result_text += f"📍 *STATION: {station}*\n"
            result_text += f"━━━━━━━━━━━━━━━━━━━━\n\n"
            
            # --- METAR ---
            if station in metars_by_station:
                m = metars_by_station[station]
                raw_metar = m.get('rawOb', 'N/A')
                name = m.get('name', 'Unknown Station')
                obs_time_raw = m.get('obsTime', 'N/A')
                obs_time = str(obs_time_raw).replace('T', ' ').replace('Z', ' UTC')
                flt_cat = m.get('fltCat', 'N/A')
                temp = m.get('temp', 'N/A')
                dewp = m.get('dewp', 'N/A')
                wdir = m.get('wdir', 'VRB' if m.get('wdir') == 0 else m.get('wdir', 'N/A'))
                wspd = m.get('wspd', 'N/A')
                vis = m.get('visib', 'N/A')
                vis_formatted = format_visibility(vis, station)
                clouds = m.get('clouds', [])
                wx = m.get('wxString', '')
                
                cloud_str = decode_clouds(clouds)
                wx_str = decode_wx(wx)
                
                result_text += f"✈️ *METAR*\n`{raw_metar}`\n\n"
                if name != 'Unknown Station':
                    result_text += f"🏢 *Facility:* {name}\n"
                if obs_time != 'N/A':
                    result_text += f"🕒 *Observed:* {obs_time}\n"
                if flt_cat != 'N/A':
                    result_text += f"🚦 *Flight Rules:* {flt_cat}\n"
                
                result_text += f"🌡️ *Temp:* {temp}°C | *Dewpoint:* {dewp}°C\n"
                result_text += f"💨 *Winds:* {wdir}° at {wspd} knots\n"
                result_text += f"👁️ *Visibility:* {vis_formatted}\n"
                result_text += f"☁️ *Clouds:* {cloud_str}\n"
                if wx_str:
                    result_text += f"🌧️ *Weather:* {wx_str}\n"
                result_text += "\n"
            else:
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
                    # Parse basic ZULU time from raw string if available
                    parts = raw_metar.split()
                    zulu_time = "N/A"
                    if len(parts) > 1 and parts[1].endswith('Z'):
                        zulu_time = f"Day {parts[1][:2]} at {parts[1][2:6]} UTC"
                        
                    result_text += f"✈️ *METAR*\n`{raw_metar}`\n\n"
                    if zulu_time != "N/A":
                        result_text += f"🕒 *Observed:* {zulu_time}\n"
                else:
                    result_text += f"✈️ *METAR*\n_No METAR data available._\n\n"
                
            # --- TAF ---
            if station in tafs_by_station:
                t = tafs_by_station[station]
                raw_taf = t.get('rawTAF', 'N/A')
                result_text += f"📅 *TAF (Forecast)*\n_{raw_taf}_\n\n"
                result_text += "*Decoded Forecast:*\n"
                
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
                    result_text += f"📅 *TAF (Forecast)*\n_{clean_taf}_\n\n"
                else:
                    result_text += f"📅 *TAF*\n_No TAF forecast available._\n\n"
                
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
                    result_text += f"📻 *D-ATIS*\n_Not available online (Usually only US airports broadcast D-ATIS to the internet)._\n\n"
            except Exception:
                result_text += f"📻 *D-ATIS*\n_Could not connect to ATIS server._\n\n"
                
        return result_text
        
    except Exception as e:
        return f"Error fetching weather data: {str(e)}"
