# scripts/close_rogue_positions.py
"""
Utility script to purge and close all rogue open positions in MT5
created by the unvetted user strategy runners (Magic: 202608),
while strictly preserving legitimate War Rig Convergence trades (Magic: 202409).
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    import MetaTrader5 as mt5
except ImportError:
    print("[ERROR] MetaTrader5 library is not installed in this environment.")
    sys.exit(1)

from backend.app.analytics.mt5_bridge import (
    ensure_mt5_connected,
    close_position,
    MIMIR_MAGIC,
    STAT_ARB_MAGIC,
    AUTHORIZED_MAGICS
)

def purge_rogue_positions():
    print(f"[PURGE] Initializing connection to MetaTrader 5...")
    if not mt5.initialize():
        print(f"[ERROR] Failed to connect to MT5 terminal: {mt5.last_error()}")
        print("[TIP] Ensure MetaTrader 5 is launched on your desktop and connected to the trade server.")
        return False

    positions = mt5.positions_get()
    if positions is None:
        print("[INFO] No active positions found in MT5.")
        mt5.shutdown()
        return True

    print(f"[INFO] Total open positions in MT5: {len(positions)}")
    
    war_rig_positions = [p for p in positions if p.magic == MIMIR_MAGIC]
    stat_arb_positions = [p for p in positions if p.magic == STAT_ARB_MAGIC]
    rogue_positions = [p for p in positions if p.magic not in AUTHORIZED_MAGICS]

    print(f"[AUDIT] War Rig legitimate positions (Magic {MIMIR_MAGIC}): {len(war_rig_positions)}")
    print(f"[AUDIT] Stat-Arb APU legitimate positions (Magic {STAT_ARB_MAGIC}): {len(stat_arb_positions)}")
    print(f"[AUDIT] Rogue strategy positions to close (Unregistered Magic): {len(rogue_positions)}")

    if not rogue_positions:
        print("[SUCCESS] No rogue positions found. Clean state.")
        mt5.shutdown()
        return True

    closed_count = 0
    failed_count = 0

    for p in rogue_positions:
        ticket = p.ticket
        symbol = p.symbol
        pos_type = "BUY" if p.type == 0 else "SELL"
        vol = p.volume
        
        # Use close_position from mt5_bridge
        res = close_position(ticket=ticket)
        if res.get("success"):
            closed_count += 1
            print(f"  [CLOSED] #{ticket} {symbol} ({pos_type} {vol} lots) -> {res.get('message')}")
        else:
            failed_count += 1
            print(f"  [FAILED] #{ticket} {symbol} -> {res.get('message')}")

    print("\n" + "=" * 50)
    print(f"[SUMMARY] Successfully closed: {closed_count} | Failed: {failed_count}")
    
    remaining = mt5.positions_get()
    remaining_count = len(remaining) if remaining else 0
    print(f"[REMAINING] Active positions in MT5: {remaining_count}")
    if remaining:
        for r in remaining:
            print(f"  #{r.ticket} {r.symbol} (Magic: {r.magic}) | PnL: ${r.profit:.2f}")

    mt5.shutdown()
    return True

if __name__ == "__main__":
    purge_rogue_positions()
