import os, sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import MetaTrader5 as mt5
from datetime import datetime

if not mt5.initialize():
    print("MT5 initialize failed:", mt5.last_error())
    sys.exit(1)

print("=" * 70)
print("             MIMIR POST-PURGE WEEKLY PERFORMANCE AUDIT")
print("=" * 70)

acc = mt5.account_info()
if acc:
    print(f"Account Balance:   ${acc.balance:,.2f}")
    print(f"Account Equity:    ${acc.equity:,.2f}")
    print(f"Open Floating PnL: ${acc.profit:+,.2f}")

print("\n" + "-" * 70)
print("1. CURRENT ACTIVE OPEN POSITIONS (WAR RIG TRANSMISSION FLEET)")
print("-" * 70)
positions = mt5.positions_get()
tot_unrealized = 0.0
win_pos = 0
loss_pos = 0

if positions:
    print(f"{'TICKER':<8} {'VOL':<6} {'OPEN $':<9} {'CURR $':<9} {'UNREALIZED $':<14} {'RETURN %':<10} {'SL':<9} {'TP':<9}")
    print("-" * 70)
    for p in positions:
        d = p._asdict()
        pnl = d['profit']
        tot_unrealized += pnl
        if pnl > 0: win_pos += 1
        elif pnl < 0: loss_pos += 1
        pct = ((d['price_current'] - d['price_open']) / d['price_open']) * 100.0 if d['price_open'] else 0.0
        print(f"{d['symbol']:<8} {d['volume']:<6.1f} ${d['price_open']:<8.2f} ${d['price_current']:<8.2f} ${pnl:+12.2f} {pct:+7.2f}%   ${d['sl']:<8.2f} ${d['tp']:<8.2f}")
    
    print("-" * 70)
    print(f"Open Positions Count: {len(positions)} | Green: {win_pos} | Red: {loss_pos}")
    print(f"Total Net Floating P&L: ${tot_unrealized:+,.2f}")

print("\n" + "-" * 70)
print("2. CLOSED TRADES POST-PURGE (Since Wednesday Oct 14 00:00 UTC)")
print("-" * 70)
deals = mt5.history_deals_get(datetime(2026, 10, 14, 0, 0), datetime(2026, 10, 18, 0, 0))
post_purge_pnl = 0.0
closed_trades = []
gross_profit = 0.0
gross_loss = 0.0

if deals:
    for d in deals:
        d_dict = d._asdict()
        if d_dict.get('entry') == 1: # Exit
            p = d_dict['profit']
            post_purge_pnl += p
            if p > 0: gross_profit += p
            elif p < 0: gross_loss += abs(p)
            closed_trades.append(d_dict)
            dt_str = datetime.fromtimestamp(d_dict['time']).strftime('%m-%d %H:%M')
            print(f"{d_dict['symbol']:<8} Vol: {d_dict['volume']:<5.1f} Exit $: {d_dict['price']:<8.2f} PnL: ${p:+9.2f}  Time: {dt_str}")

print("-" * 70)
closed_wins = sum(1 for t in closed_trades if t['profit'] > 0)
closed_losses = sum(1 for t in closed_trades if t['profit'] < 0)
win_rate = (closed_wins / len(closed_trades) * 100.0) if closed_trades else 0.0
profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (999.0 if gross_profit > 0 else 0.0)

print(f"Closed Trades Count: {len(closed_trades)}")
print(f"Win Rate:            {win_rate:.1f}% ({closed_wins} wins, {closed_losses} losses)")
print(f"Gross Profit:        ${gross_profit:+,.2f}")
print(f"Gross Loss:          ${gross_loss:+,.2f}")
print(f"Profit Factor:       {profit_factor:.2f}")
print(f"Net Realized PnL:    ${post_purge_pnl:+,.2f}")
print("=" * 70)

mt5.shutdown()
