import os
import telebot
import time
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
    weather_data = get_instant_weather(stations)
    return jsonify({"text": weather_data})

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
            # High-speed match found! Return the JSON payload directly
            # We return the exact decoded_data structure so frontend modal doesn't break
            return jsonify(cached_data['decoded_data'])

    # 2. Cache Miss / Stale Data / Forced Refresh -> Execute legacy weather_engine.py
    try:
        live_result = get_station_details(icao) 
        
        # Adapt to existing get_station_details output structure
        raw_metar = live_result.get('history', [''])[0] if live_result.get('history') else ''
        raw_taf = '' # TAF is not exposed by get_station_details currently
        decoded_payload = live_result
        
        # 3. Securely update the cache in the background for subsequent users
        upsert_weather(icao, raw_metar, raw_taf, decoded_payload)
        
        return jsonify(decoded_payload)
    except Exception as e:
        return jsonify({"error": f"Failed to retrieve weather: {str(e)}"}), 500

# ==========================================
# 2. TELEGRAM WEBHOOK ENDPOINT
# ==========================================
# We use the token in the URL so only Telegram knows where to send data securely
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
