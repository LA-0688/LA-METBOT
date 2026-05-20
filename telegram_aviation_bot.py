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
        try:
            if "📅 **TAF**" in weather_data:
                # Split into METAR (including everything before TAF) and TAF sections
                metar_part, taf_part = weather_data.split("\n📅 **TAF**", 1)
            # If a METAR block is present, extract it and send it separately
            metar_header = "✈️ **METAR**"
            if metar_header in weather_data:
                # Find the start of the METAR block (including the header line)
                start_idx = weather_data.find(metar_header)
                # Find the first opening ``` after the header
                opening = weather_data.find("```", start_idx)
                # Find the closing ``` after the opening
                closing = weather_data.find("```", opening + 3)
                if opening != -1 and closing != -1:
                    # Include the closing backticks line (+3 chars)
                    raw_metar_msg = weather_data[start_idx:closing + 3]
                    # The rest of the message is everything before the METAR block plus everything after it
                    remaining_msg = weather_data[:start_idx] + weather_data[closing + 3:]
                    # Send the raw METAR block first (it will have its own Copy button)
                    bot.reply_to(message, raw_metar_msg, parse_mode="Markdown")
                    # Then send the remaining content (decoded METAR info, TAF, etc.)
                    if remaining_msg.strip():
                        bot.reply_to(message, remaining_msg, parse_mode="Markdown")
                else:
                    # Fallback: send the whole thing if we couldn't parse correctly
                    bot.reply_to(message, weather_data, parse_mode="Markdown")
            else:
                bot.reply_to(message, weather_data, parse_mode="Markdown")
        except telebot.apihelper.ApiTelegramException:
            # If Telegram's strict Markdown parser fails, send as plain text
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
