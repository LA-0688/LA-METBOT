import os
import sqlite3
import json
from datetime import datetime, timedelta, timezone

DB_PATH = 'weather.db'

# How long a cached record is considered fresh. Used by BOTH get_cached_weather
# and get_weather_batch so every endpoint applies the same freshness rule.
STALE_AFTER = timedelta(minutes=30)

def get_connection():
    """Returns an active SQLite connection with row_factory enabled.

    WAL mode + a busy timeout let the background sync worker and the web
    workers read/write the same file concurrently without 'database is locked'.
    """
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=30000;")
    except Exception:
        pass
    return conn

def init_db():
    """Creates the necessary tables if they don't exist."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS airport_weather (
                icao_code TEXT PRIMARY KEY,
                raw_metar TEXT,
                raw_taf TEXT,
                decoded_data TEXT,
                last_updated TIMESTAMP
            )
        """)
        conn.commit()

# Ensure the database is initialized at script startup
init_db()

def get_cached_weather(icao):
    """Retrieves airport data if it exists and is less than 30 minutes old."""
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM airport_weather WHERE icao_code = ?", (icao.upper(),))
            row = cursor.fetchone()
            
            if row:
                result = dict(row)
                now = datetime.now(timezone.utc)
                last_updated_str = result['last_updated']
                
                # Parse ISO format timestamp securely
                try:
                    last_updated = datetime.fromisoformat(last_updated_str)
                    if last_updated.tzinfo is None:
                        last_updated = last_updated.replace(tzinfo=timezone.utc)
                except ValueError:
                    return None
                    
                data_age = now - last_updated
                if data_age < STALE_AFTER:
                    # Save the parsed datetime back into the dictionary
                    result['last_updated'] = last_updated
                    # Parse the JSON string back into a dictionary
                    result['decoded_data'] = json.loads(result['decoded_data']) if result['decoded_data'] else {}
                    return result 
            return None 
    except Exception as e:
        print(f"Database read error: {e}")
        return None

def _process_history(existing_row, new_raw_metar, decoded_json):
    """Helper to process history in pure Python since SQLite lacks JSONB array functions."""
    history = []
    
    # Extract existing history if it exists
    if existing_row and existing_row['decoded_data']:
        try:
            existing_data = json.loads(existing_row['decoded_data'])
            history = existing_data.get('history', [])
            if not isinstance(history, list):
                history = []
        except Exception:
            pass

    # Insert the new METAR at the beginning if valid and not a duplicate
    if new_raw_metar and new_raw_metar not in history:
        history.insert(0, new_raw_metar)
        
    # Keep only unique elements, preserving order
    unique_history = []
    for item in history:
        if item and item not in unique_history:
            unique_history.append(item)
            
    # Limit to the last 3 items
    decoded_json['history'] = unique_history[:3]
    return decoded_json

def upsert_weather(icao, raw_metar, raw_taf, decoded_json):
    """Inserts or updates weather records using SQLite UPSERT."""
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            icao = icao.upper()
            now = datetime.now(timezone.utc).isoformat()
            
            # Fetch existing record for history processing
            cursor.execute("SELECT raw_metar, decoded_data FROM airport_weather WHERE icao_code = ?", (icao,))
            existing = cursor.fetchone()
            
            # Process history array in pure Python
            decoded_json = _process_history(existing, raw_metar, decoded_json)

            query = """
            INSERT INTO airport_weather (icao_code, raw_metar, raw_taf, decoded_data, last_updated)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(icao_code) DO UPDATE SET 
                raw_metar = COALESCE(NULLIF(excluded.raw_metar, ''), airport_weather.raw_metar),
                raw_taf = COALESCE(NULLIF(excluded.raw_taf, ''), airport_weather.raw_taf),
                decoded_data = excluded.decoded_data,
                last_updated = excluded.last_updated;
            """
            
            cursor.execute(query, (icao, raw_metar, raw_taf, json.dumps(decoded_json), now))
            conn.commit()
    except Exception as e:
        print(f"Database write error: {e}")

def bulk_upsert_weather(records):
    """Bulk inserts/updates thousands of weather records in a single transaction."""
    if not records:
        return 0
    
    try:
        with get_connection() as conn:
            cursor = conn.cursor()

            # Pre-fetch all existing rows in a single query instead of one
            # SELECT per record, so this is genuinely a bulk operation.
            icaos = [r[0].upper() for r in records]
            existing_by_icao = {}
            CHUNK = 500
            for i in range(0, len(icaos), CHUNK):
                chunk = icaos[i:i + CHUNK]
                placeholders = ','.join(['?'] * len(chunk))
                cursor.execute(
                    f"SELECT icao_code, raw_metar, decoded_data FROM airport_weather WHERE icao_code IN ({placeholders})",
                    tuple(chunk),
                )
                for row in cursor.fetchall():
                    existing_by_icao[row['icao_code']] = row

            for icao, raw_metar, raw_taf, decoded_json in records:
                icao = icao.upper()
                now = datetime.now(timezone.utc).isoformat()

                existing = existing_by_icao.get(icao)

                decoded_json = _process_history(existing, raw_metar, decoded_json)

                query = """
                INSERT INTO airport_weather (icao_code, raw_metar, raw_taf, decoded_data, last_updated)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(icao_code) DO UPDATE SET 
                    raw_metar = COALESCE(NULLIF(excluded.raw_metar, ''), airport_weather.raw_metar),
                    raw_taf = COALESCE(NULLIF(excluded.raw_taf, ''), airport_weather.raw_taf),
                    decoded_data = excluded.decoded_data,
                    last_updated = excluded.last_updated;
                """
                
                cursor.execute(query, (icao, raw_metar, raw_taf, json.dumps(decoded_json), now))
                
            conn.commit()
        print(f"[BULK DB] Successfully upserted {len(records)} airport records.", flush=True)
        return len(records)
    except Exception as e:
        print(f"[BULK DB] Bulk write error: {e}", flush=True)
        return 0

def get_weather_batch(icao_list):
    """Fetches the absolute latest weather for multiple airports instantly."""
    if not icao_list:
        return {}
        
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            placeholders = ','.join(['?'] * len(icao_list))
            query = f"SELECT * FROM airport_weather WHERE icao_code IN ({placeholders})"
            cursor.execute(query, tuple(icao_list))
            
            now = datetime.now(timezone.utc)
            weather_data = {}
            for row in cursor.fetchall():
                icao = row['icao_code']
                decoded_data = {}
                try:
                    decoded_data = json.loads(row['decoded_data']) if row['decoded_data'] else {}
                except Exception:
                    pass

                dt = None
                if row['last_updated']:
                    try:
                        dt = datetime.fromisoformat(row['last_updated'])
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                    except Exception:
                        pass

                # Apply the same freshness rule as get_cached_weather. Stale rows
                # are skipped so the caller treats them as a cache miss and
                # re-fetches live data instead of serving arbitrarily old weather.
                if dt is None or (now - dt) >= STALE_AFTER:
                    continue

                weather_data[icao] = {
                    "raw_metar": row['raw_metar'] or '',
                    "raw_taf": row['raw_taf'] or '',
                    "decoded": decoded_data,
                    "last_updated": dt
                }
            return weather_data
    except Exception as e:
        print(f"[LAYER 1 DB ERROR] Failed to fetch weather batch: {e}")
        return {}
