import os
import telebot
from flask import Flask, request, jsonify, render_template
from weather_engine import get_instant_weather, get_station_details
from db_manager import get_cached_weather, upsert_weather, get_weather_batch
import re
from datetime import datetime, timezone
import decoder

# Initialize the Flask Web Server
app = Flask(__name__)

# Telegram Bot Token
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
if TELEGRAM_TOKEN:
    bot = telebot.TeleBot(TELEGRAM_TOKEN, threaded=False)
else:
    bot = None
    print("[WARNING] TELEGRAM_TOKEN not set. Telegram bot is disabled.", flush=True)

def get_elapsed_str(raw_text):
    """Extracts DDHHMMZ from METAR or TAF and computes '(Xh Ym ago)'"""
    if not raw_text:
        return ""
    match = re.search(r'\b(\d{2})(\d{2})(\d{2})Z\b', raw_text)
    if not match:
        return ""
    day, hour, minute = int(match.group(1)), int(match.group(2)), int(match.group(3))
    now = datetime.now(timezone.utc)
    month, year = now.month, now.year
    if day > now.day + 15:
        month -= 1
        if month == 0:
            month = 12
            year -= 1
    try:
        obs_time = datetime(year, month, day, hour, minute, tzinfo=timezone.utc)
        diff = now - obs_time
        mins = int(diff.total_seconds() / 60)
        if mins < 0:
            return ""
        if mins < 60:
            return f" ({mins}m ago)"
        else:
            return f" ({mins//60}h {mins%60}m ago)"
    except Exception:
        return ""

# ==========================================
# 1. THE WEBSITE ENDPOINTS
# ==========================================
@app.route("/", methods=['GET'])
def index():
    """Serves the beautiful frontend website!"""
    return render_template("index.html")

def calculate_flight_category(vis_str, clouds_str):
    vis_m = 10000
    if vis_str:
        v = vis_str.lower()
        if 'm' in v and 'k' not in v:
            try: vis_m = int(''.join(filter(str.isdigit, v)))
            except: pass
        elif 'k' in v:
            try: vis_m = float(''.join([ch for ch in v if ch.isdigit() or ch=='.'])) * 1000
            except: pass

    ceiling_ft = 99999
    if clouds_str and "CAVOK" not in clouds_str.upper():
        matches = re.findall(r'(?:BKN|OVC|VV)([0-9]{3})', clouds_str.upper())
        for m in matches:
            try:
                c_ft = int(m) * 100
                if c_ft < ceiling_ft: ceiling_ft = c_ft
            except: pass

    if ceiling_ft < 500 or vis_m < 1600: return "LIFR"
    elif ceiling_ft < 1000 or vis_m < 4800: return "IFR"
    elif ceiling_ft <= 3000 or vis_m <= 8000: return "MVFR"
    else: return "VFR"

@app.route("/api/weather", methods=['GET'])
def api_weather():
    """Returns pure JSON for the frontend to render the UI."""
    from db_manager import get_weather_batch
    raw_stations = request.args.get('stations', '')
    stations_list = []
    for s in raw_stations.replace(",", " ").split():
        s_clean = s.strip().upper()
        if s_clean and s_clean not in stations_list:
            stations_list.append(s_clean)
    
    if not stations_list:
        return jsonify({"status": "error", "message": "Please provide at least one station code."}), 400
        
    results = {}
    cached_batch = get_weather_batch(stations_list)
    
    for icao in stations_list:
        cached_data = cached_batch.get(icao)
        
        # If cache miss, fetch on-demand and store
        if not cached_data:
            try:
                live_result = get_station_details(icao)
                raw_metar = live_result.get('history', [''])[0] if live_result.get('history') else ''
                raw_taf = live_result.get('raw_taf', '')
                payload_for_db = dict(live_result)
                payload_for_db['history'] = list(payload_for_db.get('history', []))[:3]
                upsert_weather(icao, raw_metar, raw_taf, payload_for_db)
                
                # Fetch back from DB to get the correct standard format
                from db_manager import get_cached_weather
                db_record = get_cached_weather(icao)
                if db_record:
                    cached_data = {
                        "raw_metar": db_record.get('raw_metar', ''),
                        "raw_taf": db_record.get('raw_taf', ''),
                        "decoded": db_record.get('decoded_data', {}),
                        "last_updated": db_record['last_updated'].isoformat()
                    }
            except Exception as e:
                print(f"Dynamic fetch failed for {icao}: {e}", flush=True)

        if cached_data:
            c = cached_data['decoded']
            m = c.get('model', {})
            
            history = c.get('history', [])
            raw_metar = history[0] if history else ""
            raw_taf = cached_data.get('raw_taf', '')
            
            # Cleanly parse out numbers for the developer API
            temp_c = None
            if m.get('temp') and '°C' in m['temp']:
                try: temp_c = int(m['temp'].replace('°C', ''))
                except: pass
                
            flight_cat = calculate_flight_category(m.get('visibility', ''), m.get('clouds', ''))

            results[icao] = {
                "icao": c.get('icao', icao),
                "name": c.get('name', 'Unknown Station'),
                "coords": c.get('coords_str', c.get('coords', '')),
                "sun": c.get('sun_str', c.get('sun', '')),
                "raw_metar": raw_metar,
                "metar_time_ago": get_elapsed_str(raw_metar).replace(' (', '').replace(')', '').strip() if raw_metar else '',
                "raw_taf": raw_taf,
                "taf_time_ago": get_elapsed_str(raw_taf).replace(' (', '').replace(')', '').strip() if raw_taf else '',
                "decoded": {
                    "windStr": m.get('windStr', 'N/A'),
                    "wind_speed_knots": m.get('windSpeed', 0),
                    "visibility": m.get('visibility', 'N/A'),
                    "weather": m.get('weather', 'NONE'),
                    "clouds": m.get('clouds', 'CLEAR'),
                    "temp": m.get('temp', 'N/A'),
                    "temperature_c": temp_c,
                    "dew": m.get('dew', 'N/A'),
                    "altimeter": m.get('altimeter', 'N/A'),
                    "flight_category": flight_cat
                },
                "decoded_metar": decoder.decode_metar(raw_metar),
                "decoded_taf": decoder.decode_taf(raw_taf),
                "last_updated": cached_data.get('last_updated', '')
            }
        else:
            results[icao] = {
                "icao": icao,
                "error": "Cache miss. Data not currently synced.",
                "decoded": {}
            }
            
    return jsonify({
        "status": "success",
        "results": results
    }), 200

