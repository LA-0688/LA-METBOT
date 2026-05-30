# ✈️ Aviation Weather Expert & Telegram Bot

This project is a lightning-fast aviation weather decoding engine that powers both a **Flask web frontend** and a **Telegram Bot**. It intelligently fetches and decodes METAR, TAF, and D-ATIS reports for any ICAO airport code worldwide, with specialized multi-tier fallback support for Indian stations (AAI and AMSS).

## 🌟 Key Features

### 1. Robust Weather Engine (`weather_engine.py`)
- **Instant Decoding**: Decodes complex METAR and TAF strings into human-readable text instantly.
- **Multi-Source Fetching**: Concurrently fetches data from:
  - AviationWeather API (NOAA)
  - Live Airports Authority of India (AAI) Domestic Streams (Tier 1A)
  - AMSS Delhi Regional Nodes (Tier 1A)
  - CheckWX and AVWX REST APIs (Tier 2/3)
  - Ogimet (Tier 1B last resort)
  - D-ATIS via Clowd API
- **Smart Fallback & Stale Data Detection**: Detects if primary data is older than 2 hours and automatically triggers fallback web-scraping to find the latest valid observation.
- **Coordinate & Sun Tracking**: Maps ICAO codes to coordinates, calculating real-time Sunrise/Sunset based on latitude and longitude using Sunrise-Sunset API.
- **TLS & Certificate Bypasses**: Custom HTTP adapters to bypass SSL handshake errors (DH_KEY_TOO_SMALL) frequently encountered on older government servers (e.g., AAI, AMSS).
- **Parallel Processing**: Uses `ThreadPoolExecutor` for ultra-fast, concurrent API calls and HTML scraping.

### 2. Telegram Bot Interface (`telegram_aviation_bot.py` / `app.py`)
- **Instant Replies**: Send any ICAO code (e.g., `CYYZ`, `KLAX`, `VIDP`) to the bot, and it instantly responds with decoded weather.
- **Markdown Formatting**: Formats the weather data beautifully with emojis, keeping the raw METAR block separate for easy copying.
- **WebHook Support**: Can run locally via polling (`telegram_aviation_bot.py`) or via Webhooks on a Flask server (`app.py`).

### 3. Flask Web Application (`app.py`)
- **Frontend Serving**: Serves an interactive website frontend (HTML/JS) via `/`.
- **API Endpoints**: 
  - `/api/weather?stations=...` : Returns decoded text.
  - `/api/station?icao=...` : Returns detailed JSON for the frontend modal.
- **Telegram Webhook Endpoint**: Secures Telegram webhook routing using the token in the URL.

### 4. Modular AI Agents (Experimental)
- **`aviation_expert.py` & `metar_agent.py`**: Experimental agents for interacting with AI (like GPT/LangChain) for more complex natural language understanding.
- **`smart_team.py` / `team.py`**: Multi-agent framework configurations.

## 🚀 Setup & Execution

1. **Install Dependencies**:
   ```bash
   pip install -r requirements.txt
   ```
2. **Environment Variables**:
   Create a `.env` file based on `.env.example`:
   ```env
   TELEGRAM_BOT_TOKEN="your-telegram-token"
   CHECKWX_API_KEY="your-checkwx-key"
   AVWX_API_KEY="your-avwx-key"
   ```
3. **Run the Telegram Bot Locally**:
   ```bash
   python telegram_aviation_bot.py
   ```
4. **Run the Flask Web App**:
   ```bash
   python app.py
   ```

## 🧠 How the Decoding Works
- **Visibility**: Automatically converts Statute Miles (SM) for US stations and Meters/Kilometers for international stations.
- **Clouds**: Decodes covers (FEW, SCT, BKN, OVC, CAVOK) and bases into thousands of feet.
- **Present Weather**: Uses a dictionary mapping to decode abbreviations like `-SN`, `+TSRA`, `BR`, `FG`, `HZ`, `VCTS` into readable English (e.g., "Light Snow", "Heavy Thunderstorm Rain", "Mist").
- **Flight Category**: Retrieves Flight Rules (VFR, MVFR, IFR, LIFR).
