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

def is_us_stock(ticker: str) -> bool:
    """
    Returns True if ticker represents a major US stock listed on NYSE, NASDAQ, or AMEX (tradable on Dime).
    Filters out OTC stocks (5-letter tickers ending in F or Y), crypto (-USD), forex (=X), commodities (=F),
    and foreign exchange tickers with dots (.L, .BK, .DE, .NS, .SS, .SZ, .HK, .KS, .KQ, .TW, .SR, .SI, etc.).
    """
    if not ticker:
        return False
    t = ticker.strip().upper()
    
    # Exclude Crypto (-USD), Forex (=X), Commodities (=F)
    if "-USD" in t or "=X" in t or "=F" in t:
        return False

    # Exclude foreign exchange extensions with dots (e.g. .BK, .L, .DE, .NS, .SS, .SZ, .TO, .PA, .HK, .KS, .KQ, .TW, .SR, .SI)
    if "." in t:
        return False
        
    # Standard US equities consist of 1 to 5 alphabetical characters (e.g. AAPL, MSFT, TSLA, NVDA, AMD, F, T).
    # 5-letter tickers ending in 'F' or 'Y' denote OTC Foreign Ordinary shares and OTC ADRs (e.g. CPNFF, PILBF, HWAUF, ANPDY, VDMCY).
    if t.isalpha() and 1 <= len(t) <= 5:
        if len(t) == 5 and t[-1] in ('F', 'Y'):
            return False
        return True
    return False

# Execution blackout periods (all times ET)
EXECUTION_BLACKOUT = {
    'market_open': ('09:30', '10:15'),    # Retail emotion zone — do NOT enter
    'market_close': ('15:45', '16:00'),   # End-of-day volatility — avoid
}

REGIME_EXIT_RULES = {
    'ACCUMULATING': {'stop_loss': -4.0, 'take_profit': 10.0, 'trailing_stop': None},
    'ALIGNED': {'stop_loss': -2.5, 'take_profit': 6.0, 'trailing_stop': -2.0},
    'EXHAUSTED': {'stop_loss': -1.5, 'take_profit': 3.0, 'trailing_stop': -1.0},
    'PANIC_OVERSOLD': {'stop_loss': -5.0, 'take_profit': 12.0, 'trailing_stop': None},
    'NEUTRAL': {'stop_loss': -3.0, 'take_profit': 6.0, 'trailing_stop': None},
}

TIER_HOLD_LIMITS = {
    'tier_2': {'min_hold': 2, 'max_hold': 7, 'optimal_hold': 5},
    'tier_3': {'min_hold': 5, 'max_hold': 15, 'optimal_hold': 10},
    'direct': {'min_hold': 1, 'max_hold': 10, 'optimal_hold': 5},
}

def is_execution_allowed(ignore_market_hours: bool = False) -> bool:
    """
    Checks if current time is within US regular market hours (Mon-Fri 09:30-16:00 ET)
    and outside retail execution blackout windows (09:30-10:15 & 15:45-16:00 ET).
    If ignore_market_hours is True, allows execution anytime for testing.
    """
    if ignore_market_hours:
        return True
    try:
        from zoneinfo import ZoneInfo
        now_et = datetime.now(ZoneInfo('America/New_York'))
        
        # 1. Weekend check (Saturday = 5, Sunday = 6)
        if now_et.weekday() >= 5:
            print(f"[PAPER_TRADER] Execution paused: Weekend ({now_et.strftime('%A')}). Market is closed.")
            return False

        current_time = now_et.strftime('%H:%M')
        
        # 2. Regular session hours check (09:30 to 16:00 ET)
        if current_time < '09:30' or current_time > '16:00':
            print(f"[PAPER_TRADER] Execution paused: Outside regular market hours ({current_time} ET). Only regular session (09:30-16:00 ET Mon-Fri) is allowed.")
            return False

        # 3. Monday Morning Strategy delay
        if now_et.weekday() == 0 and current_time < '10:30':
            print("[PAPER_TRADER] Execution paused: Monday Morning Strategy delay until 10:30 AM ET.")
            return False
            
        # 4. Blackout windows within regular session
        for name, (start, end) in EXECUTION_BLACKOUT.items():
            if start <= current_time <= end:
                print(f"[PAPER_TRADER] Execution paused during blackout window ({name}: {start}-{end} ET).")
                return False
                
        return True
    except Exception as e:
        print(f"[PAPER_TRADER] Warning during execution time check: {e}")
        return False

