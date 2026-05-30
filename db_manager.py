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
        _pool = ConnectionPool(db_url, min_size=1, max_size=10, timeout=2.0, kwargs={"connect_timeout": 2})
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
                    raw_taf = EXCLUDED.raw_taf,
                    decoded_data = EXCLUDED.decoded_data,
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
                    raw_metar = EXCLUDED.raw_metar,
                    raw_taf = EXCLUDED.raw_taf,
                    decoded_data = EXCLUDED.decoded_data,
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
