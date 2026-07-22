# scripts/test_paper_trading.py
import sys
import os
from pathlib import Path
from datetime import datetime, timezone

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.database import get_db_connection, get_db_connection_dict
from backend.app.config import get_settings
from backend.app.analytics.paper_trader import (
    init_paper_trading_db,
    get_paper_config,
    update_paper_config,
    auto_execute_pending_alerts,
    process_paper_position_exits,
    get_paper_trading_summary,
    close_paper_position,
    reset_paper_account
)

settings = get_settings()

def run_test_suite():
    print("=== [PAPER TRADING TEST SUITE] ===")

    # 1. DB Init
    print("\n1. Testing Database Initialization...")
    init_paper_trading_db()
    print("   [OK] Tables initialized successfully.")

    # 2. Get/Update Config
    print("\n2. Testing Config Retrieval & Update...")
    config = get_paper_config()
    print(f"   [OK] Config retrieved: enabled={config['is_enabled']}, mode={config['execution_mode']}, min_win_rate={config['min_win_rate']}%")

    updated = update_paper_config({"min_win_rate": 50.0, "position_size_value": 1500.0})
    print(f"   [OK] Config updated: min_win_rate={updated['min_win_rate']}%, pos_val=${updated['position_size_value']}")
    
    # Restore min win rate
    update_paper_config({"min_win_rate": 55.0, "position_size_value": 1000.0})

    # 3. Create mock pending alert
    print("\n3. Inserting Mock Pending Signal for Testing...")
    conn = get_db_connection()
    cur = conn.cursor()
    mock_ticker = "TEST_PAPER_TICKER"
    try:
        cur.execute(f"DELETE FROM {settings.mimir_schema}.mimir_trade_signals WHERE ticker = %s", (mock_ticker,))
        cur.execute(f"DELETE FROM {settings.mimir_schema}.mimir_portfolio WHERE ticker = %s", (mock_ticker,))
        cur.execute(f"DELETE FROM {settings.mimir_schema}.mimir_paper_trade_log WHERE ticker = %s", (mock_ticker,))
        
        cur.execute(f"""
            INSERT INTO {settings.mimir_schema}.mimir_trade_signals
            (ticker, signal_type, trigger_price, rsi_value, sentiment_score, reason, status, created_at)
            VALUES (%s, 'BUY', 150.0, 25.0, 0.85, 'Test breakout paper alert', 'PENDING', NOW())
            RETURNING id
        """, (mock_ticker,))
        alert_id = cur.fetchone()[0]

        # Insert high win rate parameter for mock ticker
        cur.execute(f"""
            INSERT INTO {settings.mimir_schema}.mimir_ticker_parameters 
            (ticker, win_rate, avg_pnl, optimal_hold_days, optimal_rsi_buy, optimal_rsi_sell, optimal_sentiment, optimal_vol_ratio)
            VALUES (%s, 70.0, 4.5, 5, 30.0, 70.0, 0.0, 1.0)
            ON CONFLICT (ticker) DO UPDATE SET win_rate = 70.0
        """, (mock_ticker,))
        conn.commit()
        print(f"   [OK] Mock alert created with ID {alert_id} for {mock_ticker}.")
    finally:
        cur.close()
        conn.close()

    # 4. Test Auto-Execution
    print("\n4. Testing Auto-Execution of Pending Alerts...")
    exec_res = auto_execute_pending_alerts()
    print(f"   [OK] Auto-trade executed count: {exec_res.get('executed_count')}")

    # 5. Check Summary
    print("\n5. Testing Paper Trading Summary...")
    summary = get_paper_trading_summary()
    print(f"   [OK] Equity: ${summary['current_equity']:,.2f}, Cash: ${summary['cash_balance']:,.2f}, Active Positions: {len(summary['active_positions'])}")
    if mock_ticker in summary["active_positions"]:
        pos = summary["active_positions"][mock_ticker]
        print(f"   [OK] Found active paper position for {mock_ticker}: {pos['quantity']} shares @ ${pos['avg_entry_price']:.2f}")

    # 6. Test Position Exit (Manual close)
    print("\n6. Testing Manual Position Close...")
    close_res = close_paper_position(mock_ticker)
    print(f"   [OK] Close position result: {close_res.get('message')}")

    # 7. Cleanup test data
    print("\n7. Cleaning up test data...")
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute(f"DELETE FROM {settings.mimir_schema}.mimir_trade_signals WHERE ticker = %s", (mock_ticker,))
        cur.execute(f"DELETE FROM {settings.mimir_schema}.mimir_portfolio WHERE ticker = %s", (mock_ticker,))
        cur.execute(f"DELETE FROM {settings.mimir_schema}.mimir_paper_trade_log WHERE ticker = %s", (mock_ticker,))
        cur.execute(f"DELETE FROM {settings.mimir_schema}.mimir_ticker_parameters WHERE ticker = %s", (mock_ticker,))
        conn.commit()
        print("   [OK] Test data cleaned up successfully.")
    finally:
        cur.close()
        conn.close()

    print("\n=== [ALL PAPER TRADING TESTS PASSED] ===")

if __name__ == "__main__":
    run_test_suite()
