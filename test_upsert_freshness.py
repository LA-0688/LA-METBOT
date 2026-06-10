"""Verifies a failed (empty-METAR) fetch cannot re-stamp old weather as fresh."""
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import db_manager as dbm

old_ts = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
conn = sqlite3.connect('weather.db')
conn.execute(
    "INSERT OR REPLACE INTO airport_weather VALUES ('ZZZT', 'ZZZT 100100Z OLD', '', ?, ?)",
    (json.dumps({'history': ['ZZZT 100100Z OLD']}), old_ts),
)
conn.commit()
conn.close()

# Upsert with EMPTY metar -> old metar kept, last_updated must NOT bump
dbm.upsert_weather('ZZZT', '', 'ZZZT TAF 1000/1106', {'history': []})
conn = sqlite3.connect('weather.db')
row = conn.execute(
    "SELECT raw_metar, raw_taf, last_updated FROM airport_weather WHERE icao_code='ZZZT'"
).fetchone()
print('after empty-metar upsert:', row)
assert row[0] == 'ZZZT 100100Z OLD', 'old metar should be kept'
assert row[1] == 'ZZZT TAF 1000/1106', 'new taf should be saved'
assert row[2] == old_ts, 'last_updated must NOT bump on empty metar'
assert dbm.get_cached_weather('ZZZT') is None, 'row must still be treated as stale'

# Upsert with REAL metar -> last_updated bumps, row becomes fresh
dbm.upsert_weather('ZZZT', 'ZZZT 100600Z NEW', '', {'history': []})
row = conn.execute(
    "SELECT raw_metar, last_updated FROM airport_weather WHERE icao_code='ZZZT'"
).fetchone()
print('after real-metar upsert:', row)
assert row[0] == 'ZZZT 100600Z NEW'
assert dbm.get_cached_weather('ZZZT') is not None, 'row must now be fresh'

# Same rules via the bulk path
conn.execute("UPDATE airport_weather SET last_updated = ? WHERE icao_code='ZZZT'", (old_ts,))
conn.commit()
dbm.bulk_upsert_weather([('ZZZT', '', '', {'history': []})])
row = conn.execute(
    "SELECT raw_metar, last_updated FROM airport_weather WHERE icao_code='ZZZT'"
).fetchone()
print('after bulk empty-metar upsert:', row)
assert row[0] == 'ZZZT 100600Z NEW', 'bulk: old metar kept'
assert row[1] == old_ts, 'bulk: last_updated must NOT bump on empty metar'

conn.execute("DELETE FROM airport_weather WHERE icao_code='ZZZT'")
conn.commit()
conn.close()
print('ALL UPSERT TESTS PASSED')
