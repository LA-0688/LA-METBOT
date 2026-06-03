import re

# Standard Aviation Weather Dictionaries
WX_INTENSITY = {"-": "Light", "+": "Heavy", "VC": "In Vicinity"}
WX_DESCRIPTOR = {"MI": "Shallow", "PR": "Partial", "BC": "Patches", "DR": "Low Drifting",
                 "BL": "Blowing", "SH": "Showers", "TS": "Thunderstorm", "FZ": "Freezing"}
WX_PHENOMENA = {"DZ": "Drizzle", "RA": "Rain", "SN": "Snow", "SG": "Snow Grains",
                "IC": "Ice Crystals", "PL": "Ice Pellets", "GR": "Hail", "GS": "Small Hail",
                "UP": "Unknown Precipitation", "BR": "Mist", "FG": "Fog", "FU": "Smoke",
                "VA": "Volcanic Ash", "DU": "Widespread Dust", "SA": "Sand", "HZ": "Haze",
                "PY": "Spray", "PO": "Well-Developed Dust/Sand Whirls", "SQ": "Squalls",
                "FC": "Funnel Cloud, Tornado, or Waterspout", "SS": "Sandstorm", "DS": "Duststorm"}
CLOUD_COVER = {"FEW": "Few", "SCT": "Scattered", "BKN": "Broken", "OVC": "Overcast", "VV": "Vertical Visibility", "CLR": "Clear", "SKC": "Sky Clear", "NSC": "No Significant Clouds"}
CLOUD_TYPE = {"CB": "Cumulonimbus", "TCU": "Towering Cumulus"}

def compute_flight_rules(vis_m, ceiling_ft):
    """Computes standard FAA flight rules based on visibility (meters) and ceiling (feet)."""
    vis_sm = vis_m / 1609.34 if vis_m is not None else 999
    
    if (ceiling_ft is not None and ceiling_ft < 500) or (vis_sm < 1.0):
        return "LIFR"
    elif (ceiling_ft is not None and ceiling_ft < 1000) or (vis_sm < 3.0):
        return "IFR"
    elif (ceiling_ft is not None and ceiling_ft <= 3000) or (vis_sm <= 5.0):
        return "MVFR"
    else:
        return "VFR"