@app.route('/api/station', methods=['GET'])
def api_station():
    """Frontend Javascript calls this to get detailed JSON for the modal."""
    icao = request.args.get('icao', '').strip().upper()
    force_refresh = request.args.get('refresh', 'false').lower() == 'true'
    
    if not icao or len(icao) != 4:
        return jsonify({"error": "Invalid ICAO code"}), 400

    # 1. Try checking the database first (Bypassed if user clicks 'Force Refresh')
    if not force_refresh:
        cached_data = get_cached_weather(icao)
        if cached_data:
            payload = dict(cached_data['decoded_data'])
            # Only keep the last three METARs
            payload['history'] = list(payload.get('history', []))[:3]
                
            return jsonify(payload)

    # 2. Cache Miss / Stale Data / Forced Refresh -> Execute legacy weather_engine.py
    try:
        live_result = get_station_details(icao) 
        
        # Adapt to existing get_station_details output structure
        raw_metar = live_result.get('history', [''])[0] if live_result.get('history') else ''
        raw_taf = live_result.get('raw_taf', '') 
        decoded_payload = live_result
        
        # Only keep the last three METARs
        decoded_payload['history'] = list(decoded_payload.get('history', []))[:3]
        
        # 3. Securely update the cache in the background for subsequent users
        payload_for_db = dict(decoded_payload)
        upsert_weather(icao, raw_metar, raw_taf, payload_for_db)
        
        return jsonify(decoded_payload)
    except Exception as e:
        return jsonify({"error": f"Failed to retrieve weather: {str(e)}"}), 500

