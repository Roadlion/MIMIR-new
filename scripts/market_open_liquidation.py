# scripts/market_open_liquidation.py
"""
Automated Market-Open Liquidation Engine
========================================
Waits for the US regular market opening bell (09:30:00 AM Eastern Time),
then immediately executes liquidation for MIMIR positions (Magic 202409)
and rogue strategy positions (Magic 202608).

Includes:
- Countdown timer until 09:30:00 ET
- Opening spread/tick verification
- Automatic retry loop for initial market-open latency / requotes
- Audit logging into PostgreSQL mimir_paper_trade_log
"""

import sys
import time
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    import MetaTrader5 as mt5
except ImportError:
    print("[ERROR] MetaTrader 5 Python library not installed.")
    sys.exit(1)

from backend.app.analytics.mt5_bridge import (
    ensure_mt5_connected,
    close_position,
    MIMIR_MAGIC,
    STAT_ARB_MAGIC,
    AUTHORIZED_MAGICS
)

def get_seconds_until_market_open() -> float:
    """Returns seconds remaining until next 09:30:00 AM Eastern Time session."""
    now_ny = datetime.now(ZoneInfo('America/New_York'))
    bell_ny = now_ny.replace(hour=9, minute=30, second=0, microsecond=0)
    
    # If today's bell has already passed today, target tomorrow's bell
    if now_ny >= bell_ny:
        # If market is currently open (between 09:30 and 16:00 ET on weekday), return 0 to execute immediately
        if now_ny.weekday() < 5 and now_ny.hour < 16:
            return 0.0
        # Otherwise target next morning
        bell_ny = bell_ny + timedelta(days=1)
        while bell_ny.weekday() >= 5:  # Skip weekends
            bell_ny += timedelta(days=1)

    return (bell_ny - now_ny).total_seconds()


def close_with_retry(ticket: int, symbol: str, max_retries: int = 10, retry_delay: float = 1.0) -> dict:
    """Attempts to close a position in MT5, retrying on initial opening tick synchronization."""
    last_res = None
    for attempt in range(1, max_retries + 1):
        res = close_position(ticket=ticket)
        if res.get("success"):
            return res
        last_res = res
        retcode = res.get("retcode")
        # 10018: Market is closed (waiting for opening quote)
        # 10004: Requote
        # 10020: Prices changed
        # 10021: No quotes available yet
        if retcode in (10018, 10004, 10020, 10021) and attempt < max_retries:
            time.sleep(retry_delay)
        else:
            return res

    return last_res or {"success": False, "message": f"Failed after {max_retries} retries."}


