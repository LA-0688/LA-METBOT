import os
import time
import requests
import schedule
import google.generativeai as genai
from dotenv import load_dotenv

# 1. Load keys and configure
load_dotenv()
genai.configure(api_key=os.environ.get("GEMINI_API_KEY"))

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# 2. Choose your stations here! (Comma separated ICAO codes)
# e.g., CYYZ = Toronto, KJFK = New York, KLAX = Los Angeles, EGLL = London Heathrow
STATIONS = "CYYZ, KJFK, KLAX" 

# Dictionary to remember the last time we saw a METAR so we don't spam you
last_seen_metar_times = {}

# 3. Create the Agent
meteorologist_agent = genai.GenerativeModel(
    model_name='gemini-2.5-flash',
    system_instruction="You are an expert aviation meteorologist. The user will give you a raw METAR string. Translate it into a friendly, easy-to-read text message format. Include emojis. Be concise but include wind, visibility, weather, and temperature."
)

def send_telegram_message(text):
    """Sends a text message to your phone via Telegram"""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID, 
        "text": text,
        "parse_mode": "Markdown" # Allows bolding and formatting
    }
    requests.post(url, json=payload)

def check_for_weather_updates():
    print(f"\n[{time.strftime('%X')}] Checking AviationWeather API for {STATIONS}...")
    try:
        # Fetch live JSON data from the US Government Aviation API
        clean_stations = STATIONS.replace(" ", "")
        url = f"https://aviationweather.gov/api/data/metar?ids={clean_stations}&format=json"
        response = requests.get(url).json()
        
        # If the API returned an error dict instead of a list of weather data, print it and stop
        if not isinstance(response, list):
            print(f"API Error: {response}")
            return
            
        for station in response:
            icao = station.get("icaoId")
            raw_metar = station.get("rawOb")
            obs_time = station.get("obsTime") # This is a timestamp
            
            # Check if this is a NEW report we haven't seen yet
            if icao not in last_seen_metar_times or last_seen_metar_times[icao] != obs_time:
                print(f"  🆕 New update found for {icao}! Giving it to the Agent...")
                last_seen_metar_times[icao] = obs_time
                
                # Have the agent translate it
                agent_analysis = meteorologist_agent.generate_content(f"Translate this METAR: {raw_metar}").text
                
                # Format the message
                phone_message = f"✈️ *WEATHER UPDATE: {icao}*\n\n{agent_analysis}\n\n_Raw: {raw_metar}_"
                
                # Send it to your phone!
                send_telegram_message(phone_message)
                print(f"  ✅ Sent text message to your phone for {icao}!")
            else:
                print(f"  (No new updates for {icao})")
                
    except Exception as e:
        print(f"Error checking weather: {e}")

if __name__ == "__main__":
    print("==================================================")
    print("🚀 Proactive METAR Agent Started!")
    print(f"Monitoring: {STATIONS}")
    print("==================================================")
    
    # Send a startup message to your phone
    send_telegram_message("🤖 Your METAR Agent is now online and watching the skies! ☁️")
    
    # 1. Run it once immediately
    check_for_weather_updates()
    
    # 2. Schedule it to run automatically.
    # METARs usually update once an hour, but "SPECI" (special updates) can happen anytime.
    # We will check every 5 minutes just to be safe.
    schedule.every(5).minutes.do(check_for_weather_updates)
    
    print("\nWaiting in the background... (Press Ctrl+C to stop)")
    
    # 3. Keep the script running forever
    while True:
        schedule.run_pending()
        time.sleep(1)
