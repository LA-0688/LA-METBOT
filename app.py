import os
import telebot
from flask import Flask, request, jsonify, render_template
from weather_engine import get_instant_weather, get_station_details
from db_manager import get_cached_weather, upsert_weather, get_weather_batch
import re
from datetime import datetime, timezone

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

@app.route("/api/weather", methods=['GET'])
def api_weather():
    """Frontend Javascript calls this to get the weather text."""
    stations = request.args.get('stations', '')
    stations_list = [s.strip().upper() for s in stations.replace(",", " ").split() if s.strip()]
    
    if not stations_list:
        return jsonify({"text": "Please provide at least one station code."})
        
    result_text = ""
    cached_batch = get_weather_batch(stations_list)
    
    for icao in stations_list:
        cached_data = cached_batch.get(icao)
        if cached_data:
            c = cached_data['decoded']
            m = c.get('model', {})
            
            # Reconstruct the exact Markdown expected by index.html from the cache
            md = f"### 📍 {c.get('icao', icao)} | {c.get('name', 'Unknown Station')} | {c.get('coords', '')} | {c.get('sun', '')}\n\n"
            
            history = c.get('history', [])
            raw_metar = history[0] if history else ""
            if raw_metar:
                metar_elapsed = get_elapsed_str(raw_metar)
                md += f"✈️ **METAR**{metar_elapsed}\n```\n{raw_metar}\n```\n\n"
            else:
                md += f"✈️ *METAR*\n_No recent METAR data available._\n\n"
                
            # Read TAF directly from database
            raw_taf = cached_data.get('raw_taf', '')
            if raw_taf:
                taf_elapsed = get_elapsed_str(raw_taf)
                md += f"📅 **TAF**{taf_elapsed}\n```\n{raw_taf}\n```\n\n"
            else:
                md += f"📅 **TAF**\n_No recent TAF forecast available._\n\n"
                
            md += "*Decoded:*\n"
            md += f"  🔹 **Wind**: {m.get('windStr', 'N/A')}\n"
            md += f"  🔹 **Visibility**: {m.get('visibility', 'N/A')}\n"
            md += f"  🔹 **Weather**: {m.get('weather', 'NONE')}\n"
            md += f"  🔹 **Clouds**: {m.get('clouds', 'CLEAR')}\n"
            md += f"  🔹 **Temp**: {m.get('temp', 'N/A')} | **Dew**: {m.get('dew', 'N/A')}\n"
            md += f"  🔹 **Altimeter**: {m.get('altimeter', 'N/A')}\n\n"
            
            result_text += md
        else:
            # Fallback to slow legacy scrape if not cached (This natively handles Indian govt SSL bypass)
            result_text += get_instant_weather(icao) + "\n\n"
            
    return jsonify({"text": result_text})

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
        raw_taf = '' # TAF is not exposed by get_station_details currently
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
# 2. TELEGRAM WEBHOOK ENDPOINT
# ==========================================
if bot:
    @app.route(f"/{TELEGRAM_TOKEN}", methods=['POST'])
    def telegram_webhook():
        """Telegram servers will send POST requests here when someone texts the bot."""
        if request.headers.get('content-type') == 'application/json':
            json_string = request.get_data().decode('utf-8')
            update = telebot.types.Update.de_json(json_string)
            bot.process_new_updates([update])
            return "OK", 200
        return "Forbidden", 403

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
            stations_list = [s.strip().upper() for s in user_text.replace(",", " ").split() if s.strip()]
            if not stations_list:
                bot.reply_to(message, "Please provide at least one ICAO station code (e.g. 'VIDP').")
                return

            weather_data = ""
            for icao in stations_list:
                cached_data = get_cached_weather(icao)
                if cached_data:
                    c = cached_data['decoded_data']
                    m = c.get('model', {})
                    md = f"### 📍 {c.get('icao', icao)} | {c.get('station_name', c.get('name', 'Unknown Station'))} | {c.get('coords_str', '')} | {c.get('sun_str', '')}\n\n"
                    
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
                        
                    md += "*Decoded:*\n"
                    md += f"  🔹 **Wind**: {m.get('windStr', 'N/A')}\n"
                    md += f"  🔹 **Visibility**: {m.get('visibility', 'N/A')}\n"
                    md += f"  🔹 **Weather**: {m.get('weather', 'NONE')}\n"
                    md += f"  🔹 **Clouds**: {m.get('clouds', 'CLEAR')}\n"
                    md += f"  🔹 **Temp**: {m.get('temp', 'N/A')} | **Dew**: {m.get('dew', 'N/A')}\n"
                    md += f"  🔹 **Altimeter**: {m.get('altimeter', 'N/A')}\n\n"
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

if __name__ == "__main__":
    # When deployed on Render, the PORT is provided by the environment
    port = int(os.environ.get('PORT', 5000))
    app.run(host="0.0.0.0", port=port)