def parse_weather_string(raw_str):
    """Extracts met elements from a raw string (applicable to METAR or TAF change group)"""
    decoded = {
        "wind": "N/A", "visibility": "N/A", "weather": "None", "clouds": "Clear",
        "temp": "N/A", "dew": "N/A", "altimeter": "N/A", "flight_rules": "VFR"
    }
    
    vis_m = None
    ceiling_ft = None
    
    # 1. Wind
    wind_match = re.search(r'\b([0-9]{3}|VRB)([0-9]{2,3})(?:G([0-9]{2,3}))?KT\b', raw_str)
    if wind_match:
        wdir_str = wind_match.group(1)
        wspd = int(wind_match.group(2))
        wgst = wind_match.group(3)
        dir_text = "Variable" if wdir_str == "VRB" else f"{wdir_str}°"
        wind_text = f"{dir_text} at {wspd} knots"
        if wgst:
            wind_text += f", gusting to {wgst} knots"
        decoded["wind"] = wind_text

    # 2. Visibility
    # Remove time strings like 021800Z or 0218/0303 to avoid confusing the 4-digit visibility regex
    clean_str = re.sub(r'\b[0-9]{6}Z\b', '', raw_str)
    clean_str = re.sub(r'\b[0-9]{4}/[0-9]{4}\b', '', clean_str)
    
    if "CAVOK" in clean_str:
        decoded["visibility"] = "10+ km (CAVOK)"
        decoded["weather"] = "CAVOK"
        decoded["clouds"] = "CAVOK"
        vis_m = 10000
        ceiling_ft = 9999
    else:
        vis_match = re.search(r'\b([0-9]{4})\b', clean_str)
        if vis_match:
            val = int(vis_match.group(1))
            vis_m = val
            if val == 9999:
                decoded["visibility"] = "10+ km"
            else:
                sm = round(val / 1609.34, 1)
                decoded["visibility"] = f"{val} meters ({sm} sm)"
        else:
            vis_sm = re.search(r'\b([0-9]+)SM\b', clean_str)
            if vis_sm:
                sm_val = int(vis_sm.group(1))
                vis_m = sm_val * 1609.34
                decoded["visibility"] = f"{sm_val} sm"

    # 3. Weather
    wx_matches = re.finditer(r'\b(-|\+|VC)?(TS|SH|DZ|RA|SN|SG|IC|PL|GR|GS|UP|BR|FG|FU|VA|DU|SA|HZ|PY|PO|SQ|FC|SS|DS)+\b', raw_str)
    wx_list = []
    for m in wx_matches:
        full_code = m.group(0)
        desc = ""
        # Check intensity
        if full_code.startswith("+"): desc += "Heavy "; full_code = full_code[1:]
        elif full_code.startswith("-"): desc += "Light "; full_code = full_code[1:]
        elif full_code.startswith("VC"): desc += "In Vicinity "; full_code = full_code[2:]
        
        # Check descriptor (first 2 chars might be descriptor if len > 2)
        if len(full_code) >= 4 and full_code[0:2] in WX_DESCRIPTOR:
            desc += WX_DESCRIPTOR[full_code[0:2]] + " "
            full_code = full_code[2:]
            
        # Parse remaining phenomena
        phenomena = []
        for i in range(0, len(full_code), 2):
            chunk = full_code[i:i+2]
            if chunk in WX_PHENOMENA: phenomena.append(WX_PHENOMENA[chunk])
            elif chunk in WX_DESCRIPTOR: phenomena.append(WX_DESCRIPTOR[chunk])
            
        desc += " ".join(phenomena)
        wx_list.append(desc.strip())
        
    if wx_list:
        decoded["weather"] = ", ".join(wx_list)

    # 4. Clouds
    cloud_groups = re.findall(r'\b(FEW|SCT|BKN|OVC|VV|CLR|SKC|NSC|CAVOK)([0-9]{3})?(CB|TCU)?\b', raw_str)
    if cloud_groups:
        cloud_strs = []
        for cvr, base, ctype in cloud_groups:
            if cvr in ["CLR", "SKC", "NSC", "CAVOK"]: continue
            cover_text = CLOUD_COVER.get(cvr, cvr)
            base_ft = int(base) * 100 if base else 0
            type_text = f", {CLOUD_TYPE.get(ctype, ctype)}" if ctype else ""
            cloud_strs.append(f"{cover_text} at {base_ft} ft{type_text}")
            
            if cvr in ["BKN", "OVC", "VV"] and (ceiling_ft is None or base_ft < ceiling_ft):
                ceiling_ft = base_ft
                
        if cloud_strs:
            decoded["clouds"] = "; ".join(cloud_strs)

    # 5. Temp/Dew
    temp_match = re.search(r'\b(M?[0-9]{2})/(M?[0-9]{2})\b', raw_str)
    if temp_match:
        t = int(temp_match.group(1).replace('M', '-'))
        d = int(temp_match.group(2).replace('M', '-'))
        decoded["temp"] = f"{t}°C"
        decoded["dew"] = f"{d}°C"

    # 6. Altimeter
    alt_q = re.search(r'\bQ([0-9]{4})\b', raw_str)
    if alt_q:
        decoded["altimeter"] = f"{alt_q.group(1)} hPa"
    else:
        alt_a = re.search(r'\bA([0-9]{4})\b', raw_str)
        if alt_a:
            inhg = float(alt_a.group(1)) / 100.0
            hpa = int(round(inhg * 33.8639))
            decoded["altimeter"] = f"{inhg} inHg ({hpa} hPa)"

    decoded["flight_rules"] = compute_flight_rules(vis_m, ceiling_ft)
    return decoded


def decode_metar(raw_metar):
    if not raw_metar: return None
    return parse_weather_string(raw_metar)


def decode_taf(raw_taf):
    if not raw_taf: return None
    
    # Split TAF by change groups
    # Look for PROB30, PROB40, TEMPO, BECMG, FM
    tokens = raw_taf.split()
    
    initial_tokens = []
    changes = []
    current_change = None
    
    for token in tokens:
        if token in ["TEMPO", "BECMG"] or token.startswith("PROB") or token.startswith("FM"):
            if current_change:
                changes.append(current_change)
            current_change = {"type": token, "raw": [], "parsed": {}}
        else:
            if current_change:
                current_change["raw"].append(token)
            else:
                initial_tokens.append(token)
                
    if current_change:
        changes.append(current_change)
        
    # Parse initial period
    initial_raw = " ".join(initial_tokens)
    initial_parsed = parse_weather_string(initial_raw)
    
    # Extract valid period from initial block (e.g. 0218/0303)
    period_match = re.search(r'\b([0-9]{4})/([0-9]{4})\b', initial_raw)
    valid_period = period_match.group(0) if period_match else "Unknown"
    initial_parsed["period"] = valid_period
    
    # Parse change blocks
    parsed_changes = []
    for c in changes:
        raw_str = " ".join(c["raw"])
        parsed = parse_weather_string(raw_str)
        
        # Time period for the change
        if c["type"].startswith("FM"):
            parsed["period"] = f"From {c['type'][2:]}Z"
        else:
            p_match = re.search(r'\b([0-9]{4})/([0-9]{4})\b', raw_str)
            parsed["period"] = p_match.group(0) if p_match else ""
            
        parsed["type"] = c["type"]
        parsed["raw"] = f"{c['type']} {raw_str}"
        parsed_changes.append(parsed)
        
    return {
        "initial": initial_parsed,
        "changes": parsed_changes
    }
