# scripts/test_catalyst_engine.py
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.app.database import get_db_connection
from backend.app.config import get_settings
from backend.app.analytics.catalyst_engine import eval_realtime_article_catalyst, scan_pre_earnings_catalysts

settings = get_settings()

def run_test():
    print("[TEST] Testing MIMIR Catalyst & Pre-Earnings Engine...")

    conn = get_db_connection()
    cur = conn.cursor()

    # Clean test alerts
    cur.execute(f"DELETE FROM {settings.mimir_schema}.mimir_trade_signals WHERE ticker = 'NVDA' AND headline LIKE '%%TSMC%%'")
    conn.commit()

    # 1. Test Real-Time Event Catalyst Trigger
    test_headline = "TSMC Reports Record Q2 Earnings Beat Driven by Surging AI Accelerator Demand"
    test_reasoning = "TSMC reported +36% YoY revenue growth due to strong orders for 3nm/5nm AI chips. This indicates massive upstream demand for Nvidia and AMD."
    
    print(f"\n[TEST 1] Triggering real-time catalyst evaluation for NVDA...")
    res = eval_realtime_article_catalyst(
        ticker="NVDA",
        sentiment_score=0.85,
        confidence=0.90,
        headline=test_headline,
        reasoning=test_reasoning,
        policy_signal="AI_INFRASTRUCTURE_BOOM",
        is_spillover=True,
        spillover_source_asset="TSMC",
        ignore_macro=True,
        conn=conn
    )

    if res:
        print("[TEST 1 SUCCESS] Created Real-time Catalyst Alert:")
        print(f" - Ticker: {res['ticker']}")
        print(f" - Catalyst Type: {res['catalyst_type']}")
        print(f" - Holding Period: {res['holding_period']}")
        print(f" - Target Price: ${res['target_price']}")
        print(f" - Stop Loss: ${res['stop_loss']}")
        print(f" - Thesis Preview: {res['investment_thesis'][:150].encode('ascii', 'ignore').decode()}...")
    else:
        print("[TEST 1 FAILED] Catalyst alert creation returned None.")

    # 2. Test Pending Alerts Endpoint Logic
    cur.execute(f"""
        SELECT ticker, catalyst_type, headline, holding_period, target_price, stop_loss, investment_thesis
        FROM {settings.mimir_schema}.mimir_trade_signals
        WHERE ticker = 'NVDA' AND status = 'PENDING'
        ORDER BY created_at DESC LIMIT 1
    """)
    row = cur.fetchone()
    if row:
        print(f"\n[TEST 2 SUCCESS] Verified Database Persistence:")
        print(f" - Ticker: {row[0]}")
        print(f" - Catalyst: {row[1]}")
        print(f" - Headline: {row[2]}")
        print(f" - Holding Period: {row[3]}")
        print(f" - Target: ${row[4]}")
        print(f" - Stop: ${row[5]}")
    else:
        print("[TEST 2 FAILED] Could not fetch saved signal from database.")

    # Clean up test alert
    cur.execute(f"DELETE FROM {settings.mimir_schema}.mimir_trade_signals WHERE ticker = 'NVDA' AND headline LIKE '%%TSMC%%'")
    conn.commit()
    cur.close()
    conn.close()

if __name__ == "__main__":
    run_test()
