# scripts/test_oracle_trade_history.py
import sys
import json
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.app.sentiment.agent_tools import (
    query_portfolio,
    query_trade_signals,
    query_backtest_history,
    execute_oracle_tool,
    ORACLE_TOOLS
)
from backend.app.database import get_db_connection
from backend.app.config import get_settings

settings = get_settings()

def test_oracle_trade_history():
    print("--- Running Oracle Trading History Access Tests ---")

    # 1. Check ORACLE_TOOLS definitions
    tool_names = [t["function"]["name"] for t in ORACLE_TOOLS]
    print(f"Registered Oracle Tools: {tool_names}")
    assert "query_portfolio" in tool_names, "query_portfolio tool missing from ORACLE_TOOLS"
    assert "query_trade_signals" in tool_names, "query_trade_signals tool missing from ORACLE_TOOLS"
    assert "query_backtest_history" in tool_names, "query_backtest_history tool missing from ORACLE_TOOLS"
    print("[PASS] All required tools registered in ORACLE_TOOLS.")

    # 2. Test query_portfolio with >20 mock transactions
    conn = get_db_connection()
    cur = conn.cursor()
    
    # Clean up test records
    cur.execute(f"DELETE FROM {settings.mimir_schema}.mimir_portfolio WHERE ticker = 'TEST_ORACLE'")
    cur.execute(f"DELETE FROM {settings.mimir_schema}.mimir_trade_signals WHERE ticker = 'TEST_ORACLE'")
    conn.commit()

    try:
        # Insert 25 mock transactions for TEST_ORACLE
        print("Inserting 25 test transactions for TEST_ORACLE...")
        for i in range(25):
            cur.execute(f"""
                INSERT INTO {settings.mimir_schema}.mimir_portfolio 
                (ticker, order_date, buy_price, quantity, transaction_type, brokerage_fee, regulatory_fee, other_fee)
                VALUES ('TEST_ORACLE', NOW() - INTERVAL '{i} hours', 100.0 + {i}, 10.0, 'BUY', 1.0, 0.5, 0.0)
            """)
        
        # Insert test trade signal
        cur.execute(f"""
            INSERT INTO {settings.mimir_schema}.mimir_trade_signals
            (ticker, signal_type, trigger_price, rsi_value, sentiment_score, reason, status)
            VALUES ('TEST_ORACLE', 'BUY', 105.0, 32.5, 0.85, 'Test oversold signal', 'APPROVED')
        """)
        conn.commit()

        # Execute query_portfolio without limit (should return all 25)
        res_all_json = query_portfolio(ticker="TEST_ORACLE")
        res_all = json.loads(res_all_json)
        print(f"Fetched {len(res_all)} transactions for TEST_ORACLE (expected 25)")
        assert len(res_all) == 25, f"Expected 25 transactions, got {len(res_all)}"
        print("[PASS] query_portfolio returned ALL transactions (exceeding previous 20-limit threshold).")

        # Test query_portfolio with limit=5
        res_limit_json = query_portfolio(ticker="TEST_ORACLE", limit=5)
        res_limit = json.loads(res_limit_json)
        assert len(res_limit) == 5, f"Expected 5 transactions, got {len(res_limit)}"
        print("[PASS] query_portfolio respects specified limit=5.")

        # Test execute_oracle_tool dispatcher for portfolio
        disp_res_json = execute_oracle_tool("query_portfolio", {"ticker": "TEST_ORACLE"})
        disp_res = json.loads(disp_res_json)
        assert len(disp_res) == 25, "Dispatcher failed to return all 25 transactions."
        print("[PASS] execute_oracle_tool correctly dispatches query_portfolio.")

        # Test query_trade_signals
        signals_json = execute_oracle_tool("query_trade_signals", {"ticker": "TEST_ORACLE"})
        signals = json.loads(signals_json)
        assert len(signals) >= 1, "Expected at least 1 trade signal for TEST_ORACLE"
        assert signals[0]["ticker"] == "TEST_ORACLE"
        assert signals[0]["status"] == "APPROVED"
        print("[PASS] query_trade_signals returned trade signal and execution log.")

        # Test query_backtest_history
        bt_res = execute_oracle_tool("query_backtest_history", {"limit": 5})
        print(f"query_backtest_history response sample: {bt_res[:100]}...")
        assert "Error" not in bt_res, "query_backtest_history returned error"
        print("[PASS] query_backtest_history works.")

        print("\nAll Oracle Assistant trading history access tests PASSED successfully!")

    finally:
        # Cleanup
        cur.execute(f"DELETE FROM {settings.mimir_schema}.mimir_portfolio WHERE ticker = 'TEST_ORACLE'")
        cur.execute(f"DELETE FROM {settings.mimir_schema}.mimir_trade_signals WHERE ticker = 'TEST_ORACLE'")
        conn.commit()
        cur.close()
        conn.close()

if __name__ == "__main__":
    test_oracle_trade_history()