def run_market_open_liquidation(wait_for_bell: bool = True, close_rogue: bool = True):
    print("=" * 65)
    print(" MIMIR AUTOMATED MARKET-OPEN LIQUIDATION ENGINE")
    print("=" * 65)

    if wait_for_bell:
        sec_left = get_seconds_until_market_open()
        if sec_left > 0:
            now_ny = datetime.now(ZoneInfo('America/New_York'))
            print(f"[TIME] Current NY Time: {now_ny.strftime('%Y-%m-%d %H:%M:%S %Z')}")
            print(f"[TIME] Market Open Bell: 09:30:00 AM ET")
            print(f"[WAIT] Waiting {sec_left:.0f} seconds ({sec_left/60:.1f} mins / {sec_left/3600:.2f} hours) until opening bell...")
            
            while True:
                sec_left = get_seconds_until_market_open()
                if sec_left <= 0:
                    break
                if sec_left > 60:
                    sleep_chunk = min(60.0, sec_left - 5.0)
                    time.sleep(sleep_chunk)
                    now_ny = datetime.now(ZoneInfo('America/New_York'))
                    print(f"  [COUNTDOWN] {sec_left/60:.1f} mins remaining... (Current NY: {now_ny.strftime('%H:%M:%S')})")
                else:
                    time.sleep(max(0.5, sec_left))
                    break

            print("\n[BELL] 09:30:00 AM ET opening bell has arrived! Initializing execution...")
            # Brief 2-second grace period for broker price feed synchronization
            time.sleep(2.0)

    print("\n[MT5] Connecting to MetaTrader 5 terminal...")
    if not ensure_mt5_connected():
        print("[ERROR] Cannot connect to MT5 terminal.")
        return False

    positions = mt5.positions_get()
    if not positions:
        print("[INFO] No active open positions in MT5.")
        return True

    mimir_positions = [p for p in positions if p.magic == MIMIR_MAGIC]
    stat_arb_positions = [p for p in positions if p.magic == STAT_ARB_MAGIC]
    rogue_positions = [p for p in positions if p.magic not in AUTHORIZED_MAGICS]

    print(f"[AUDIT] Active positions found:")
    print(f"  - MIMIR War Rig positions (Magic {MIMIR_MAGIC}): {len(mimir_positions)}")
    print(f"  - Stat-Arb APU positions (Magic {STAT_ARB_MAGIC}): {len(stat_arb_positions)}")
    print(f"  - Unregistered Rogue positions: {len(rogue_positions)}")

    # 1. Close MIMIR legitimate positions
    mimir_closed = 0
    mimir_failed = 0
    print("\n--- PHASE 1: CLOSING MIMIR WAR RIG POSITIONS ---")
    for p in mimir_positions:
        print(f"Closing MIMIR #{p.ticket} {p.symbol} ({p.volume} lots, Open ${p.price_open:.2f})...")
        res = close_with_retry(p.ticket, p.symbol)
        if res.get("success"):
            mimir_closed += 1
            print(f"  [OK] #{p.ticket} {p.symbol} at ${res.get('close_price', 0):.2f} | PnL: ${res.get('profit', 0):.2f}")
            # Audit log into DB
            try:
                settings = get_settings()
                conn = get_db_connection()
                cur = conn.cursor()
                gmt_plus_7 = timezone(timedelta(hours=7))
                now_local = datetime.now(gmt_plus_7)
                cur.execute(f"""
                    INSERT INTO {settings.mimir_schema}.mimir_paper_trade_log
                    (ticker, action, entry_price, exit_price, quantity, entry_time, exit_time, exit_reason, realized_pnl, notes, mt5_ticket)
                    VALUES (%s, 'SELL', %s, %s, %s, %s, %s, 'BELL_LIQUIDATION', %s, 'Market-open user liquidation', %s)
                """, (p.symbol, p.price_open, res.get("close_price", 0.0), p.volume, now_local, now_local, res.get("profit", 0.0), p.ticket))
                # Delete from active portfolio table
                cur.execute(f"""
                    DELETE FROM {settings.mimir_schema}.mimir_paper_portfolio
                    WHERE mt5_ticket = %s
                """, (p.ticket,))
                conn.commit()
                cur.close()
                conn.close()
            except Exception as dbe:
                print(f"  [DB WARNING] {dbe}")
        else:
            mimir_failed += 1
            print(f"  [FAILED] #{p.ticket} {p.symbol} -> {res.get('message')}")

    # 2. Close Rogue positions if requested
    rogue_closed = 0
    rogue_failed = 0
    if close_rogue and rogue_positions:
        print("\n--- PHASE 2: PURGING RESIDUAL ROGUE POSITIONS ---")
        for p in rogue_positions:
            res = close_position(ticket=p.ticket)
            if res.get("success"):
                rogue_closed += 1
                print(f"  [PURGED] #{p.ticket} {p.symbol} -> {res.get('message')}")
            else:
                rogue_failed += 1
                print(f"  [FAILED] #{p.ticket} {p.symbol} -> {res.get('message')}")

    # 3. Close ANY other remaining open positions in MT5
    other_closed = 0
    if other_positions:
        print("\n--- PHASE 3: CLOSING ALL OTHER REMAINING POSITIONS ---")
        for p in other_positions:
            res = close_position(ticket=p.ticket)
            if res.get("success"):
                other_closed += 1
                print(f"  [CLOSED] #{p.ticket} {p.symbol} -> {res.get('message')}")
            else:
                print(f"  [FAILED] #{p.ticket} {p.symbol} -> {res.get('message')}")

    # 4. Final safety sweep: verify zero positions remain
    remaining = mt5.positions_get()
    if remaining:
        print("\n--- FINAL SWEEP: CLOSING REMAINING STUB POSITIONS ---")
        for p in remaining:
            res = close_position(ticket=p.ticket)
            if res.get("success"):
                print(f"  [SWEEP CLOSED] #{p.ticket} {p.symbol}")

    print("\n" + "=" * 65)
    print(f"[SUMMARY] MIMIR Closed: {mimir_closed} | MIMIR Failed: {mimir_failed}")
    if close_rogue:
        print(f"[SUMMARY] Rogue Purged: {rogue_closed} | Rogue Failed: {rogue_failed}")
    if other_positions:
        print(f"[SUMMARY] Other Closed: {other_closed}")
    
    final_remaining = mt5.positions_get()
    rem_count = len(final_remaining) if final_remaining else 0
    print(f"[FINAL] Remaining Active Positions in MT5: {rem_count}")
    print("=" * 65)
    mt5.shutdown()
    return True

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="MIMIR Market Open Liquidation")
    parser.add_argument("--now", action="store_true", help="Execute immediately without waiting for 09:30 ET bell")
    parser.add_argument("--mimir-only", action="store_true", help="Close MIMIR positions only, skip rogue positions")
    args = parser.parse_args()

    run_market_open_liquidation(wait_for_bell=not args.now, close_rogue=not args.mimir_only)