# ==========================================
# 2. TELEGRAM BOT
# ==========================================
if bot:
    @bot.message_handler(commands=['start', 'help'])
    def send_welcome(message):
        bot.reply_to(message, "✈️ Welcome! Text me any ICAO airport codes (like 'CYYZ' or 'KLAX KJFK') and I will instantly fetch the decoded weather for you!")

    @bot.message_handler(func=lambda message: True)
    def handle_telegram_messages(message):
        user_text = message.text
        pilot_name = message.from_user.first_name or "A Pilot"
        username = f"(@{message.from_user.username})" if message.from_user.username else ""
        
        print(f"\n[TELEGRAM] {pilot_name} {username} requested: {user_text}", flush=True)
        try:
            raw_list = user_text.replace(",", " ").split()
            stations_list = []
            for s in raw_list:
                s_clean = s.strip().upper()
                if s_clean and s_clean not in stations_list:
                    stations_list.append(s_clean)
            if not stations_list:
                bot.reply_to(message, "Please provide at least one ICAO station code (e.g. 'VIDP').")
                return

            weather_data = ""
            for icao in stations_list:
                cached_data = get_cached_weather(icao)
                if cached_data:
                    c = cached_data['decoded_data']
                    m = c.get('model', {})
                    md = f"### 📍 {c.get('icao', icao)} | {c.get('name', c.get('station_name', 'Unknown Station'))} | {c.get('coords', c.get('coords_str', ''))} | {c.get('sun', c.get('sun_str', ''))}\n\n"
                    
                    history = c.get('history', [])
                    raw_metar = history[0] if history else ""
                    if raw_metar:
                        metar_elapsed = get_elapsed_str(raw_metar)
                        md += f"✈️ **METAR**{metar_elapsed}\n```\n{raw_metar}\n```\n\n"
                    else:
                        md += f"✈️ *METAR*\n_No recent METAR data available._\n\n"
                        
                    raw_taf = cached_data.get('raw_taf', '')
                    if raw_taf:
                        taf_elapsed = get_elapsed_str(raw_taf)
                        md += f"📅 **TAF**{taf_elapsed}\n```\n{raw_taf}\n```\n\n"
                    else:
                        md += f"📅 **TAF**\n_No recent TAF forecast available._\n\n"
                        
                    md += "\n\n📖 **Official Decode:**\n"
                    dec_metar = decoder.decode_metar(raw_metar)
                    if dec_metar:
                        md += f"  🔹 **Wind**: {dec_metar.get('wind', 'N/A')}\n"
                        md += f"  🔹 **Visibility**: {dec_metar.get('visibility', 'N/A')}\n"
                        md += f"  🔹 **Weather**: {dec_metar.get('weather', 'NONE')}\n"
                        md += f"  🔹 **Clouds**: {dec_metar.get('clouds', 'CLEAR')}\n"
                        md += f"  🔹 **Temp**: {dec_metar.get('temp', 'N/A')} | **Dew**: {dec_metar.get('dew', 'N/A')}\n"
                        md += f"  🔹 **Altimeter**: {dec_metar.get('altimeter', 'N/A')}\n"
                    
                    dec_taf = decoder.decode_taf(raw_taf)
                    if dec_taf:
                        md += "\n📅 **TAF Forecast Periods:**\n"
                        init = dec_taf["initial"]
                        md += f"  ⏳ **Initial Forecast** ({init.get('period', 'N/A')}):\n"
                        md += f"      Wind: {init.get('wind', 'N/A')} | Vis: {init.get('visibility', 'N/A')}\n"
                        md += f"      Wx: {init.get('weather', 'None')} | Clouds: {init.get('clouds', 'Clear')}\n"
                        for chg in dec_taf.get("changes", []):
                            md += f"  ⏳ **{chg.get('type')}** ({chg.get('period', 'N/A')}):\n"
                            if chg.get('wind') != "N/A": md += f"      Wind: {chg.get('wind')}\n"
                            if chg.get('visibility') != "N/A": md += f"      Vis: {chg.get('visibility')}\n"
                            if chg.get('weather') != "None": md += f"      Wx: {chg.get('weather')}\n"
                            if chg.get('clouds') != "Clear": md += f"      Clouds: {chg.get('clouds')}\n"
                    md += "\n"
                    weather_data += md
                else:
                    weather_data += get_instant_weather(icao) + "\n\n"
            
            # Telegram limit is 4096. We split at 4000 to be safe.
            if len(weather_data) > 4000:
                chunks = [weather_data[i:i+4000] for i in range(0, len(weather_data), 4000)]
                for chunk in chunks:
                    try:
                        bot.reply_to(message, chunk, parse_mode="Markdown")
                    except:
                        bot.reply_to(message, chunk) # Fallback if markdown is broken in chunk
            else:
                try:
                    bot.reply_to(message, weather_data, parse_mode="Markdown")
                except telebot.apihelper.ApiTelegramException:
                    bot.reply_to(message, weather_data) 
                    
        except Exception as e:
            bot.reply_to(message, f"Sorry, I ran into an error: {str(e)}")

import threading
import time

# ==========================================
# 3. TELEGRAM WEBHOOK ROUTE (Production)
# ==========================================
if bot:
    # Use the token as a hidden URL path so random internet scanners can't hit it
    @app.route('/' + TELEGRAM_TOKEN, methods=['POST'])
    def telegram_webhook():
        if request.headers.get('content-type') == 'application/json':
            json_string = request.get_data().decode('utf-8')
            update = telebot.types.Update.de_json(json_string)
            bot.process_new_updates([update])
            return "!", 200
        return "Invalid request", 403

# ==========================================
# APP EXECUTION & SMART TOGGLE
# ==========================================
if __name__ == "__main__":
    port = int(os.environ.get('PORT', 5000))
    
    if bot:
        # 1. RENDER PRODUCTION (Webhook Mode)
        if os.environ.get('RENDER'):
            print("[TELEGRAM] Render production detected. Configuring Webhooks...", flush=True)
            # Render automatically provides your app's base URL in this environment variable
            base_url = os.environ.get('RENDER_EXTERNAL_URL')
            webhook_url = f"{base_url}/{TELEGRAM_TOKEN}"
            
            # Clear old configurations and set the live webhook
            bot.remove_webhook()
            time.sleep(1) 
            bot.set_webhook(url=webhook_url)
            print(f"[TELEGRAM] Webhook successfully bound to {base_url}", flush=True)

        # 2. LOCAL DEVELOPMENT (Polling Mode)
        else:
            print("[TELEGRAM] Local environment detected. Starting infinite polling...", flush=True)
            # CRITICAL: You must remove the webhook first, otherwise Telegram blocks polling!
            bot.remove_webhook()
            time.sleep(1)
            threading.Thread(
                target=bot.infinity_polling, 
                kwargs={"timeout": 10, "long_polling_timeout": 5}, 
                daemon=True
            ).start()

    # Start the Flask web server
    app.run(host="0.0.0.0", port=port)
