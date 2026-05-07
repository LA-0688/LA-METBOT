import os
import requests
import telebot
from dotenv import load_dotenv

# Load keys
load_dotenv()
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")

# Initialize the Telegram Bot
bot = telebot.TeleBot(TELEGRAM_TOKEN)

def get_instant_weather(stations: str) -> str:
    """Fetches and decodes live METAR and TAF instantly using pure Python (No AI overhead)."""
    stations_list = [s.strip().upper() for s in stations.replace(",", " ").split() if s.strip()]
    if not stations_list:
        return "Please provide at least one station code."
        
    clean_stations = ",".join(stations_list)
    result_text = ""
    
    try:
        metar_url = f"https://aviationweather.gov/api/data/metar?ids={clean_stations}&format=json"
        metar_response = requests.get(metar_url).json()
        
        taf_url = f"https://aviationweather.gov/api/data/taf?ids={clean_stations}&format=json"
        taf_response = requests.get(taf_url).json()
        
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
            
            if station in metars_by_station:
                m = metars_by_station[station]
                raw_metar = m.get('rawOb', 'N/A')
                temp = m.get('temp', 'N/A')
                dewp = m.get('dewp', 'N/A')
                wdir = m.get('wdir', 'VRB' if m.get('wdir') == 0 else m.get('wdir', 'N/A'))
                wspd = m.get('wspd', 'N/A')
                vis = m.get('visib', 'N/A')
                clouds = m.get('clouds', [])
                wx = m.get('wxString', '')
                
                cloud_str = "Clear"
                if clouds:
                    cloud_str = ", ".join([f"{c.get('cover')} at {c.get('base', 0)*100} ft" for c in clouds])
                
                result_text += f"✈️ *METAR*\n_{raw_metar}_\n\n"
                result_text += f"🌡️ *Temp:* {temp}°C | *Dewpoint:* {dewp}°C\n"
                result_text += f"💨 *Winds:* {wdir}° at {wspd} knots\n"
                result_text += f"👁️ *Visibility:* {vis} miles\n"
                result_text += f"☁️ *Clouds:* {cloud_str}\n"
                if wx:
                    result_text += f"🌧️ *Weather:* {wx}\n"
                result_text += "\n"
            else:
                result_text += f"✈️ *METAR*\n_No METAR data available._\n\n"
                
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
                    clouds = fcst.get('clouds', [])
                    wx = fcst.get('wxString', '')
                    
                    cloud_str = "Clear"
                    if clouds:
                        cloud_str = ", ".join([f"{c.get('cover')} at {c.get('base', 0)*100} ft" if c.get('base') else f"{c.get('cover')}" for c in clouds])
                    
                    result_text += f"  🔹 **{change}**: Wind {wdir}° at {wspd}kt, Vis {vis} miles, {cloud_str}"
                    if wx:
                        result_text += f", Wx: {wx}"
                    result_text += "\n"
                result_text += "\n"
            else:
                result_text += f"📅 *TAF*\n_No TAF forecast available._\n\n"
                
            # --- ATIS BLOCK ---
            try:
                atis_url = f"https://datis.clowd.io/api/{station}"
                atis_resp = requests.get(atis_url, timeout=3)
                if atis_resp.status_code == 200:
                    atis_data = atis_resp.json()
                    if isinstance(atis_data, list) and atis_data:
                        # Sometimes airports have multiple ATIS (Arr/Dep), we'll grab the first or combine them
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
