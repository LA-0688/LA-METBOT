import os
import requests
import telebot
from dotenv import load_dotenv

# Load keys
load_dotenv()
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")

from weather_engine import get_instant_weather

# Initialize the Telegram Bot
bot = telebot.TeleBot(TELEGRAM_TOKEN)

# ---------------------------------------------------------
# TELEGRAM LISTENERS
# ---------------------------------------------------------
@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    bot.reply_to(message, "✈️ Welcome! Text me any ICAO airport codes (like 'CYYZ' or 'KLAX, KJFK') and I will instantly fetch the decoded weather for you!")

@bot.message_handler(func=lambda message: True)
def handle_all_messages(message):
    user_text = message.text
    print(f"\n📥 Received message: {user_text}")
    
    try:
        # INSTANT PATH: No AI, just pure Python decoding
        print("⚡ Fetching and decoding raw data instantly...")
        weather_data = get_instant_weather(user_text)
        
        # Send instantly
        try:
            bot.reply_to(message, weather_data, parse_mode="Markdown")
        except telebot.apihelper.ApiTelegramException:
            # If Telegram's strict Markdown parser fails, send it as plain text instead of crashing!
            bot.reply_to(message, weather_data)
        
        print("📤 Sent instant reply to Telegram!")
        
    except Exception as e:
        error_msg = str(e)
        print(f"Error: {error_msg}")
        bot.reply_to(message, f"Sorry, I ran into an error processing that request.\n\nError details: {error_msg}")

if __name__ == "__main__":
    print("==================================================")
    print("⚡ LIGHTNING FAST TELEGRAM BOT IS ONLINE!")
    print("Waiting for messages from your phone... (Press Ctrl+C to stop)")
    print("==================================================")
    bot.infinity_polling()
