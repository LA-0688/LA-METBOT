import os
import telebot
from flask import Flask, request, jsonify, render_template
from weather_engine import get_instant_weather
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
