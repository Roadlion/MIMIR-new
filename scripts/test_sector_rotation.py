import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from backend.app.services.sector_rotation_service import (
    get_sector_rotation_matrix,
    get_ticker_sector_tailwinds,
    scan_sector_rotation_runners
)

def main():
    print("Testing Sector Rotation Matrix...")
    matrix = get_sector_rotation_matrix()
    spy = matrix.get("spy", {})
    print(f"SPY: Last={spy.get('last_close')} 5D={spy.get('return_5d_pct')}%")
    print(f"{'Ticker':<6} {'Sector':<22} {'5D Ret':<10} {'RS vs SPY':<12} {'CMF':<8} {'Phase'}")
    print("-" * 75)
    for s in matrix.get("sectors", []):
        print(f"{s['ticker']:<6} {s['name']:<22} {s['return_5d_pct']:+6.2f}%    {s['rs_vs_spy_5d_pct']:+6.2f}%     {s['cmf_14']:+5.2f}   {s['phase']}")

    print("\nTesting get_ticker_sector_tailwinds...")
    for test_ticker in ["DVN", "MRNA", "AAPL", "XOM", "NVDA"]:
        tw = get_ticker_sector_tailwinds(test_ticker)
        print(f"{test_ticker:<5}: Tailwind={tw['has_tailwind']} Outflow={tw['is_outflow']} Phase={tw['phase']} Reason={tw['reason']}")

    print("\nTesting scan_sector_rotation_runners...")
    signals = scan_sector_rotation_runners()
    print(f"Generated {len(signals)} rotation runner signals.")
    for sig in signals:
        print(f"-> {sig['ticker']} ({sig['sector']}): Trigger=${sig['trigger_price']} Target=${sig['target_price']} Stop=${sig['stop_loss']}")

if __name__ == "__main__":
    main()
