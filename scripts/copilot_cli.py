# scripts/copilot_cli.py
"""
MIMIR Co-Pilot Command-Line Interface (CLI)
===========================================
Easy utility to view status, configure budgets, record IRL trades,
and trigger position monitoring from the terminal.

Usage:
  python scripts/copilot_cli.py status [--user ID]
  python scripts/copilot_cli.py set-budget --user ID --budget USD --dry-powder USD [--risk PCT] [--max-pos PCT] [--max-sector PCT]
  python scripts/copilot_cli.py set-discord --user ID [--webhook URL] [--discord-id ID]
  python scripts/copilot_cli.py buy --user ID --ticker SYMBOL --shares N --price USD --sl USD [--tp USD] [--days N]
  python scripts/copilot_cli.py sell --pos POSITION_ID --price USD [--reason REASON]
  python scripts/copilot_cli.py monitor
"""

import sys
import os
import argparse

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from backend.app.services.copilot_allocator import (
    get_copilot_users,
    get_user_portfolio_state,
    record_user_buy,
    record_user_sell
)
from backend.app.services.copilot_monitor import check_and_update_copilot_positions
from backend.app.database import get_db_connection


def cmd_status(args):
    users = get_copilot_users()
    target_user_id = args.user

    for u in users:
        if target_user_id and u["id"] != target_user_id:
            continue

        state = get_user_portfolio_state(u["id"])
        print("\n" + "=" * 65)
        print(f"👤 USER: {u['username']} (ID: {u['id']})")
        print("=" * 65)
        print(f"  💵 Total Equity:       ${state['total_equity']:,.2f}")
        print(f"  💰 Dry Powder (Cash):  ${state['dry_powder']:,.2f}")
        print(f"  📈 Invested Value:     ${state['invested_value']:,.2f}")
        print(f"  🛡️ Risk Per Trade:     {state['risk_per_trade_pct']}% of equity")
        print(f"  🧱 Max Position Cap:   {state['max_position_pct']}%")
        print(f"  🧱 Max Sector Cap:     {state['max_sector_pct']}%")
        print(f"  📡 Discord Webhook:    {u.get('discord_webhook_trades') or 'Default system webhook'}")

        print("\n  📦 ACTIVE REAL-LIFE HOLDINGS:")
        positions = state.get("open_positions", [])
        if not positions:
            print("     (No active open positions)")
        else:
            for p in positions:
                tp_str = f" | Target: ${p['target_price']:,.2f}" if p.get("target_price") else ""
                print(f"     • #{p['position_id']} {p['ticker']}: {p['shares']} shares @ ${p['entry_price']:,.2f} "
                      f"(${p['current_value']:,.2f}) | SL: ${p['stop_loss']:,.2f}{tp_str} | Sector: {p['sector']}")

        print("\n  📊 SECTOR CONCENTRATION:")
        weights = state.get("sector_weights", {})
        if not weights:
            print("     (All capital in cash / 0% invested)")
        else:
            for sec, w in weights.items():
                print(f"     • {sec}: {w:.1f}% (Max: {state['max_sector_pct']}%)")


def cmd_set_budget(args):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            updates = []
            vals = []
            if args.budget is not None:
                updates.append("total_budget = %s")
                vals.append(args.budget)
            if args.dry_powder is not None:
                updates.append("dry_powder = %s")
                vals.append(args.dry_powder)
            if args.risk is not None:
                updates.append("risk_per_trade_pct = %s")
                vals.append(args.risk)
            if args.max_pos is not None:
                updates.append("max_position_pct = %s")
                vals.append(args.max_pos)
            if args.max_sector is not None:
                updates.append("max_sector_pct = %s")
                vals.append(args.max_sector)

            if not updates:
                print("No budget parameters specified.")
                return

            vals.append(args.user)
            cur.execute(f"""
                UPDATE yggdrasil.mimir_users
                SET {', '.join(updates)}
                WHERE id = %s
            """, tuple(vals))
            conn.commit()
    print(f"✅ Budget settings updated successfully for User {args.user}!")


def cmd_set_discord(args):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            updates = []
            vals = []
            if args.webhook:
                updates.append("discord_webhook_trades = %s")
                vals.append(args.webhook)
            if args.discord_id:
                updates.append("discord_id = %s")
                vals.append(args.discord_id)

            if not updates:
                print("No Discord parameters specified.")
                return

            vals.append(args.user)
            cur.execute(f"""
                UPDATE yggdrasil.mimir_users
                SET {', '.join(updates)}
                WHERE id = %s
            """, tuple(vals))
            conn.commit()
    print(f"✅ Discord settings updated successfully for User {args.user}!")


