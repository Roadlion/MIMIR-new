import os, sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import MetaTrader5 as mt5
from datetime import datetime
from backend.app.database import get_db_connection
from backend.app.config import get_settings

settings = get_settings()

print("=" * 75)
print("          MIMIR WEEKLY RECAP AUDIT (OCT 19 - OCT 23, 2026)")
print("=" * 75)

if not mt5.initialize():
    print("MT5 initialize failed:", mt5.last_error())
else:
    acc = mt5.account_info()
    if acc:
        print(f"Account Balance:   ${acc.balance:,.2f}")
        print(f"Account Equity:    ${acc.equity:,.2f}")
        print(f"Open Floating PnL: ${acc.profit:+,.2f}")
    
    # 1. Closed deals this week (Oct 18 00:00 to now)
    from_date = datetime(2026, 10, 18, 0, 0)
    to_date = datetime(2026, 10, 24, 0, 0)
    deals = mt5.history_deals_get(from_date, to_date)
    print("\n" + "-" * 75)
    print("1. CLOSED DEALS THIS WEEK (OCT 19 - 23)")
    print("-" * 75)
    realized_this_week = 0.0
    closed_list = []
    closed_winners = 0
    closed_losers = 0
    gross_win = 0.0
    gross_loss = 0.0
    if deals:
        for d in deals:
            d_dict = d._asdict()
            if d_dict.get('entry') == 1:
                p = d_dict['profit']
                realized_this_week += p
                closed_list.append(d_dict)
                if p > 0:
                    closed_winners += 1
                    gross_win += p
                elif p < 0:
                    closed_losers += 1
                    gross_loss += abs(p)
                dt_str = datetime.fromtimestamp(d_dict['time']).strftime('%m-%d %H:%M')
                print(f"{d_dict['symbol']:<8} Vol: {d_dict['volume']:<5.1f} Exit Price: ${d_dict['price']:<8.2f} PnL: ${p:+9.2f}  Time: {dt_str}")
    print("-" * 75)
    print(f"Total Closed Deals: {len(closed_list)} (Wins: {closed_winners}, Losses: {closed_losers})")
    print(f"Gross Profit: ${gross_win:+,.2f} | Gross Loss: ${gross_loss:,.2f}")
    pf = (gross_win / gross_loss) if gross_loss > 0 else (999.0 if gross_win > 0 else 0.0)
    print(f"Weekly Closed Profit Factor: {pf:.2f}")
    print(f"Total Realized P&L this week: ${realized_this_week:+,.2f}")

    # 2. Open positions right now
    positions = mt5.positions_get()
    print("\n" + "-" * 75)
    print(f"2. ACTIVE OPEN POSITIONS ({len(positions) if positions else 0} POSITIONS)")
    print("-" * 75)
    tot_unrealized = 0.0
    open_win = 0
    open_loss = 0
    if positions:
        print(f"{'TICKER':<8} {'VOL':<6} {'OPEN $':<9} {'CURR $':<9} {'PROFIT $':<12} {'RET %':<9} {'SL':<9} {'TP':<9}")
        print("-" * 75)
        for p in positions:
            d = p._asdict()
            pnl = d['profit']
            tot_unrealized += pnl
            if pnl > 0: open_win += 1
            elif pnl < 0: open_loss += 1
            pct = ((d['price_current'] - d['price_open']) / d['price_open']) * 100.0 if d['price_open'] else 0.0
            print(f"{d['symbol']:<8} {d['volume']:<6.1f} ${d['price_open']:<8.2f} ${d['price_current']:<8.2f} ${pnl:+10.2f} {pct:+6.2f}%  ${d['sl']:<8.2f} ${d['tp']:<8.2f}")
        print("-" * 75)
        print(f"Active Fleet Win Rate: {open_win}/{len(positions)} ({(open_win/len(positions)*100):.1f}%)")
        print(f"Total Net Floating P&L: ${tot_unrealized:+,.2f}")
    
    mt5.shutdown()

print("\n" + "=" * 75)
print("3. POSTGRESQL PAPER TRADE LOG ENTRIES (OCT 19 - 23)")
print("=" * 75)
conn = get_db_connection()
cur = conn.cursor()
cur.execute(f"""
    SELECT ticker, action, entry_price, exit_price, quantity, entry_time, exit_time, exit_reason, realized_pnl, notes
    FROM {settings.mimir_schema}.mimir_paper_trade_log
    WHERE (exit_time >= '2026-10-18' OR entry_time >= '2026-10-18')
    ORDER BY COALESCE(exit_time, entry_time) DESC
""")
db_rows = cur.fetchall()
print(f"DB Log Rows Found: {len(db_rows)}")
for r in db_rows:
    t, act, en_p, ex_p, q, en_t, ex_t, ex_r, pnl, notes = r
    en_str = en_t.strftime('%m-%d %H:%M') if en_t else 'N/A'
    ex_str = ex_t.strftime('%m-%d %H:%M') if ex_t else 'OPEN'
    pnl_str = f"${float(pnl):+,.2f}" if pnl is not None else "OPEN"
    print(f"{t:<6} {act:<4} Qty: {q:<5} En: ${en_p or 0:<7.2f} Ex: ${ex_p or 0:<7.2f} Time: {en_str} -> {ex_str} PnL: {pnl_str} ({ex_r or notes or ''})")

cur.close()
conn.close()
