#!/usr/bin/env bash
set -e

# Start the background sync worker that populates the SQLite cache.
# Without this, every request is a cache miss (see api_weather on-demand path).
python sync_weather.py &

# Start the web server in the foreground (PID 1 keeps the container alive).
# Keep workers at 1: the Telegram long-polling thread must run in a single
# process, otherwise multiple workers poll the same token and Telegram 409s.
exec gunicorn -w 1 --threads 4 -b 0.0.0.0:"${PORT:-7860}" app:app
