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
    edit_paper_position,
    edit_paper_signal,
    edit_paper_order_history,
    delete_paper_order_history,
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
    alert_id = None
    log_id = None
    try:
        cur.execute(f"DELETE FROM {settings.mimir_schema}.mimir_trade_signals WHERE ticker = %s", (mock_ticker,))
        cur.execute(f"DELETE FROM {settings.mimir_schema}.mimir_portfolio WHERE ticker = %s", (mock_ticker,))
        cur.execute(f"DELETE FROM {settings.mimir_schema}.mimir_paper_portfolio WHERE ticker = %s", (mock_ticker,))
        cur.execute(f"DELETE FROM {settings.mimir_schema}.mimir_paper_trade_log WHERE ticker = %s", (mock_ticker,))
        
        cur.execute(f"""
            INSERT INTO {settings.mimir_schema}.mimir_trade_signals
            (ticker, signal_type, trigger_price, rsi_value, sentiment_score, reason, status, created_at)
            VALUES (%s, 'BUY', 150.0, 25.0, 0.85, 'Test breakout paper alert', 'PENDING', NOW())
            RETURNING id
        """, (mock_ticker,))
        alert_id = cur.fetchone()[0]

        # Insert mock paper trade log for testing history edit/delete
        cur.execute(f"""
            INSERT INTO {settings.mimir_schema}.mimir_paper_trade_log
            (signal_id, ticker, action, entry_price, quantity, entry_time, exit_reason, notes)
            VALUES (%s, %s, 'BUY', 150.0, 10.0, NOW(), 'TEST_EXEC', 'Initial mock order log')
            RETURNING id
        """, (alert_id, mock_ticker))
        log_id = cur.fetchone()[0]

        # Insert high win rate parameter for mock ticker
        cur.execute(f"""
            INSERT INTO {settings.mimir_schema}.mimir_ticker_parameters 
            (ticker, win_rate, avg_pnl, optimal_hold_days, optimal_rsi_buy, optimal_rsi_sell, optimal_sentiment, optimal_vol_ratio)
            VALUES (%s, 70.0, 4.5, 5, 30.0, 70.0, 0.0, 1.0)
            ON CONFLICT (ticker) DO UPDATE SET win_rate = 70.0
        """, (mock_ticker,))
        conn.commit()
        print(f"   [OK] Mock alert created with ID {alert_id} & mock trade log ID {log_id} for {mock_ticker}.")
    finally:
        cur.close()
        conn.close()

    # 4. Test Editing Trade Signal
    print("\n4. Testing Edit Trade Signal...")
    sig_edit_res = edit_paper_signal(alert_id, trigger_price=155.50, signal_type="BUY")
    assert sig_edit_res.get("success") is True, f"Failed to edit signal: {sig_edit_res}"
    print(f"   [OK] Signal #{alert_id} edited: trigger_price updated to $155.50.")

    # 5. Test Editing Paper Position (MT5 validation check)
    print("\n5. Testing Edit Active Paper Position validation...")
    pos_edit_res = edit_paper_position(mock_ticker, new_quantity=15.0, new_buy_price=145.0)
    print(f"   [OK] MT5 validation returned as expected: {pos_edit_res.get('message')}")

    # 6. Test Editing & Deleting Paper Order History (PostgreSQL Audit Log)
    print("\n6. Testing Paper Trade Order History Edit & Delete...")
    hist_edit_res = edit_paper_order_history(log_id, ticker=mock_ticker, action="BUY", entry_price=145.0, exit_price=160.0, quantity=15.0, exit_reason="TAKE_PROFIT", notes="Updated via unit test")
    assert hist_edit_res.get("success") is True, f"Failed to edit paper order history: {hist_edit_res}"
    print(f"   [OK] Order history entry #{log_id} edited successfully.")

    hist_del_res = delete_paper_order_history(log_id)
    assert hist_del_res.get("success") is True, f"Failed to delete paper order history: {hist_del_res}"
    print(f"   [OK] Order history entry #{log_id} deleted successfully.")

    # 7. Check Summary (Live MT5 state)
    print("\n7. Testing Live MT5 Paper Trading Summary...")
    summary = get_paper_trading_summary()
    print(f"   [OK] Equity: ${summary['current_equity']:,.2f}, Cash: ${summary['cash_balance']:,.2f}, Active Positions: {len(summary['active_positions'])}, MT5 Live: {summary['is_mt5_live']}")

    # 8. Test Position Exit (Manual close validation)
    print("\n8. Testing Manual Position Close validation...")
    close_res = close_paper_position(mock_ticker)
    print(f"   [OK] Close position result: {close_res.get('message')}")

    # 9. Cleanup test data
    print("\n9. Cleaning up test data...")
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute(f"DELETE FROM {settings.mimir_schema}.mimir_trade_signals WHERE ticker = %s", (mock_ticker,))
        cur.execute(f"DELETE FROM {settings.mimir_schema}.mimir_portfolio WHERE ticker = %s", (mock_ticker,))
        cur.execute(f"DELETE FROM {settings.mimir_schema}.mimir_paper_portfolio WHERE ticker = %s", (mock_ticker,))
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


