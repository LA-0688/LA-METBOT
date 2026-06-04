# Aviation Weather Expert & Telegram Bot

## Project Description
This project is a highly capable aviation weather decoding engine that serves both a **Flask web frontend** and a **Telegram Bot**. It intelligently fetches and decodes METAR, TAF, and D-ATIS reports for any ICAO airport code globally, with specialized support and multi-tier fallbacks for Indian stations (AAI and AMSS).

## Key Features

### 1. Robust Weather Engine
- **Instant Decoding:** Parses complex METAR and TAF strings into human-readable text instantly.
- **Multi-Source Data Fetching:** Concurrently retrieves data from various aviation sources:
  - AviationWeather API (NOAA)
  - Live Airports Authority of India (AAI) Domestic Streams (Tier 1A)
  - AMSS Delhi Regional Nodes (Tier 1A)
  - CheckWX and AVWX REST APIs (Tier 2/3)
  - Ogimet (Tier 1B last resort)
  - D-ATIS via Clowd API
- **Smart Fallback & Stale Data Detection:** Automatically detects if primary data is older than 2 hours and triggers fallback web-scraping to guarantee the latest valid observation.
- **Coordinate & Sun Tracking:** Calculates real-time Sunrise/Sunset based on latitude and longitude using the Sunrise-Sunset API.
- **Custom SSL/TLS Bypasses:** Custom HTTP adapters to bypass SSL handshake errors typically encountered on older government servers (e.g., AAI, AMSS).
- **Parallel Processing:** Uses `ThreadPoolExecutor` for concurrent API calls and HTML scraping to ensure ultra-fast data retrieval.

### 2. Telegram Bot Interface
- **Instant Replies:** Users can send any ICAO code (e.g., `VIDP`, `KJFK`) to receive instantly decoded weather.
- **Markdown Formatting:** Neatly formatted weather data with emojis, separating the raw METAR block for easy copying.
- **Flexible Hosting:** Capable of running locally via polling or via Webhooks on the Flask server.

### 3. Flask Web Application
- **Interactive Web Interface:** Serves a beautiful, interactive frontend website (HTML/JS) for querying aviation weather.
- **Developer API Endpoints:** 
  - `/api/weather?stations=...` for retrieving decoded JSON data.
  - `/api/station?icao=...` for detailed station information used by the frontend modal.
- **Integrated Webhooks:** Secures Telegram webhook routing.

### 4. Advanced Decoding Capabilities
- **Visibility Parsing:** Converts Statute Miles (SM) and Meters/Kilometers intelligently based on the station.
- **Cloud Covers:** Decodes abbreviations like FEW, SCT, BKN, OVC, and CAVOK, converting bases into thousands of feet.
- **Present Weather:** Translates complex abbreviations (e.g., `-SN`, `+TSRA`, `VCTS`) into plain English ("Light Snow", "Heavy Thunderstorm Rain").
- **Flight Category Determination:** Automatically calculates Flight Rules (VFR, MVFR, IFR, LIFR) based on visibility and ceilings.

### 5. Modular AI Agents (Experimental)
- Incorporates experimental agents (`aviation_expert.py`, `metar_agent.py`) for interacting with AI (like GPT/LangChain) for advanced natural language understanding and multi-agent framework integration.

## Architecture & Technical Stack (For LLM Context)

To guide code changes, the following architectural details are essential:

### 1. File Structure
- **`app.py`**: The main entry point. Initializes the Flask server and the Telegram Bot (via `telebot`). Contains API endpoints (`/api/weather`, `/api/station`) and the core Telegram message handler (`handle_telegram_messages`).
- **`weather_engine.py`**: The heavy lifter for data fetching. Contains `get_instant_weather()` and `get_station_details()`. Implements concurrent fetching via `ThreadPoolExecutor` and custom SSL adapters.
- **`decoder.py`**: The parsing logic. Contains functions like `decode_metar()` and `decode_taf()` that convert raw string data into structured dictionaries.
- **`db_manager.py`**: Handles database interactions using `psycopg` (PostgreSQL) and `psycopg_pool`. Contains functions like `get_cached_weather()` and `upsert_weather()` for caching responses and minimizing API calls.
- **`sync_weather.py`**: A standalone script (likely run as a cron job or background process) to synchronize and mass-update weather data into the database.

### 2. Data Flow
1. **Request Initiation**: User sends an ICAO code via Web UI (`app.py` `/api/station`) or Telegram (`app.py` `@bot.message_handler`).
2. **Cache Check**: System queries PostgreSQL (`db_manager.py`) to see if fresh data (< 30 mins old) exists.
3. **Data Fetching (If Cache Miss)**: `weather_engine.py` parallel-fetches data from primary sources (AviationWeather, AAI). If primary fails, it falls back to Tier 2/3 APIs.
4. **Decoding & Storage**: Raw data is decoded (`decoder.py`) into structured JSON and upserted back into the database (`db_manager.py`).
5. **Response**: Formatted markdown (Telegram) or JSON (Web) is returned to the user.

### 3. Key Dependencies
- **Web Framework**: Flask
- **Telegram Bot API**: `pyTelegramBotAPI` (`telebot`)
- **Database**: PostgreSQL (`psycopg`, `psycopg-pool`), utilizing `jsonb` for flexible data storage.
- **Concurrency**: `concurrent.futures.ThreadPoolExecutor`
