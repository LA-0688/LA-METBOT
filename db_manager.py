import os
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool
from datetime import datetime, timedelta, timezone

_pool = None

def get_pool():
    """Lazily initializes and returns the connection pool."""
    global _pool
    if _pool is None:
        db_url = os.environ.get("DATABASE_URL")
        if db_url:
            db_url = db_url.strip()
        # Ensure minimum of 1 connection and max of 10 to avoid connection limits
        # timeout=2.0 fails fast if pool is empty. kwargs={"connect_timeout": 2} fails fast on DB connection issue
        # Supabase uses PgBouncer in transaction mode, which breaks psycopg3 prepared statements. Must disable them!
        _pool = ConnectionPool(db_url, min_size=1, max_size=10, timeout=2.0, kwargs={"connect_timeout": 2, "prepare_threshold": None})
    return _pool

def get_cached_weather(icao):
    """Retrieves airport data if it exists and is less than 30 minutes old."""
    try:
        with get_pool().connection(timeout=2.0) as conn:
            with conn.cursor(row_factory=dict_row) as cursor:
                query = "SELECT * FROM airport_weather WHERE icao_code = %s"
                cursor.execute(query, (icao.upper(),))
                result = cursor.fetchone()
                
                if result:
                    now = datetime.now(timezone.utc)
                    last_updated = result['last_updated']
                    
                    # Ensure datetime is timezone-aware to prevent crash when subtracting
                    if last_updated.tzinfo is None:
                        last_updated = last_updated.replace(tzinfo=timezone.utc)
                        
                    data_age = now - last_updated
                    if data_age < timedelta(minutes=30):
                        return result # Data is fresh!
                return None # Cache miss or data is stale
    except Exception as e:
        print(f"Database read error: {e}")
        return None

def upsert_weather(icao, raw_metar, raw_taf, decoded_json):
    """Inserts or updates weather records cleanly using PostgreSQL UPSERT syntax."""
    try:
        with get_pool().connection(timeout=2.0) as conn:
            with conn.cursor() as cursor:
                query = """
                INSERT INTO airport_weather (icao_code, raw_metar, raw_taf, decoded_data, last_updated)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (icao_code) 
                DO UPDATE SET 
                    raw_metar = EXCLUDED.raw_metar,
                    raw_taf = COALESCE(NULLIF(EXCLUDED.raw_taf, ''), airport_weather.raw_taf),
                    decoded_data = jsonb_set(
                        EXCLUDED.decoded_data,
                        '{history}',
                        (
                            SELECT COALESCE(jsonb_agg(elem), '[]'::jsonb) FROM (
                                SELECT EXCLUDED.raw_metar AS elem WHERE EXCLUDED.raw_metar != ''
                                UNION ALL
                                SELECT elem FROM jsonb_array_elements_text(
                                    CASE 
                                        WHEN airport_weather.decoded_data->'history' IS NULL THEN '[]'::jsonb
                                        WHEN jsonb_typeof(airport_weather.decoded_data->'history') != 'array' THEN '[]'::jsonb
                                        ELSE airport_weather.decoded_data->'history'
                                    END
                                ) AS elem
                                WHERE elem != EXCLUDED.raw_metar AND elem != ''
                            ) sub LIMIT 3
                        )::jsonb
                    ),
                    last_updated = EXCLUDED.last_updated;
                """
                now = datetime.now(timezone.utc)
                
                # Use Jsonb wrapper instead of json.dumps to avoid Postgres type mismatch
                cursor.execute(query, (icao.upper(), raw_metar, raw_taf, Jsonb(decoded_json), now))
                
                # Note: The context manager (with conn:) automatically commits on success
    except Exception as e:
        print(f"Database write error: {e}")

def bulk_upsert_weather(records):
    """Bulk inserts/updates thousands of weather records in a single transaction.
    
    Args:
        records: list of tuples (icao_code, raw_metar, raw_taf, decoded_json_dict)
    
    Returns:
        Number of records successfully upserted, or 0 on failure.
    """
    if not records:
        return 0
    
    try:
        # Use a longer timeout for bulk operations (up to 30s for thousands of rows)
        with get_pool().connection(timeout=10.0) as conn:
            with conn.cursor() as cursor:
                query = """
                INSERT INTO airport_weather (icao_code, raw_metar, raw_taf, decoded_data, last_updated)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (icao_code) 
                DO UPDATE SET 
                    raw_metar = COALESCE(NULLIF(EXCLUDED.raw_metar, ''), airport_weather.raw_metar),
                    raw_taf = COALESCE(NULLIF(EXCLUDED.raw_taf, ''), airport_weather.raw_taf),
                    decoded_data = jsonb_set(
                        EXCLUDED.decoded_data,
                        '{history}',
                        (
                            SELECT COALESCE(jsonb_agg(elem), '[]'::jsonb) FROM (
                                SELECT EXCLUDED.raw_metar AS elem WHERE EXCLUDED.raw_metar != ''
                                UNION ALL
                                SELECT elem FROM jsonb_array_elements_text(
                                    CASE 
                                        WHEN airport_weather.decoded_data->'history' IS NULL THEN '[]'::jsonb
                                        WHEN jsonb_typeof(airport_weather.decoded_data->'history') != 'array' THEN '[]'::jsonb
                                        ELSE airport_weather.decoded_data->'history'
                                    END
                                ) AS elem
                                WHERE elem != EXCLUDED.raw_metar AND elem != ''
                            ) sub LIMIT 3
                        )::jsonb
                    ),
                    last_updated = EXCLUDED.last_updated;
                """
                now = datetime.now(timezone.utc)
                
                # Build parameter rows with Jsonb wrapper for each record
                params = [
                    (icao.upper(), raw_metar, raw_taf, Jsonb(decoded_json), now)
                    for icao, raw_metar, raw_taf, decoded_json in records
                ]
                
                cursor.executemany(query, params)
                
        print(f"[BULK DB] Successfully upserted {len(records)} airport records.", flush=True)
        return len(records)
    except Exception as e:
        print(f"[BULK DB] Bulk write error: {e}", flush=True)
        return 0

def get_weather_batch(icao_list):
    """
    LAYER 1 READER: Fetches the absolute latest weather for multiple airports instantly.
    """
    if not icao_list:
        return {}
        
    try:
        # Use our 2-second timeout defensive pool
        with get_pool().connection(timeout=2.0) as conn:
            with conn.cursor(row_factory=dict_row) as cursor:
                # Create a parameterized query for multiple airports
                placeholders = ','.join(['%s'] * len(icao_list))
                query = f"SELECT * FROM airport_weather WHERE icao_code IN ({placeholders})"
                
                cursor.execute(query, tuple(icao_list))
                results = cursor.fetchall()
                
                # Format into a clean dictionary where the ICAO code is the key
                weather_data = {}
                for row in results:
                    weather_data[row['icao_code']] = {
                        "raw_metar": row.get('raw_metar', ''),
                        "raw_taf": row.get('raw_taf', ''),
                        "decoded": row.get('decoded_data', {}),
                        "last_updated": row['last_updated'].isoformat()
                    }
                return weather_data
    except Exception as e:
        print(f"[LAYER 1 DB ERROR] Failed to fetch weather batch: {e}")
        return {}
