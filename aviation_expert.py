import os
import requests
import google.generativeai as genai
from dotenv import load_dotenv

load_dotenv()
genai.configure(api_key=os.environ.get("GEMINI_API_KEY"))

# ---------------------------------------------------------
# THE TOOL
# ---------------------------------------------------------
def get_aviation_weather(stations: str) -> str:
    """Fetches live METAR and TAF for a comma-separated list of ICAO airport codes (e.g., 'KLAX, KJFK, CYYZ')."""
    print(f"\n  📡 [Tool Execution] Accessing AviationWeather.gov for: {stations}...")
    clean_stations = stations.replace(" ", "") # Fix the spaces issue
    
    result_text = ""
    try:
        # 1. Fetch METAR
        metar_url = f"https://aviationweather.gov/api/data/metar?ids={clean_stations}&format=json"
        metar_response = requests.get(metar_url).json()
        
        if isinstance(metar_response, list):
            for m in metar_response:
                result_text += f"METAR for {m.get('icaoId')}: {m.get('rawOb')}\n"
        
        # 2. Fetch TAF
        taf_url = f"https://aviationweather.gov/api/data/taf?ids={clean_stations}&format=json"
        taf_response = requests.get(taf_url).json()
        
        if isinstance(taf_response, list):
            for t in taf_response:
                result_text += f"TAF for {t.get('icaoId')}: {t.get('rawTAF')}\n"
                
        if not result_text:
            return "No weather data found for those stations. Please check the ICAO codes."
            
        return result_text
        
    except Exception as e:
        return f"Error fetching weather data: {str(e)}"

# ---------------------------------------------------------
# THE AGENT
# ---------------------------------------------------------
model = genai.GenerativeModel(
    model_name='gemini-2.5-flash',
    tools=[get_aviation_weather],
    system_instruction="""You are an expert, friendly flight dispatcher. The user will ask for weather at various airports. 
    1. Use your get_aviation_weather tool to fetch the raw METAR and TAF.
    2. Read the raw data and translate it into a highly readable, clear summary for a pilot.
    3. Always show the RAW METAR and RAW TAF strings first, then provide your translation below it.
    4. Group the information by airport if they ask for multiple."""
)

def start_interactive_session():
    print("\n==================================================")
    print("✈️  AVIATION WEATHER AGENT (METAR & TAF)")
    print("==================================================")
    print("Type one or more ICAO codes (e.g. 'CYYZ' or 'KLAX, KJFK, EGLL').")
    print("Type 'exit' to quit.")
    
    chat = model.start_chat(enable_automatic_function_calling=True)
    
    while True:
        user_input = input("\n📍 Enter Airports: ")
        if user_input.lower() in ['exit', 'quit']:
            break
            
        try:
            print("🤖 Agent is thinking...")
            response = chat.send_message(user_input)
            print("\n" + "="*50)
            print(response.text)
            print("="*50)
        except Exception as e:
            print(f"An error occurred: {e}")

if __name__ == "__main__":
    start_interactive_session()