def cmd_buy(args):
    res = record_user_buy(
        user_id=args.user,
        ticker=args.ticker,
        shares=args.shares,
        entry_price=args.price,
        stop_loss=args.sl,
        target_price=args.tp,
        holding_period_days=args.days or 7
    )
    if res.get("success"):
        print(f"✅ Position #{res['position_id']} recorded!")
        print(f"   Bought {res['shares']} shares of {res['ticker']} @ ${res['entry_price']:,.2f}")
        print(f"   Total Cost: ${res['total_cost']:,.2f}")
        print(f"   Remaining Dry Powder: ${res['remaining_dry_powder']:,.2f}")
    else:
        print(f"❌ Buy failed: {res.get('error')}")


def cmd_sell(args):
    res = record_user_sell(
        position_id=args.pos,
        exit_price=args.price,
        close_reason=args.reason or "MANUAL"
    )
    if res.get("success"):
        print(f"✅ Position #{args.pos} closed ({res['close_reason']})!")
        print(f"   Sold {res['shares']} shares of {res['ticker']} @ ${res['exit_price']:,.2f}")
        print(f"   Realized P&L: {'+' if res['realized_pnl'] >= 0 else ''}${res['realized_pnl']:,.2f} ({res['return_pct']:+.1f}%)")
        print(f"   Cash Recovered: ${res['cash_recovered']:,.2f}")
        print(f"   New Dry Powder: ${res['new_dry_powder']:,.2f}")
    else:
        print(f"❌ Sell failed: {res.get('error')}")


def cmd_monitor(args):
    print("🔍 Running Co-Pilot IRL Exit & Ratchet Monitor Cycle...")
    res = check_and_update_copilot_positions()
    print(f"   Ratchets Triggered: {res['ratchets_triggered']}")
    print(f"   Exits Triggered:    {res['exits_triggered']}")
    for d in res.get("details", []):
        print(f"   • {d}")


def main():
    parser = argparse.ArgumentParser(description="MIMIR Co-Pilot Portfolio Manager")
    subparsers = parser.add_subparsers(dest="command")

    # status
    p_status = subparsers.add_parser("status", help="View user portfolio and dry powder status")
    p_status.add_argument("--user", type=int, help="User ID (optional)")

    # set-budget
    p_budget = subparsers.add_parser("set-budget", help="Set user budget and risk parameters")
    p_budget.add_argument("--user", type=int, required=True, help="User ID")
    p_budget.add_argument("--budget", type=float, help="Total portfolio capital")
    p_budget.add_argument("--dry-powder", type=float, help="Available cash")
    p_budget.add_argument("--risk", type=float, help="Risk per trade pct (e.g. 1.0)")
    p_budget.add_argument("--max-pos", type=float, help="Max single position pct (e.g. 10.0)")
    p_budget.add_argument("--max-sector", type=float, help="Max sector concentration pct (e.g. 25.0)")

    # set-discord
    p_disc = subparsers.add_parser("set-discord", help="Set user Discord notification target")
    p_disc.add_argument("--user", type=int, required=True, help="User ID")
    p_disc.add_argument("--webhook", type=str, help="Discord webhook URL")
    p_disc.add_argument("--discord-id", type=str, help="Discord User ID for @mentions")

    # buy
    p_buy = subparsers.add_parser("buy", help="Record real-life buy execution")
    p_buy.add_argument("--user", type=int, required=True, help="User ID")
    p_buy.add_argument("--ticker", type=str, required=True, help="Ticker symbol")
    p_buy.add_argument("--shares", type=float, required=True, help="Number of shares")
    p_buy.add_argument("--price", type=float, required=True, help="Entry price")
    p_buy.add_argument("--sl", type=float, required=True, help="Stop loss price")
    p_buy.add_argument("--tp", type=float, help="Profit target price")
    p_buy.add_argument("--days", type=int, default=7, help="Holding horizon in days")

    # sell
    p_sell = subparsers.add_parser("sell", help="Record real-life sell execution")
    p_sell.add_argument("--pos", type=int, required=True, help="Position ID")
    p_sell.add_argument("--price", type=float, required=True, help="Exit price")
    p_sell.add_argument("--reason", type=str, default="MANUAL", help="Exit reason")

    # monitor
    subparsers.add_parser("monitor", help="Scan open positions for ratchets and exits")

    args = parser.parse_args()
    if args.command == "status":
        cmd_status(args)
    elif args.command == "set-budget":
        cmd_set_budget(args)
    elif args.command == "set-discord":
        cmd_set_discord(args)
    elif args.command == "buy":
        cmd_buy(args)
    elif args.command == "sell":
        cmd_sell(args)
    elif args.command == "monitor":
        cmd_monitor(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
