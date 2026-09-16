import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from backend.app.analytics.catalyst_engine import scan_pre_earnings_catalysts
from backend.app.database import get_db_connection

def main():
    print("Testing Uncapped Pre-Earnings Beat Engine...")
    conn = get_db_connection()
    sigs = scan_pre_earnings_catalysts(conn=conn)
    print(f"\nTotal Pre-Earnings Signals Generated: {len(sigs)}")
    for s in sigs:
        upside = ((s['target_price'] / s['trigger_price']) - 1) * 100
        downside = (1 - (s['stop_loss'] / s['trigger_price'])) * 100
        rr = upside / downside if downside > 0 else 0
        print(f"\n[PRE-EARNINGS SETUP] {s['ticker']}")
        print(f"Trigger: ${s['trigger_price']:.2f} | Target: ${s['target_price']:.2f} (+{upside:.1f}%) | Stop: ${s['stop_loss']:.2f} (-{downside:.1f}%) | R/R: {rr:.2f}:1")
        print(f"Holding: {s['holding_period']}")
        print(f"Thesis Excerpt:\n{s['investment_thesis'][:250]}...")
    conn.close()

if __name__ == "__main__":
    main()
