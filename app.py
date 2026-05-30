import os
import telebot
import time
import requests
from flask import Flask, request, jsonify, render_template
from weather_engine import get_instant_weather, get_station_details
from db_manager import get_cached_weather, upsert_weather
from datetime import datetime, timezone
from dotenv import load_dotenv

# Load secret keys from .env
load_dotenv()
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")

# Initialize Telegram Bot (WebHook Mode)
bot = telebot.TeleBot(TELEGRAM_TOKEN, threaded=False)

# Initialize the Flask Web Server
app = Flask(__name__)

# ==========================================
# ROBUST API FETCHER
# ==========================================
def robust_get(url, timeout=5, retries=2, as_json=False):
    """Fetches URLs with exponential backoff to defeat flaky network timeouts."""
    backoff = 1
    for attempt in range(retries + 1):
        try:
            resp = requests.get(url, timeout=timeout)
            resp.raise_for_status()
            return resp.json() if as_json else resp.text
        except Exception:
            if attempt == retries:
                return None
            time.sleep(backoff)
            backoff *= 2
    return None

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
        
    # Bulk fetch TAFs for the requested stations with robust retries
    taf_dict = {}
    clean_stations = ",".join(stations_list)
    taf_url = f"https://aviationweather.gov/api/data/taf?ids={clean_stations}&format=json"
    tafs = robust_get(taf_url, timeout=8, retries=2, as_json=True)
    
    if tafs:
        for t in tafs:
            t_icao = t.get('icaoId', '').upper()
            raw_taf = t.get('rawTAF', '')
            issue_time = t.get('issueTime', 'N/A')
            if t_icao and raw_taf:
                clean_taf = raw_taf.strip()
                if clean_taf.upper().startswith('TAF'):
                    clean_taf = clean_taf[3:].strip()
                issue_time_formatted = str(issue_time).replace('T', ' ').replace('Z', ' UTC')
                taf_dict[t_icao] = (clean_taf, issue_time_formatted)
        
    result_text = ""
    for icao in stations_list:
        cached_data = get_cached_weather(icao)
        if cached_data:
            c = cached_data['decoded_data']
            m = c.get('model', {})
            
            # Reconstruct the exact Markdown expected by index.html from the cache
            md = f"### 📍 {c.get('icao', icao)} | {c.get('name', 'Unknown Station')} | {c.get('coords', '')} | {c.get('sun', '')}\n\n"
            
            history = c.get('history', [])
            raw_metar = history[0] if history else ""
            if raw_metar:
                md += f"✈️ **METAR**\n```\n{raw_metar}\n```\n\n"
            else:
                md += f"✈️ *METAR*\n_No recent METAR data available._\n\n"
                
            # Inject dynamically fetched live TAF
            if icao in taf_dict:
                clean_taf, issue_time = taf_dict[icao]
                md += f"📅 **TAF** (Issued: {issue_time})\n```\n{clean_taf}\n```\n\n"
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
            # Fallback to slow legacy scrape if not cached
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
            payload = cached_data['decoded_data']
            
            # Dynamically fetch 3-hour history + TAF for the modal with robust retries
            hist_url = f"https://aviationweather.gov/api/data/metar?ids={icao}&format=raw&hours=4"
            hist_text = robust_get(hist_url, timeout=4, retries=1)
            if hist_text:
                hist_lines = [l.strip() for l in hist_text.strip().split('\n') if l.strip()]
                if hist_lines:
                    payload['history'] = hist_lines[:3]
            
            taf_url = f"https://aviationweather.gov/api/data/taf?ids={icao}&format=raw"
            taf_text = robust_get(taf_url, timeout=4, retries=1)
            if taf_text and taf_text.strip():
                payload['history'].insert(0, f"TAF {taf_text.strip()}")
                
            return jsonify(payload)

    # 2. Cache Miss / Stale Data / Forced Refresh -> Execute legacy weather_engine.py
    try:
        live_result = get_station_details(icao) 
        
        # Adapt to existing get_station_details output structure
        raw_metar = live_result.get('history', [''])[0] if live_result.get('history') else ''
        raw_taf = ''
        decoded_payload = live_result
        
        # Dynamically inject TAF and 3-hour history to live scrape too
        hist_url = f"https://aviationweather.gov/api/data/metar?ids={icao}&format=raw&hours=4"
        hist_text = robust_get(hist_url, timeout=4, retries=1)
        if hist_text:
            hist_lines = [l.strip() for l in hist_text.strip().split('\n') if l.strip()]
            if hist_lines:
                decoded_payload['history'] = hist_lines[:3]
        
        taf_url = f"https://aviationweather.gov/api/data/taf?ids={icao}&format=raw"
        taf_text = robust_get(taf_url, timeout=4, retries=1)
        if taf_text and taf_text.strip():
            decoded_payload['history'].insert(0, f"TAF {taf_text.strip()}")
        
        # 3. Securely update the cache in the background for subsequent users
        upsert_weather(icao, raw_metar, raw_taf, decoded_payload)
        
        return jsonify(decoded_payload)
    except Exception as e:
        return jsonify({"error": f"Failed to retrieve weather: {str(e)}"}), 500

# ==========================================
# 2. TELEGRAM WEBHOOK ENDPOINT
# ==========================================
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
        weather_data = get_instant_weather(user_text)
        
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
