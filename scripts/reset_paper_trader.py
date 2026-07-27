# scripts/reset_paper_trader.py
import sys
import os
from pathlib import Path
import psycopg2
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent.parent))
dotenv_path = Path(__file__).parent.parent / '.env'
load_dotenv(dotenv_path)

DB_URL = f"postgresql://{os.getenv('DB_USER')}:{os.getenv('DB_PASSWORD')}@{os.getenv('DB_HOST')}:{os.getenv('DB_PORT')}/{os.getenv('DB_NAME')}"
SCHEMA = os.getenv('MIMIR_SCHEMA', 'yggdrasil')

RESET_SQL = f"""
-- Clear paper trading portfolio and log tables
TRUNCATE TABLE {SCHEMA}.mimir_paper_portfolio;
TRUNCATE TABLE {SCHEMA}.mimir_paper_trade_log;

-- Reset paper trading config to defaults ($200 capital)
UPDATE {SCHEMA}.mimir_paper_trading_config
SET is_enabled = TRUE,
    execution_mode = 'AUTO',
    initial_capital = 200.0,
    position_size_value = 20.0,
    us_stocks_only = TRUE,
    updated_at = NOW();

-- Clear old pending/approved test trade signals
DELETE FROM {SCHEMA}.mimir_trade_signals
WHERE ticker LIKE 'TEST_%%' OR created_at < NOW() - INTERVAL '7 days';
"""

if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except AttributeError:
        pass

def reset_paper_trader():
    print("=== MIMIR Paper Trader Reset ===")
    print(f"Connecting to database to reset paper portfolio...")
    try:
        conn = psycopg2.connect(DB_URL)
        cur = conn.cursor()
        cur.execute(RESET_SQL)
        conn.commit()
        print("Paper trader reset complete! Portfolio cleared, cash balance reset to $200.00.")
        cur.close()
        conn.close()
    except Exception as e:
        print(f"Error resetting paper trader: {e}")

if __name__ == "__main__":
    reset_paper_trader()
