import time
from db_manager import upsert_weather
from weather_engine import get_station_details

# Add your critical target airports here (e.g., core Indian airports and major global hubs)
CORE_STATIONS = ["VIDP", "VABB", "VECC", "VOBL", "VAAH", "VOMM", "KJFK", "EGLL"]

def cron_sync_job():
    print(f"Starting scheduled weather refresh loop for {len(CORE_STATIONS)} airports...")
    
    for icao in CORE_STATIONS:
        try:
            print(f"Syncing: {icao}")
            live_result = get_station_details(icao)
            
            # Adapt to existing get_station_details output structure
            raw_metar = live_result.get('history', [''])[0] if live_result.get('history') else ''
            raw_taf = '' # TAF is not exposed by get_station_details currently
            decoded_payload = live_result
            
            upsert_weather(
                icao=icao,
                raw_metar=raw_metar,
                raw_taf=raw_taf,
                decoded_json=decoded_payload
            )
            time.sleep(1) # Gentle cooldown padding between operations
        except Exception as e:
            print(f"Error syncing {icao}: {e}")
    print("Sync loop completed successfully.")

if __name__ == "__main__":
    print("[SYNC PROCESS] Background worker initialized. Starting permanent loop...", flush=True)
    while True:
        try:
            cron_sync_job()
        except Exception as e:
            print(f"[SYNC PROCESS] Fatal error in loop: {e}", flush=True)
        
        print("[SYNC PROCESS] Sleeping for 25 minutes before next run...", flush=True)
        time.sleep(25 * 60)  # Wait 25 minutes before running again
