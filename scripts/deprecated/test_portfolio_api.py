# scripts/test_portfolio_api.py
import sys
from pathlib import Path
import json

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.app.routers.portfolio import fetch_current_prices
from backend.app.database import get_db_connection

def test_yfinance():
    print("Testing yfinance price fetch...")
    tickers = ["AAPL", "NVDA", "MSFT"]
    prices = fetch_current_prices(tickers)
    print(f"Fetched prices: {prices}")
    assert len(prices) > 0, "Failed to fetch any prices"
    print("[OK] yfinance fetch test passed.\n")

def test_db_insert():
    print("Testing database insert into yggdrasil.mimir_portfolio...")
    conn = get_db_connection()
    cur = conn.cursor()
    
    # Clean up old test data if any
    cur.execute("DELETE FROM yggdrasil.mimir_portfolio WHERE ticker = 'TEST_TICKER'")
    
    # Insert test position
    cur.execute("""
        INSERT INTO yggdrasil.mimir_portfolio (ticker, order_date, buy_price, quantity)
        VALUES ('TEST_TICKER', NOW(), 123.45, 10.0)
    """)
    conn.commit()
    print("[OK] Test position inserted.")
    
    # Query to verify
    cur.execute("SELECT * FROM yggdrasil.mimir_portfolio WHERE ticker = 'TEST_TICKER'")
    row = cur.fetchone()
    print(f"Queried row: {row}")
    assert row is not None, "Insert failed"
    
    # Clean up
    cur.execute("DELETE FROM yggdrasil.mimir_portfolio WHERE ticker = 'TEST_TICKER'")
    conn.commit()
    print("[OK] Cleaned up test data.")
    
    cur.close()
    conn.close()
    print("[OK] DB CRUD tests passed.\n")

if __name__ == "__main__":
    try:
        test_yfinance()
        test_db_insert()
        print("[OK] ALL TESTS PASSED.")
    except Exception as e:
        print(f"[FAIL] Verification failed: {e}")
        sys.exit(1)