def check_herd_arrival(ticker: str, conn=None) -> bool:
    """When social volume spikes > 3x AND unanimity > 0.85, retail herd has arrived."""
    try:
        from .sentiment_momentum import compute_unanimity, compute_attention_decay
        unanimity = compute_unanimity(ticker, lookback_days=5, conn=conn)
        attention = compute_attention_decay(ticker, conn=conn)
        return unanimity >= 0.85 and attention >= 0.80
    except Exception:
        return False


def init_paper_trading_db():
    """Initializes paper trading configuration, paper portfolio, and log tables in the PostgreSQL database."""
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
                position_size_value FLOAT DEFAULT 20.0,
                initial_capital FLOAT DEFAULT 200.0,
                stop_loss_pct FLOAT DEFAULT 3.0,
                take_profit_pct FLOAT DEFAULT 6.0,
                auto_exit_on_hold_days BOOLEAN DEFAULT TRUE,
                us_stocks_only BOOLEAN DEFAULT TRUE,
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Add column if missing in existing table
        cur.execute(f"""
            ALTER TABLE {schema}.mimir_paper_trading_config
            ADD COLUMN IF NOT EXISTS us_stocks_only BOOLEAN DEFAULT TRUE;
        """)

        # Seed default row if empty
        cur.execute(f"SELECT COUNT(*) FROM {schema}.mimir_paper_trading_config")
        if cur.fetchone()[0] == 0:
            cur.execute(f"""
                INSERT INTO {schema}.mimir_paper_trading_config 
                (is_enabled, execution_mode, min_win_rate, min_sentiment_score, position_size_type, position_size_value, initial_capital, stop_loss_pct, take_profit_pct, auto_exit_on_hold_days, us_stocks_only)
                VALUES (TRUE, 'AUTO', 55.0, 0.0, 'FIXED_USD', 20.0, 200.0, 3.0, 6.0, TRUE, TRUE)
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

        # 3. Dedicated Paper Portfolio Table (strictly isolated from real portfolio)
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {schema}.mimir_paper_portfolio (
                id SERIAL PRIMARY KEY,
                ticker VARCHAR(50) NOT NULL,
                order_date TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                buy_price FLOAT NOT NULL,
                quantity FLOAT NOT NULL,
                transaction_type VARCHAR(10) DEFAULT 'BUY',
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            );
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
                   stop_loss_pct, take_profit_pct, auto_exit_on_hold_days, us_stocks_only, updated_at
            FROM {settings.mimir_schema}.mimir_paper_trading_config
            ORDER BY id ASC LIMIT 1
        """)
        row = cur.fetchone()
        if row:
            res = dict(row)
            if res.get("us_stocks_only") is None:
                res["us_stocks_only"] = True
            return res
        return {
            "is_enabled": True,
            "execution_mode": "AUTO",
            "min_win_rate": 55.0,
            "min_sentiment_score": 0.0,
            "position_size_type": "FIXED_USD",
            "position_size_value": 20.0,
            "initial_capital": 200.0,
            "stop_loss_pct": 3.0,
            "take_profit_pct": 6.0,
            "auto_exit_on_hold_days": True,
            "us_stocks_only": True
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
                us_stocks_only = COALESCE(%s, us_stocks_only),
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
            updates.get("auto_exit_on_hold_days"),
            updates.get("us_stocks_only")
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
    restricts execution to US stocks only (if enabled), prevents position stacking,
    and executes small/fractional paper trades up to starting capital ($200 default).
    """
    config = get_paper_config()
    if not config.get("is_enabled"):
        return {"executed_count": 0, "message": "Paper trading is currently disabled in settings."}

    ignore_hours = config.get("ignore_market_hours", False)
    if not is_execution_allowed(ignore_market_hours=ignore_hours):
        return {"executed_count": 0, "message": "Paper trading execution paused: outside US regular market hours (Mon-Fri 09:30-16:00 ET) or during blackout window."}

    initial_capital = float(config.get("initial_capital", 200.0))
    us_only = config.get("us_stocks_only", True)

    conn = get_db_connection_dict()
    cur = conn.cursor()
    executed_count = 0
    executed_details = []

    try:
        schema = settings.mimir_schema

        # Fetch active paper positions and compute current portfolio cash balance
        cur.execute(f"""
            SELECT ticker, transaction_type, quantity, buy_price
            FROM {schema}.mimir_paper_portfolio
            ORDER BY order_date ASC
        """)
        existing_txs = cur.fetchall()

        active_qtys = {}
        active_costs = {}
        total_open_cost = 0.0
        total_realized_pnl = 0.0

        for tx in existing_txs:
            t = tx["ticker"].upper()
            q = float(tx["quantity"])
            p = float(tx["buy_price"])
            ttype = tx["transaction_type"].upper()

            if t not in active_qtys:
                active_qtys[t] = 0.0
                active_costs[t] = 0.0

            if ttype == "BUY":
                if active_qtys[t] + q > 0:
                    active_costs[t] = (active_qtys[t] * active_costs[t] + q * p) / (active_qtys[t] + q)
                active_qtys[t] += q
            elif ttype == "SELL":
                total_realized_pnl += q * (p - active_costs[t])
                active_qtys[t] -= q
                if active_qtys[t] <= 0:
                    active_qtys[t] = 0.0
                    active_costs[t] = 0.0

        for t, q in active_qtys.items():
            if q > 0.0001:
                total_open_cost += q * active_costs[t]

        current_cash = initial_capital - total_open_cost + total_realized_pnl

        # Fetch pending alerts joined with ticker parameters
        cur.execute(f"""
            SELECT s.id, s.ticker, s.signal_type, s.trigger_price, s.rsi_value, s.sentiment_score,
                   s.support_level, s.resistance_level, s.reason, s.created_at,
                   COALESCE(s.conviction_score, 0.5) as conviction_score, s.catalyst_type,
                   COALESCE(p.win_rate, 50.0) as win_rate
            FROM {schema}.mimir_trade_signals s
            LEFT JOIN {schema}.mimir_ticker_parameters p ON s.ticker = p.ticker
            WHERE s.status = 'PENDING'
            ORDER BY s.created_at ASC
        """)
        pending_alerts = cur.fetchall()

        gmt_plus_7 = timezone(timedelta(hours=7))
        now_local = datetime.now(gmt_plus_7)

        from backend.app.routers.portfolio import fetch_current_prices
        tickers_in_alerts = list(set([a["ticker"].upper() for a in pending_alerts]))
        live_prices = (fetch_current_prices(tickers_in_alerts) or {}) if tickers_in_alerts else {}

        min_win_rate = float(config.get("min_win_rate", 55.0))
        min_sentiment = float(config.get("min_sentiment_score", 0.0))

        for alert in pending_alerts:
            alert_id = alert["id"]
            ticker = alert["ticker"].upper()
            signal_type = alert["signal_type"].upper()
            trigger_price = float(alert["trigger_price"])
            win_rate = float(alert["win_rate"])
            sentiment = float(alert["sentiment_score"] or 0.0)
            conviction = float(alert.get("conviction_score") or 0.5)
            cat_type = alert.get("catalyst_type") or ""

            # Restrict to US stocks only if enabled
            if us_only and not is_us_stock(ticker):
                print(f"[PAPER_TRADER] Skipping non-US ticker {ticker} (US stocks only filter enabled).")
                continue

            # Filtering rules
            if win_rate < min_win_rate:
                continue
            if sentiment < min_sentiment:
                continue

            current_qty = active_qtys.get(ticker, 0.0)
            avg_entry = active_costs.get(ticker, 0.0)

            # Resolve live execution price to prevent instant stop-out from stale alert trigger_price
            live_p = live_prices.get(ticker, 0.0)
            exec_price = live_p if live_p > 0 else trigger_price

            if signal_type == "BUY":
                # Prevent position stacking: skip if we already hold an open position in this ticker
                if current_qty > 0.0001:
                    continue

                # Check cash availability
                if current_cash < 1.0:
                    print(f"[PAPER_TRADER] Insufficient paper cash balance (${current_cash:.2f}) to buy {ticker}.")
                    continue

                # Dynamic Conviction Sizing (Kelly Criterion Scale):
                # Standard: $500 | High Conviction: $1,250 | Home-Run Catalyst: $2,500
                base_alloc = float(config.get("position_size_value", 500.0))
                if conviction >= 0.80 or cat_type in ["PRE_EARNINGS_BEAT", "SUPPLY_CHAIN_SPILLOVER"]:
                    multiplier = 5.0  # 5x Allocation for Asymmetric Home Run Catalysts ($2,500)
                elif conviction >= 0.65:
                    multiplier = 2.5  # 2.5x Allocation for High Conviction Signals ($1,250)
                else:
                    multiplier = 1.0  # 1.0x Base Allocation ($500)

                target_alloc = base_alloc * multiplier
                trade_alloc = min(target_alloc, current_cash)
                qty = round(trade_alloc / exec_price, 6) if exec_price > 0 else 0.1

                if qty <= 0.000001:
                    continue

                actual_cost = qty * exec_price

                # Execute BUY
                cur.execute(f"""
                    INSERT INTO {schema}.mimir_paper_portfolio 
                    (ticker, order_date, buy_price, quantity, transaction_type)
                    VALUES (%s, %s, %s, %s, 'BUY')
                """, (ticker, now_local, exec_price, qty))

                cur.execute(f"""
                    INSERT INTO {schema}.mimir_paper_trade_log
                    (signal_id, ticker, action, entry_price, quantity, entry_time, exit_reason, notes)
                    VALUES (%s, %s, 'BUY', %s, %s, %s, 'ALERT_EXECUTION', %s)
                """, (alert_id, ticker, exec_price, qty, now_local, alert.get("reason")))

                # Update local trackers
                active_qtys[ticker] = qty
                active_costs[ticker] = exec_price
                current_cash -= actual_cost

            elif signal_type == "SELL":
                # Only execute SELL if we currently hold an open position for this ticker
                if current_qty <= 0.0001:
                    continue

                close_qty = current_qty  # Close existing open position
                realized_pnl = close_qty * (exec_price - avg_entry)
                realized_pnl_pct = ((exec_price - avg_entry) / avg_entry * 100.0) if avg_entry > 0 else 0.0

                # Execute SELL
                cur.execute(f"""
                    INSERT INTO {schema}.mimir_paper_portfolio 
                    (ticker, order_date, buy_price, quantity, transaction_type)
                    VALUES (%s, %s, %s, %s, 'SELL')
                """, (ticker, now_local, exec_price, close_qty))

                cur.execute(f"""
                    INSERT INTO {schema}.mimir_paper_trade_log
                    (signal_id, ticker, action, entry_price, exit_price, quantity, entry_time, exit_time, exit_reason, realized_pnl, realized_pnl_pct, notes)
                    VALUES (%s, %s, 'SELL', %s, %s, %s, %s, %s, 'SIGNAL_EXIT', %s, %s, %s)
                """, (alert_id, ticker, avg_entry, exec_price, close_qty, now_local, now_local, realized_pnl, realized_pnl_pct, alert.get("reason")))

                # Update local trackers
                active_qtys[ticker] = 0.0
                active_costs[ticker] = 0.0
                current_cash += (close_qty * exec_price)

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
                "trigger_price": exec_price,
                "quantity": qty if signal_type == 'BUY' else close_qty
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
    Evaluates open paper positions in mimir_paper_portfolio against current real-time prices to enforce
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
        cur.execute(f"""
            SELECT id, ticker, order_date, buy_price, quantity, transaction_type
            FROM {schema}.mimir_paper_portfolio
            ORDER BY order_date ASC
        """)
        paper_txs = cur.fetchall()

        if not paper_txs:
            return {"closed_count": 0, "message": "No active paper trade transactions found."}

        holdings = {}
        for tx in paper_txs:
            t = tx["ticker"].upper()
            if t not in holdings:
                holdings[t] = []
            holdings[t].append(tx)

        from backend.app.routers.portfolio import fetch_current_prices
        tickers = list(holdings.keys())
        current_prices = fetch_current_prices(tickers) or {}

        gmt_plus_7 = timezone(timedelta(hours=7))
        now_local = datetime.now(gmt_plus_7)

        for ticker, txs in holdings.items():
            curr_price = current_prices.get(ticker, 0.0)
            if curr_price <= 0:
                continue

            net_qty = 0.0
            cost_basis = 0.0
            first_entry_date = None

            for tx in txs:
                t_type = tx["transaction_type"].upper()
                q = float(tx["quantity"])
                p = float(tx["buy_price"])
                dt = tx["order_date"]
                if net_qty <= 0.0001:
                    first_entry_date = dt

                if t_type == "BUY":
                    if net_qty + q > 0:
                        cost_basis = (net_qty * cost_basis + q * p) / (net_qty + q)
                    net_qty += q
                elif t_type == "SELL":
                    net_qty -= q
                    if net_qty <= 0.0001:
                        net_qty = 0.0
                        cost_basis = 0.0
                        first_entry_date = None

            if net_qty <= 0.0001:
                continue

            avg_price = cost_basis
            if avg_price <= 0:
                continue

            pnl_pct = ((curr_price - avg_price) / avg_price) * 100.0

            exit_reason = None
            if tp_pct > 0 and pnl_pct >= tp_pct:
                exit_reason = "TAKE_PROFIT"
            elif sl_pct > 0 and pnl_pct <= -sl_pct:
                exit_reason = "STOP_LOSS"
            elif auto_exit_hold and first_entry_date:
                cur.execute(f"SELECT optimal_hold_days FROM {schema}.mimir_ticker_parameters WHERE ticker = %s", (ticker,))
                row = cur.fetchone()
                hold_days = int(row["optimal_hold_days"]) if row and row["optimal_hold_days"] else 10
                if first_entry_date + timedelta(days=hold_days) <= now_local:
                    exit_reason = "HOLD_EXPIRATION"

            if exit_reason:
                realized_pnl = net_qty * (curr_price - avg_price)
                realized_pnl_pct = pnl_pct

                cur.execute(f"""
                    INSERT INTO {schema}.mimir_paper_portfolio
                    (ticker, order_date, buy_price, quantity, transaction_type)
                    VALUES (%s, %s, %s, %s, 'SELL')
                """, (ticker, now_local, curr_price, net_qty))

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
    and recent trade log history from mimir_paper_portfolio and mimir_paper_trade_log.
    """
    config = get_paper_config()
    initial_capital = float(config.get("initial_capital", 200.0))

    conn = get_db_connection_dict()
    cur = conn.cursor()
    try:
        schema = settings.mimir_schema

        cur.execute(f"""
            SELECT id, ticker, order_date, buy_price, quantity, transaction_type, created_at
            FROM {schema}.mimir_paper_portfolio
            ORDER BY order_date ASC
        """)
        txs = cur.fetchall()

        raw_holdings = {}
        for tx in txs:
            t = tx["ticker"].upper()
            if t not in raw_holdings:
                raw_holdings[t] = []
            raw_holdings[t].append(tx)

        from backend.app.routers.portfolio import fetch_current_prices
        tickers = list(raw_holdings.keys())
        current_prices = fetch_current_prices(tickers) or {}

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
                
                qty_sum = round(qty_sum, 8)
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
                    "quantity": round(qty_sum, 6),
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

        cur.execute(f"""
            SELECT id, signal_id, ticker, action, entry_price, exit_price, quantity,
                   entry_time, exit_time, exit_reason, realized_pnl, realized_pnl_pct, notes
            FROM {schema}.mimir_paper_trade_log
            ORDER BY id DESC
            LIMIT 50
        """)
        logs = [dict(r) for r in cur.fetchall()]

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
    """Manually closes an active paper position for a given ticker in mimir_paper_portfolio."""
    conn = get_db_connection_dict()
    cur = conn.cursor()
    try:
        schema = settings.mimir_schema
        ticker_clean = ticker.upper().strip()

        cur.execute(f"""
            SELECT transaction_type, quantity, buy_price, order_date
            FROM {schema}.mimir_paper_portfolio
            WHERE ticker = %s
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

        from backend.app.routers.portfolio import fetch_current_prices
        prices = fetch_current_prices([ticker_clean]) or {}
        curr_price = prices.get(ticker_clean, avg_cost)

        gmt_plus_7 = timezone(timedelta(hours=7))
        now_local = datetime.now(gmt_plus_7)

        realized_pnl = net_qty * (curr_price - avg_cost)
        realized_pnl_pct = ((curr_price - avg_cost) / avg_cost * 100.0) if avg_cost > 0 else 0.0

        cur.execute(f"""
            INSERT INTO {schema}.mimir_paper_portfolio
            (ticker, order_date, buy_price, quantity, transaction_type)
            VALUES (%s, %s, %s, %s, 'SELL')
        """, (ticker_clean, now_local, curr_price, net_qty))

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
    """Resets paper portfolio transactions and trade logs back to default capital ($200.00)."""
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        schema = settings.mimir_schema
        cur.execute(f"TRUNCATE TABLE {schema}.mimir_paper_portfolio RESTART IDENTITY")
        cur.execute(f"TRUNCATE TABLE {schema}.mimir_paper_trade_log RESTART IDENTITY")
        
        # Reset config to 200.0 initial capital, 20.0 position value, and US stocks only
        cur.execute(f"""
            UPDATE {schema}.mimir_paper_trading_config
            SET initial_capital = 200.0,
                position_size_value = 20.0,
                us_stocks_only = TRUE,
                updated_at = NOW()
        """)
        conn.commit()
        return {"success": True, "message": "Paper trading account reset back to $200.00 baseline capital."}
    except Exception as e:
        conn.rollback()
        return {"success": False, "error": str(e)}
    finally:
        cur.close()
        conn.close()


def edit_paper_position(ticker: str, new_quantity: float, new_buy_price: float) -> Dict[str, Any]:
    """
    Edits an active open paper trading position's quantity and average entry price.
    """
    if not ticker:
        return {"success": False, "message": "Ticker is required."}
    if new_quantity <= 0:
        return {"success": False, "message": "Quantity must be greater than zero."}
    if new_buy_price <= 0:
        return {"success": False, "message": "Buy price must be greater than zero."}

    ticker_clean = ticker.strip().upper()
    conn = get_db_connection_dict()
    cur = conn.cursor()

    try:
        schema = settings.mimir_schema
        # Check active holding
        cur.execute(f"""
            SELECT id, buy_price, quantity, transaction_type
            FROM {schema}.mimir_paper_portfolio
            WHERE UPPER(ticker) = %s
            ORDER BY order_date ASC
        """, (ticker_clean,))
        txs = cur.fetchall()

        net_qty = 0.0
        for tx in txs:
            ttype = tx["transaction_type"].upper()
            q = float(tx["quantity"])
            if ttype == "BUY":
                net_qty += q
            elif ttype == "SELL":
                net_qty -= q

        if net_qty <= 0.0001:
            return {"success": False, "message": f"No active paper position found for {ticker_clean}."}

        # Clear active position records and insert updated consolidated position
        cur.execute(f"DELETE FROM {schema}.mimir_paper_portfolio WHERE UPPER(ticker) = %s", (ticker_clean,))

        gmt_plus_7 = timezone(timedelta(hours=7))
        now_local = datetime.now(gmt_plus_7)

        cur.execute(f"""
            INSERT INTO {schema}.mimir_paper_portfolio
            (ticker, order_date, buy_price, quantity, transaction_type)
            VALUES (%s, %s, %s, %s, 'BUY')
        """, (ticker_clean, now_local, new_buy_price, new_quantity))

        cur.execute(f"""
            INSERT INTO {schema}.mimir_paper_trade_log
            (ticker, action, entry_price, quantity, entry_time, exit_reason, notes)
            VALUES (%s, 'EDIT', %s, %s, %s, 'MANUAL_EDIT', %s)
        """, (ticker_clean, new_buy_price, new_quantity, now_local, f"Position updated to {new_quantity} shares @ ${new_buy_price:.2f}"))

        conn.commit()
        return {
            "success": True,
            "message": f"Successfully updated paper position for {ticker_clean}: {new_quantity} shares @ ${new_buy_price:.2f}.",
            "ticker": ticker_clean,
            "quantity": new_quantity,
            "buy_price": new_buy_price
        }
    except Exception as e:
        conn.rollback()
        return {"success": False, "message": f"Error editing paper position: {str(e)}"}
    finally:
        cur.close()
        conn.close()


def edit_paper_signal(signal_id: int, trigger_price: Optional[float] = None, signal_type: Optional[str] = None) -> Dict[str, Any]:
    """
    Edits a pending trade signal's trigger price and/or signal type before paper execution.
    """
    if not signal_id:
        return {"success": False, "message": "Signal ID is required."}

    conn = get_db_connection_dict()
    cur = conn.cursor()

    try:
        schema = settings.mimir_schema
        cur.execute(f"""
            SELECT id, ticker, signal_type, trigger_price, status
            FROM {schema}.mimir_trade_signals
            WHERE id = %s
        """, (signal_id,))
        row = cur.fetchone()

        if not row:
            return {"success": False, "message": f"Signal #{signal_id} not found."}
        if row["status"] != "PENDING":
            return {"success": False, "message": f"Signal #{signal_id} cannot be edited because status is '{row['status']}'."}

        updates = []
        params = []

        if trigger_price is not None:
            if trigger_price <= 0:
                return {"success": False, "message": "Trigger price must be greater than zero."}
            updates.append("trigger_price = %s")
            params.append(trigger_price)

        if signal_type is not None:
            st = signal_type.strip().upper()
            if st not in ("BUY", "SELL"):
                return {"success": False, "message": "Signal type must be 'BUY' or 'SELL'."}
            updates.append("signal_type = %s")
            params.append(st)

        if not updates:
            return {"success": False, "message": "No update fields provided."}

        params.append(signal_id)
        update_sql = f"UPDATE {schema}.mimir_trade_signals SET {', '.join(updates)} WHERE id = %s"
        cur.execute(update_sql, tuple(params))
        conn.commit()

        return {
            "success": True,
            "message": f"Signal #{signal_id} updated successfully.",
            "signal_id": signal_id
        }
    except Exception as e:
        conn.rollback()
        return {"success": False, "message": f"Error editing trade signal: {str(e)}"}
    finally:
        cur.close()
        conn.close()


def edit_paper_order_history(log_id: int, ticker: str, action: str, entry_price: float, exit_price: Optional[float] = None, quantity: float = 1.0, exit_reason: Optional[str] = None, notes: Optional[str] = None) -> Dict[str, Any]:
    """
    Edits a paper trade order history entry in mimir_paper_trade_log.
    """
    if not log_id:
        return {"success": False, "message": "Order Log ID is required."}
    if not ticker:
        return {"success": False, "message": "Ticker is required."}
    if quantity <= 0:
        return {"success": False, "message": "Quantity must be greater than zero."}
    if entry_price <= 0:
        return {"success": False, "message": "Entry price must be greater than zero."}

    ticker_clean = ticker.strip().upper()
    action_clean = action.strip().upper()

    realized_pnl = None
    realized_pnl_pct = None
    if exit_price is not None and exit_price > 0:
        realized_pnl = quantity * (exit_price - entry_price) if action_clean in ("BUY", "LONG") else quantity * (entry_price - exit_price)
        realized_pnl_pct = ((exit_price - entry_price) / entry_price * 100.0) if entry_price > 0 else 0.0

    conn = get_db_connection_dict()
    cur = conn.cursor()
    try:
        schema = settings.mimir_schema
        cur.execute(f"""
            SELECT id FROM {schema}.mimir_paper_trade_log WHERE id = %s
        """, (log_id,))
        if not cur.fetchone():
            return {"success": False, "message": f"Paper order record #{log_id} not found."}

        cur.execute(f"""
            UPDATE {schema}.mimir_paper_trade_log
            SET ticker = %s,
                action = %s,
                entry_price = %s,
                exit_price = %s,
                quantity = %s,
                exit_reason = %s,
                realized_pnl = %s,
                realized_pnl_pct = %s,
                notes = %s
            WHERE id = %s
        """, (ticker_clean, action_clean, entry_price, exit_price, quantity, exit_reason, realized_pnl, realized_pnl_pct, notes, log_id))
        conn.commit()
        return {"success": True, "message": f"Updated paper trade order #{log_id} successfully."}
    except Exception as e:
        conn.rollback()
        return {"success": False, "message": f"Error updating paper order: {str(e)}"}
    finally:
        cur.close()
        conn.close()


def delete_paper_order_history(log_id: int) -> Dict[str, Any]:
    """
    Deletes a paper trade order history entry from mimir_paper_trade_log.
    """
    if not log_id:
        return {"success": False, "message": "Order Log ID is required."}

    conn = get_db_connection_dict()
    cur = conn.cursor()
    try:
        schema = settings.mimir_schema
        cur.execute(f"""
            DELETE FROM {schema}.mimir_paper_trade_log WHERE id = %s
        """, (log_id,))
        conn.commit()
        return {"success": True, "message": f"Deleted paper trade order #{log_id} successfully."}
    except Exception as e:
        conn.rollback()
        return {"success": False, "message": f"Error deleting paper order: {str(e)}"}
    finally:
        cur.close()
        conn.close()


