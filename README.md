---
title: Aviation Metar
emoji: ✈️
colorFrom: blue
colorTo: green
sdk: docker
app_port: 7860
pinned: false
---

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
- **Legacy TLS Support**: Custom HTTP adapters relax the cipher security level (to fix DH_KEY_TOO_SMALL handshake errors on older government servers like AAI/AMSS) **while keeping certificate and hostname verification enabled**.
- **Parallel Processing**: Uses `ThreadPoolExecutor` for ultra-fast, concurrent API calls and HTML scraping.

### 2. Telegram Bot Interface (`app.py`)
- **Instant Replies**: Send any ICAO code (e.g., `CYYZ`, `KLAX`, `VIDP`) to the bot, and it instantly responds with decoded weather.
- **Markdown Formatting**: Formats the weather data beautifully with emojis, keeping the raw METAR block separate for easy copying.
- **Long Polling**: The bot runs via long polling in a background thread started by `app.py` (set `RUN_BOT=0` to disable, e.g. if you run more than one Gunicorn worker).

### 3. Flask Web Application (`app.py`)
- **Frontend Serving**: Serves an interactive website frontend (HTML/JS) via `/`.
- **API Endpoints**: 
  - `/api/weather?stations=...` : Returns decoded JSON for the card grid.
  - `/api/station?icao=...` : Returns detailed JSON for the frontend modal.

### 4. Background Sync Worker (`sync_weather.py`)
- Periodically bulk-loads global + Indian METAR/TAF data into the local SQLite cache (`weather.db`) so requests are served instantly. Started automatically alongside the web server by `start.sh`.

## 🚀 Setup & Execution

### Local
1. **Install Dependencies**:
   ```bash
   pip install -r requirements.txt
   ```
2. **Environment Variables** — create a `.env` from `.env.example`:
   ```env
   TELEGRAM_TOKEN="your-telegram-token"
   CHECKWX_API_KEY="your-checkwx-key"
   AVWX_API_KEY="your-avwx-key"
   ```
3. **Run the web app + bot**:
   ```bash
   python app.py
   ```
   (Optionally run the cache worker in another terminal: `python sync_weather.py`.)

### Hugging Face Spaces (Docker)
- The `README.md` frontmatter (`sdk: docker`, `app_port: 7860`) tells Spaces to build the `Dockerfile`, which runs `start.sh` (sync worker + Gunicorn).
- Add `TELEGRAM_TOKEN`, `CHECKWX_API_KEY`, and `AVWX_API_KEY` under **Settings → Variables and secrets** (do NOT commit them).
- Deploy from Windows with `deploy.bat` after `set HF_TOKEN=hf_...`.

## 🧠 How the Decoding Works
- **Visibility**: Automatically converts Statute Miles (SM) for US stations and Meters/Kilometers for international stations.
- **Clouds**: Decodes covers (FEW, SCT, BKN, OVC, CAVOK) and bases into thousands of feet.
- **Present Weather**: Uses a dictionary mapping to decode abbreviations like `-SN`, `+TSRA`, `BR`, `FG`, `HZ`, `VCTS` into readable English (e.g., "Light Snow", "Heavy Thunderstorm Rain", "Mist").
- **Flight Category**: Retrieves Flight Rules (VFR, MVFR, IFR, LIFR).
