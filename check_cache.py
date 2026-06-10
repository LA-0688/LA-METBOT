"""Quick check: cache row count, VIAR freshness, and API latency."""
import sqlite3
import time

import requests

conn = sqlite3.connect('weather.db')
n = conn.execute('SELECT COUNT(*) FROM airport_weather').fetchone()[0]
viar = conn.execute(
    "SELECT raw_metar, last_updated FROM airport_weather WHERE icao_code='VIAR'"
).fetchone()
print('rows in cache:', n)
print('VIAR row:', viar)

t0 = time.time()
r = requests.get('http://127.0.0.1:7860/api/weather?stations=VIAR,KJFK,EGLL', timeout=60)
dt = time.time() - t0
res = r.json()['results']
print(f'API call took {dt:.2f}s')
for k, v in res.items():
    print(k, '->', v.get('raw_metar', v.get('error', '?'))[:70])
