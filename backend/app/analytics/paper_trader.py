# backend/app/analytics/paper_trader.py
import sys
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Any
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.database import get_db_connection, get_db_connection_dict
from backend.app.config import get_settings

settings = get_settings()

def init_paper_trading_db():
    """Initializes paper trading configuration and log tables in the PostgreSQL database."""
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        schema = settings.mimir_schema
        
        # 1. Config Table
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {schema}.mimir_paper_trading_config (
                id SERIAL PRIMARY KEY,
                is_enabled BOOLEAN DEFAULT TRUE,
                execution_mode VARCHAR(20) DEFAULT 'AUTO',
                min_win_rate FLOAT DEFAULT 55.0,
                min_sentiment_score FLOAT DEFAULT 0.0,
                position_size_type VARCHAR(20) DEFAULT 'FIXED_USD',
                position_size_value FLOAT DEFAULT 1000.0,
                initial_capital FLOAT DEFAULT 100000.0,
                stop_loss_pct FLOAT DEFAULT 3.0,
                take_profit_pct FLOAT DEFAULT 6.0,
                auto_exit_on_hold_days BOOLEAN DEFAULT TRUE,
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Seed default row if empty
        cur.execute(f"SELECT COUNT(*) FROM {schema}.mimir_paper_trading_config")
        if cur.fetchone()[0] == 0:
            cur.execute(f"""
                INSERT INTO {schema}.mimir_paper_trading_config 
                (is_enabled, execution_mode, min_win_rate, min_sentiment_score, position_size_type, position_size_value, initial_capital, stop_loss_pct, take_profit_pct, auto_exit_on_hold_days)
                VALUES (TRUE, 'AUTO', 55.0, 0.0, 'FIXED_USD', 1000.0, 100000.0, 3.0, 6.0, TRUE)
            """)

        # 2. Paper Trade Log Table
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {schema}.mimir_paper_trade_log (
                id SERIAL PRIMARY KEY,
                signal_id INTEGER,
                ticker VARCHAR(50) NOT NULL,
                action VARCHAR(10) NOT NULL,
                entry_price FLOAT NOT NULL,
                exit_price FLOAT,
                quantity FLOAT NOT NULL,
                entry_time TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                exit_time TIMESTAMP WITH TIME ZONE,
                exit_reason VARCHAR(50),
                realized_pnl FLOAT,
                realized_pnl_pct FLOAT,
                notes TEXT
            )
        """)

        # 3. Alter mimir_portfolio to include source column if missing
        cur.execute(f"""
            ALTER TABLE {schema}.mimir_portfolio 
            ADD COLUMN IF NOT EXISTS source VARCHAR(50) DEFAULT 'MANUAL';
        """)

        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"[PAPER_TRADER] Database initialization error: {e}")
    finally:
        cur.close()
        conn.close()


def get_paper_config() -> Dict[str, Any]:
    """Retrieves the current paper trading configuration."""
    init_paper_trading_db()
    conn = get_db_connection_dict()
    cur = conn.cursor()
    try:
        cur.execute(f"""
            SELECT is_enabled, execution_mode, min_win_rate, min_sentiment_score, 
                   position_size_type, position_size_value, initial_capital, 
                   stop_loss_pct, take_profit_pct, auto_exit_on_hold_days, updated_at
            FROM {settings.mimir_schema}.mimir_paper_trading_config
            ORDER BY id ASC LIMIT 1
        """)
        row = cur.fetchone()
        if row:
            return dict(row)
        return {
            "is_enabled": True,
            "execution_mode": "AUTO",
            "min_win_rate": 55.0,
            "min_sentiment_score": 0.0,
            "position_size_type": "FIXED_USD",
            "position_size_value": 1000.0,
            "initial_capital": 100000.0,
            "stop_loss_pct": 3.0,
            "take_profit_pct": 6.0,
            "auto_exit_on_hold_days": True
        }
    finally:
        cur.close()
        conn.close()


def update_paper_config(updates: Dict[str, Any]) -> Dict[str, Any]:
    """Updates paper trading configuration parameters."""
    init_paper_trading_db()
    conn = get_db_connection_dict()
    cur = conn.cursor()
    try:
        schema = settings.mimir_schema
        cur.execute(f"""
            UPDATE {schema}.mimir_paper_trading_config
            SET is_enabled = COALESCE(%s, is_enabled),
                execution_mode = COALESCE(%s, execution_mode),
                min_win_rate = COALESCE(%s, min_win_rate),
                min_sentiment_score = COALESCE(%s, min_sentiment_score),
                position_size_type = COALESCE(%s, position_size_type),
                position_size_value = COALESCE(%s, position_size_value),
                initial_capital = COALESCE(%s, initial_capital),
                stop_loss_pct = COALESCE(%s, stop_loss_pct),
                take_profit_pct = COALESCE(%s, take_profit_pct),
                auto_exit_on_hold_days = COALESCE(%s, auto_exit_on_hold_days),
                updated_at = NOW()
            WHERE id = (SELECT id FROM {schema}.mimir_paper_trading_config ORDER BY id ASC LIMIT 1)
        """, (
            updates.get("is_enabled"),
            updates.get("execution_mode"),
            updates.get("min_win_rate"),
            updates.get("min_sentiment_score"),
            updates.get("position_size_type"),
            updates.get("position_size_value"),
            updates.get("initial_capital"),
            updates.get("stop_loss_pct"),
            updates.get("take_profit_pct"),
            updates.get("auto_exit_on_hold_days")
        ))
        conn.commit()
        return get_paper_config()
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        cur.close()
        conn.close()


def auto_execute_pending_alerts() -> Dict[str, Any]:
    """
    Scans pending trade signals in mimir_trade_signals, checks paper trading rules,
    and executes paper trades for qualifying alerts.
    """
    config = get_paper_config()
    if not config.get("is_enabled"):
        return {"executed_count": 0, "message": "Paper trading is currently disabled in settings."}

    conn = get_db_connection_dict()
    cur = conn.cursor()
    executed_count = 0
    executed_details = []

    try:
        schema = settings.mimir_schema
        # Fetch pending alerts joined with ticker parameters for win rate
        cur.execute(f"""
            SELECT s.id, s.ticker, s.signal_type, s.trigger_price, s.rsi_value, s.sentiment_score,
                   s.support_level, s.resistance_level, s.reason, s.created_at,
                   COALESCE(p.win_rate, 50.0) as win_rate
            FROM {schema}.mimir_trade_signals s
            LEFT JOIN {schema}.mimir_ticker_parameters p ON s.ticker = p.ticker
            WHERE s.status = 'PENDING'
            ORDER BY s.created_at ASC
        """)
        pending_alerts = cur.fetchall()

        gmt_plus_7 = timezone(timedelta(hours=7))
        now_local = datetime.now(gmt_plus_7)

        min_win_rate = float(config.get("min_win_rate", 55.0))
        min_sentiment = float(config.get("min_sentiment_score", 0.0))

        for alert in pending_alerts:
            alert_id = alert["id"]
            ticker = alert["ticker"].upper()
            signal_type = alert["signal_type"].upper()
            trigger_price = float(alert["trigger_price"])
            win_rate = float(alert["win_rate"])
            sentiment = float(alert["sentiment_score"] or 0.0)

            # Check filtering rules
            if win_rate < min_win_rate:
                continue
            if sentiment < min_sentiment:
                continue

            # Compute trade quantity based on position sizing
            pos_type = config.get("position_size_type", "FIXED_USD")
            pos_val = float(config.get("position_size_value", 1000.0))

            if pos_type == "FIXED_SHARES":
                qty = pos_val
            else:  # FIXED_USD
                qty = round(pos_val / trigger_price, 4) if trigger_price > 0 else 10.0

            if qty <= 0:
                qty = 1.0

            # Execute transaction in mimir_portfolio
            cur.execute(f"""
                INSERT INTO {schema}.mimir_portfolio 
                (ticker, order_date, buy_price, quantity, transaction_type, source)
                VALUES (%s, %s, %s, %s, %s, 'PAPER_ALERT')
            """, (ticker, now_local, trigger_price, qty, signal_type))

            # Log paper trade execution
            cur.execute(f"""
                INSERT INTO {schema}.mimir_paper_trade_log
                (signal_id, ticker, action, entry_price, quantity, entry_time, exit_reason, notes)
                VALUES (%s, %s, %s, %s, %s, %s, 'ALERT_EXECUTION', %s)
            """, (alert_id, ticker, signal_type, trigger_price, qty, now_local, alert.get("reason")))

            # Mark trade signal as APPROVED / AUTO_TRADED
            cur.execute(f"""
                UPDATE {schema}.mimir_trade_signals
                SET status = 'APPROVED', acted_at = %s
                WHERE id = %s
            """, (now_local, alert_id))

            executed_count += 1
            executed_details.append({
                "alert_id": alert_id,
                "ticker": ticker,
                "signal_type": signal_type,
                "trigger_price": trigger_price,
                "quantity": qty,
                "win_rate": win_rate
            })

        conn.commit()
        return {
            "executed_count": executed_count,
            "executed_details": executed_details,
            "message": f"Successfully auto-executed {executed_count} paper trades based on active alerts."
        }
    except Exception as e:
        conn.rollback()
        print(f"[PAPER_TRADER ERROR] Auto execution error: {e}")
        return {"executed_count": 0, "error": str(e)}
    finally:
        cur.close()
        conn.close()


def process_paper_position_exits() -> Dict[str, Any]:
    """
    Evaluates open paper positions against current real-time prices to enforce
    Stop Loss, Take Profit, and Hold Days Maturity exits.
    """
    config = get_paper_config()
    sl_pct = float(config.get("stop_loss_pct", 3.0))
    tp_pct = float(config.get("take_profit_pct", 6.0))
    auto_exit_hold = config.get("auto_exit_on_hold_days", True)

    conn = get_db_connection_dict()
    cur = conn.cursor()
    closed_count = 0
    closed_details = []

    try:
        schema = settings.mimir_schema
        # Get active paper trades from portfolio (where source = 'PAPER_ALERT' or all paper trades)
        cur.execute(f"""
            SELECT id, ticker, order_date, buy_price, quantity, transaction_type
            FROM {schema}.mimir_portfolio
            WHERE source = 'PAPER_ALERT'
            ORDER BY order_date ASC
        """)
        paper_txs = cur.fetchall()

        if not paper_txs:
            return {"closed_count": 0, "message": "No active paper trade transactions found."}

        # Calculate current net positions per ticker
        holdings = {}
        for tx in paper_txs:
            t = tx["ticker"].upper()
            if t not in holdings:
                holdings[t] = []
            holdings[t].append(tx)

        # Get current prices from yfinance or live cache
        from backend.app.routers.portfolio import fetch_current_prices
        tickers = list(holdings.keys())
        current_prices = fetch_current_prices(tickers)

        gmt_plus_7 = timezone(timedelta(hours=7))
        now_local = datetime.now(gmt_plus_7)

        for ticker, txs in holdings.items():
            curr_price = current_prices.get(ticker, 0.0)
            if curr_price <= 0:
                continue

            # Calculate open quantity and avg price
            net_qty = 0.0
            avg_price = 0.0
            total_cost = 0.0
            first_entry_date = None

            for tx in txs:
                t_type = tx["transaction_type"].upper()
                q = float(tx["quantity"])
                p = float(tx["buy_price"])
                dt = tx["order_date"]
                if first_entry_date is None or dt < first_entry_date:
                    first_entry_date = dt

                if t_type == "BUY":
                    total_cost += q * p
                    net_qty += q
                elif t_type == "SELL":
                    net_qty -= q

            if net_qty <= 0.0001:
                continue  # Position is closed

            avg_price = total_cost / net_qty if net_qty > 0 else 0.0
            if avg_price <= 0:
                continue

            # Calculate price movement %
            pnl_pct = ((curr_price - avg_price) / avg_price) * 100.0

            exit_reason = None
            if tp_pct > 0 and pnl_pct >= tp_pct:
                exit_reason = "TAKE_PROFIT"
            elif sl_pct > 0 and pnl_pct <= -sl_pct:
                exit_reason = "STOP_LOSS"
            elif auto_exit_hold and first_entry_date:
                # Check hold days maturity
                cur.execute(f"SELECT optimal_hold_days FROM {schema}.mimir_ticker_parameters WHERE ticker = %s", (ticker,))
                row = cur.fetchone()
                hold_days = int(row["optimal_hold_days"]) if row and row["optimal_hold_days"] else 10
                if first_entry_date + timedelta(days=hold_days) <= now_local:
                    exit_reason = "HOLD_EXPIRATION"

            if exit_reason:
                # Close the paper position by inserting opposing SELL order
                realized_pnl = net_qty * (curr_price - avg_price)
                realized_pnl_pct = pnl_pct

                cur.execute(f"""
                    INSERT INTO {schema}.mimir_portfolio
                    (ticker, order_date, buy_price, quantity, transaction_type, source)
                    VALUES (%s, %s, %s, %s, 'SELL', 'PAPER_ALERT')
                """, (ticker, now_local, curr_price, net_qty))

                # Update paper trade log exit info
                cur.execute(f"""
                    INSERT INTO {schema}.mimir_paper_trade_log
                    (ticker, action, entry_price, exit_price, quantity, entry_time, exit_time, exit_reason, realized_pnl, realized_pnl_pct, notes)
                    VALUES (%s, 'SELL', %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (ticker, avg_price, curr_price, net_qty, first_entry_date, now_local, exit_reason, realized_pnl, realized_pnl_pct, f"Auto-exit triggered by {exit_reason}"))

                closed_count += 1
                closed_details.append({
                    "ticker": ticker,
                    "quantity": net_qty,
                    "avg_entry_price": avg_price,
                    "exit_price": curr_price,
                    "exit_reason": exit_reason,
                    "realized_pnl": realized_pnl,
                    "realized_pnl_pct": realized_pnl_pct
                })

        conn.commit()
        return {
            "closed_count": closed_count,
            "closed_details": closed_details,
            "message": f"Processed paper positions. Auto-closed {closed_count} positions based on SL/TP/Maturity rules."
        }
    except Exception as e:
        conn.rollback()
        print(f"[PAPER_TRADER ERROR] Position exit evaluation error: {e}")
        return {"closed_count": 0, "error": str(e)}
    finally:
        cur.close()
        conn.close()


def get_paper_trading_summary() -> Dict[str, Any]:
    """
    Returns full paper trading performance statistics, active positions,
    and recent trade log history.
    """
    config = get_paper_config()
    initial_capital = float(config.get("initial_capital", 100000.0))

    conn = get_db_connection_dict()
    cur = conn.cursor()
    try:
        schema = settings.mimir_schema

        # Fetch paper transactions
        cur.execute(f"""
            SELECT id, ticker, order_date, buy_price, quantity, transaction_type, created_at, source
            FROM {schema}.mimir_portfolio
            WHERE source = 'PAPER_ALERT'
            ORDER BY order_date ASC
        """)
        txs = cur.fetchall()

        # Group transactions by ticker
        raw_holdings = {}
        for tx in txs:
            t = tx["ticker"].upper()
            if t not in raw_holdings:
                raw_holdings[t] = []
            raw_holdings[t].append(tx)

        # Get live current prices
        from backend.app.routers.portfolio import fetch_current_prices
        tickers = list(raw_holdings.keys())
        current_prices = fetch_current_prices(tickers)

        active_positions = {}
        total_open_cost = 0.0
        total_open_value = 0.0
        total_realized_pnl = 0.0

        for ticker, t_list in raw_holdings.items():
            qty_sum = 0.0
            cost_basis = 0.0
            realized_pl = 0.0

            for tx in t_list:
                q = float(tx["quantity"])
                p = float(tx["buy_price"])
                ttype = tx["transaction_type"].upper()

                if ttype == "BUY":
                    if qty_sum + q > 0:
                        cost_basis = (qty_sum * cost_basis + q * p) / (qty_sum + q)
                    qty_sum += q
                elif ttype == "SELL":
                    realized_pl += q * (p - cost_basis)
                    qty_sum -= q
                    if qty_sum <= 0:
                        qty_sum = 0.0
                        cost_basis = 0.0

            total_realized_pnl += realized_pl

            if qty_sum > 0.0001:
                curr_price = current_prices.get(ticker, cost_basis)
                curr_val = qty_sum * curr_price
                open_cost = qty_sum * cost_basis
                unrealized_pl = curr_val - open_cost
                unrealized_pl_pct = (unrealized_pl / open_cost * 100.0) if open_cost > 0 else 0.0

                total_open_cost += open_cost
                total_open_value += curr_val

                active_positions[ticker] = {
                    "ticker": ticker,
                    "quantity": qty_sum,
                    "avg_entry_price": cost_basis,
                    "current_price": curr_price,
                    "total_cost": open_cost,
                    "current_value": curr_val,
                    "unrealized_pnl": unrealized_pl,
                    "unrealized_pnl_pct": unrealized_pl_pct
                }

        total_unrealized_pnl = total_open_value - total_open_cost
        cash_balance = initial_capital - total_open_cost + total_realized_pnl
        current_equity = cash_balance + total_open_value
        total_pnl = current_equity - initial_capital
        total_pnl_pct = (total_pnl / initial_capital * 100.0) if initial_capital > 0 else 0.0

        # Fetch Paper Trade History Logs
        cur.execute(f"""
            SELECT id, signal_id, ticker, action, entry_price, exit_price, quantity,
                   entry_time, exit_time, exit_reason, realized_pnl, realized_pnl_pct, notes
            FROM {schema}.mimir_paper_trade_log
            ORDER BY id DESC
            LIMIT 50
        """)
        logs = [dict(r) for r in cur.fetchall()]

        # Compute win rate from closed trades log
        cur.execute(f"""
            SELECT COUNT(*) as total_closed,
                   COUNT(CASE WHEN realized_pnl > 0 THEN 1 END) as win_count
            FROM {schema}.mimir_paper_trade_log
            WHERE realized_pnl IS NOT NULL
        """)
        stats_row = cur.fetchone()
        total_closed = stats_row["total_closed"] if stats_row else 0
        win_count = stats_row["win_count"] if stats_row else 0
        win_rate_pct = (win_count / total_closed * 100.0) if total_closed > 0 else 0.0

        return {
            "config": config,
            "initial_capital": initial_capital,
            "current_equity": current_equity,
            "cash_balance": cash_balance,
            "total_open_value": total_open_value,
            "total_pnl": total_pnl,
            "total_pnl_pct": total_pnl_pct,
            "total_realized_pnl": total_realized_pnl,
            "total_unrealized_pnl": total_unrealized_pnl,
            "total_trades_logged": len(logs),
            "total_closed_trades": total_closed,
            "win_rate_pct": win_rate_pct,
            "active_positions": active_positions,
            "trade_logs": logs
        }
    finally:
        cur.close()
        conn.close()


def close_paper_position(ticker: str) -> Dict[str, Any]:
    """Manually closes an active paper position for a given ticker."""
    conn = get_db_connection_dict()
    cur = conn.cursor()
    try:
        schema = settings.mimir_schema
        ticker_clean = ticker.upper().strip()

        # Check existing paper position
        cur.execute(f"""
            SELECT transaction_type, quantity, buy_price, order_date
            FROM {schema}.mimir_portfolio
            WHERE ticker = %s AND source = 'PAPER_ALERT'
            ORDER BY order_date ASC
        """, (ticker_clean,))
        txs = cur.fetchall()

        net_qty = 0.0
        total_cost = 0.0
        first_entry = None
        for tx in txs:
            q = float(tx["quantity"])
            p = float(tx["buy_price"])
            if first_entry is None or tx["order_date"] < first_entry:
                first_entry = tx["order_date"]
            if tx["transaction_type"].upper() == "BUY":
                total_cost += q * p
                net_qty += q
            elif tx["transaction_type"].upper() == "SELL":
                net_qty -= q

        if net_qty <= 0.0001:
            return {"success": False, "message": f"No active paper position found for ticker {ticker_clean}."}

        avg_cost = total_cost / net_qty if net_qty > 0 else 0.0

        # Fetch current price
        from backend.app.routers.portfolio import fetch_current_prices
        prices = fetch_current_prices([ticker_clean])
        curr_price = prices.get(ticker_clean, avg_cost)

        gmt_plus_7 = timezone(timedelta(hours=7))
        now_local = datetime.now(gmt_plus_7)

        realized_pnl = net_qty * (curr_price - avg_cost)
        realized_pnl_pct = ((curr_price - avg_cost) / avg_cost * 100.0) if avg_cost > 0 else 0.0

        # Insert manual closing sell transaction
        cur.execute(f"""
            INSERT INTO {schema}.mimir_portfolio
            (ticker, order_date, buy_price, quantity, transaction_type, source)
            VALUES (%s, %s, %s, %s, 'SELL', 'PAPER_ALERT')
        """, (ticker_clean, now_local, curr_price, net_qty))

        # Log manual exit
        cur.execute(f"""
            INSERT INTO {schema}.mimir_paper_trade_log
            (ticker, action, entry_price, exit_price, quantity, entry_time, exit_time, exit_reason, realized_pnl, realized_pnl_pct, notes)
            VALUES (%s, 'SELL', %s, %s, %s, %s, %s, 'MANUAL_CLOSE', %s, %s, 'Manually closed by user')
        """, (ticker_clean, avg_cost, curr_price, net_qty, first_entry or now_local, now_local, realized_pnl, realized_pnl_pct))

        conn.commit()
        return {
            "success": True,
            "message": f"Closed paper position for {ticker_clean} ({net_qty} shares) at ${curr_price:.2f}.",
            "realized_pnl": realized_pnl,
            "realized_pnl_pct": realized_pnl_pct
        }
    except Exception as e:
        conn.rollback()
        return {"success": False, "message": f"Error closing position: {str(e)}"}
    finally:
        cur.close()
        conn.close()


def reset_paper_account() -> Dict[str, Any]:
    """Resets paper portfolio transactions and trade logs back to default capital."""
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        schema = settings.mimir_schema
        cur.execute(f"DELETE FROM {schema}.mimir_portfolio WHERE source = 'PAPER_ALERT'")
        cur.execute(f"TRUNCATE TABLE {schema}.mimir_paper_trade_log RESTART IDENTITY")
        conn.commit()
        return {"success": True, "message": "Paper trading account and portfolio history reset successfully."}
    except Exception as e:
        conn.rollback()
        return {"success": False, "error": str(e)}
    finally:
        cur.close()
        conn.close()
