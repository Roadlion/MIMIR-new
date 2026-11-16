import sys
import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.config import get_settings

settings = get_settings()

print("=================================================================")
print("  PHASE 1: DATABASE LOCK RESOLUTION & INDEX MIGRATION (P0)      ")
print("=================================================================")

conn = psycopg2.connect(
    host=settings.db_host,
    port=settings.db_port,
    dbname=settings.db_name,
    user=settings.db_user,
    password=settings.db_password,
    connect_timeout=5,
    options="-c zero_damaged_pages=on"
)
conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
cur = conn.cursor()

# 1. Terminate/cancel any lingering queries running > 30 seconds
print("\n[1/3] Cancelling lingering blocking queries...")
try:
    cur.execute("""
        SELECT pg_cancel_backend(pid), pid, query_start, query 
        FROM pg_stat_activity 
        WHERE datname = %s 
          AND state != 'idle' 
          AND pid != pg_backend_pid()
          AND query_start < NOW() - INTERVAL '30 seconds'
    """, (settings.db_name,))
    cancelled = cur.fetchall()
    print(f"  Cancelled {len(cancelled)} hung queries.")
    for c in cancelled[:5]:
        print(f"    PID {c[1]}: {c[3][:60]}...")
except Exception as e:
    print(f"  Note during query cancellation: {e}")

# Also terminate any idle in transaction older than 60 seconds
try:
    cur.execute("""
        SELECT pg_terminate_backend(pid), pid, state_change, query 
        FROM pg_stat_activity 
        WHERE datname = %s 
          AND state = 'idle in transaction' 
          AND state_change < NOW() - INTERVAL '60 seconds'
    """, (settings.db_name,))
    terminated = cur.fetchall()
    print(f"  Terminated {len(terminated)} idle-in-transaction connections.")
except Exception as e:
    print(f"  Note during transaction cleanup: {e}")

# 2. Deploy Performance B-tree Indexes
schema = settings.mimir_schema
indexes = [
    (
        "idx_mimir_articles_status_ts",
        f"CREATE INDEX IF NOT EXISTS idx_mimir_articles_status_ts ON {schema}.mimir_raw_articles (scoring_status, published_ts DESC);"
    ),
    (
        "idx_mimir_articles_published_ts",
        f"CREATE INDEX IF NOT EXISTS idx_mimir_articles_published_ts ON {schema}.mimir_raw_articles (published_ts DESC);"
    ),
    (
        "idx_mimir_sentiment_ticker_created",
        f"CREATE INDEX IF NOT EXISTS idx_mimir_sentiment_ticker_created ON {schema}.mimir_sentiment_impacts (ticker, created_at DESC) WHERE ticker IS NOT NULL;"
    ),
    (
        "idx_mimir_sentiment_art_ticker",
        f"CREATE INDEX IF NOT EXISTS idx_mimir_sentiment_art_ticker ON {schema}.mimir_sentiment_impacts (article_id, ticker);"
    ),
    (
        "idx_mimir_signals_status_conviction",
        f"CREATE INDEX IF NOT EXISTS idx_mimir_signals_status_conviction ON {schema}.mimir_trade_signals (status, conviction_score DESC, created_at ASC);"
    ),
    (
        "idx_mimir_trade_log_exit_entry",
        f"CREATE INDEX IF NOT EXISTS idx_mimir_trade_log_exit_entry ON {schema}.mimir_paper_trade_log (ticker, exit_time, entry_time);"
    )
]

print("\n[2/3] Deploying Core Performance Indexes...")
for idx_name, idx_sql in indexes:
    try:
        cur.execute(idx_sql)
        print(f"  [SUCCESS] {idx_name} created / verified.")
    except Exception as e:
        print(f"  [ERROR] {idx_name}: {e}")

# 3. Create circuit breaker table if not exists
print("\n[3/3] Creating mimir_circuit_breaker audit table...")
cur.execute(f"""
    CREATE TABLE IF NOT EXISTS {schema}.mimir_circuit_breaker (
        id SERIAL PRIMARY KEY,
        tripped_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
        reason VARCHAR(255) NOT NULL,
        daily_loss_usd NUMERIC(15, 2) NOT NULL,
        daily_loss_pct NUMERIC(6, 2) NOT NULL,
        account_equity NUMERIC(15, 2) NOT NULL,
        is_active BOOLEAN DEFAULT TRUE,
        reset_at TIMESTAMP WITH TIME ZONE,
        notes TEXT
    );
""")
print(f"  [SUCCESS] {schema}.mimir_circuit_breaker created / verified.")

cur.close()
conn.close()
print("\n=== PHASE 1 DATABASE MIGRATION COMPLETE ===")
