# backend/app/analytics/paper_trader.py
import sys
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Any, Union
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.database import get_db_connection, get_db_connection_dict
from backend.app.config import get_settings
from backend.app.analytics.mt5_bridge import (
    ensure_mt5_connected,
    get_terminal_and_account_status,
    resolve_mt5_symbol,
    send_market_order,
    close_position,
    close_all_positions,
    modify_position_sltp,
    get_open_positions,
    get_closed_deals,
    MIMIR_MAGIC
)

settings = get_settings()

def is_us_stock(ticker: str) -> bool:
    """
    Returns True if ticker represents a major US stock listed on NYSE, NASDAQ, or AMEX.
    Filters out OTC stocks (5-letter tickers ending in F or Y), crypto (-USD), forex (=X), commodities (=F),
    and foreign exchange tickers with dots.
    """
    if not ticker:
        return False
    t = ticker.strip().upper()
    
    # Exclude Crypto (-USD), Forex (=X), Commodities (=F)
    if "-USD" in t or "=X" in t or "=F" in t:
        return False

    # Exclude foreign exchange extensions with dots
    if "." in t and t != "BRK.B":
        return False
        
    # Standard US equities consist of 1 to 5 alphabetical characters
    if t.replace(".", "").isalpha() and 1 <= len(t) <= 6:
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
            print(f"[PAPER_TRADER] Execution paused: Outside regular market hours ({current_time} ET).")
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


def get_ticker_atr_bounds(ticker: str, current_price: float, cur=None) -> tuple[float, float]:
    """
    Computes ATR-based dynamic Stop Loss % and Take Profit % based on 14-day price history.
    SL is set to 1.5x ATR (clamped between 3.5% and 7.5%).
    TP is set to 2.5x ATR (clamped between 6.0% and 15.0%, ensuring >= 1.67 Risk/Reward).
    """
    if not ticker or current_price <= 0:
        return 4.5, 8.0

    close_cur = False
    conn = None
    if cur is None:
        conn = get_db_connection()
        cur = conn.cursor()
        close_cur = True

    try:
        cur.execute(f"""
            SELECT high, low, close
            FROM {settings.mimir_schema}.v_mimir_daily_ohlcv
            WHERE ticker = %s
            ORDER BY date DESC
            LIMIT 15
        """, (ticker.strip().upper(),))
        rows = cur.fetchall()
        if len(rows) >= 5:
            trs = []
            for i in range(len(rows) - 1):
                h = float(rows[i][0])
                l = float(rows[i][1])
                prev_c = float(rows[i+1][2])
                tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
                trs.append(tr)
            if trs:
                atr = sum(trs) / len(trs)
                sl_pct = (1.5 * atr / current_price) * 100.0
                tp_pct = (2.5 * atr / current_price) * 100.0
                sl_pct = max(3.5, min(7.5, sl_pct))
                tp_pct = max(6.0, min(15.0, tp_pct))
                return round(sl_pct, 2), round(tp_pct, 2)
    except Exception as e:
        print(f"[PAPER_TRADER] ATR calculation error for {ticker}: {e}")
    finally:
        if close_cur:
            cur.close()
            conn.close()

    # Safe default if no historical OHLCV available
    return 4.5, 8.0



_db_initialized = False

def init_paper_trading_db(force: bool = False):
    """Initializes paper trading configuration, paper portfolio, and log tables in PostgreSQL."""
    global _db_initialized
    if _db_initialized and not force:
        return

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
                position_size_value FLOAT DEFAULT 500.0,
                initial_capital FLOAT DEFAULT 100000.0,
                stop_loss_pct FLOAT DEFAULT 4.5,
                take_profit_pct FLOAT DEFAULT 8.0,
                auto_exit_on_hold_days BOOLEAN DEFAULT TRUE,
                us_stocks_only BOOLEAN DEFAULT TRUE,
                mt5_magic INTEGER DEFAULT 202409,
                mt5_enabled BOOLEAN DEFAULT TRUE,
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Add columns if missing in existing table
        cur.execute(f"""
            ALTER TABLE {schema}.mimir_paper_trading_config
            ADD COLUMN IF NOT EXISTS us_stocks_only BOOLEAN DEFAULT TRUE,
            ADD COLUMN IF NOT EXISTS mt5_magic INTEGER DEFAULT 202409,
            ADD COLUMN IF NOT EXISTS mt5_enabled BOOLEAN DEFAULT TRUE,
            ADD COLUMN IF NOT EXISTS ignore_market_hours BOOLEAN DEFAULT FALSE,
            ADD COLUMN IF NOT EXISTS max_open_positions INTEGER DEFAULT 5,
            ADD COLUMN IF NOT EXISTS min_conviction_score FLOAT DEFAULT 0.65;
        """)

        # Upgrade default 3.0/6.0 parameters to ATR-optimized 4.5/8.0
        cur.execute(f"""
            UPDATE {schema}.mimir_paper_trading_config
            SET stop_loss_pct = 4.5, take_profit_pct = 8.0
            WHERE stop_loss_pct = 3.0 AND take_profit_pct = 6.0;
        """)

        # Seed default row if empty
        cur.execute(f"SELECT COUNT(*) FROM {schema}.mimir_paper_trading_config")
        if cur.fetchone()[0] == 0:
            cur.execute(f"""
                INSERT INTO {schema}.mimir_paper_trading_config 
                (is_enabled, execution_mode, min_win_rate, min_sentiment_score, position_size_type, position_size_value, initial_capital, stop_loss_pct, take_profit_pct, auto_exit_on_hold_days, us_stocks_only, mt5_magic, mt5_enabled, ignore_market_hours, max_open_positions, min_conviction_score)
                VALUES (TRUE, 'AUTO', 55.0, 0.0, 'FIXED_USD', 500.0, 100000.0, 4.5, 8.0, TRUE, TRUE, 202409, TRUE, FALSE, 5, 0.65)
            """)

        # 2. Paper Trade Log Table (with MT5 references)
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
                notes TEXT,
                mt5_ticket BIGINT,
                mt5_deal_id BIGINT
            )
        """)

        cur.execute(f"""
            ALTER TABLE {schema}.mimir_paper_trade_log
            ADD COLUMN IF NOT EXISTS mt5_ticket BIGINT,
            ADD COLUMN IF NOT EXISTS mt5_deal_id BIGINT;
        """)

        # 3. Dedicated Paper Portfolio Table
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {schema}.mimir_paper_portfolio (
                id SERIAL PRIMARY KEY,
                ticker VARCHAR(50) NOT NULL,
                order_date TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                buy_price FLOAT NOT NULL,
                quantity FLOAT NOT NULL,
                transaction_type VARCHAR(10) DEFAULT 'BUY',
                mt5_ticket BIGINT,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            );
        """)

        cur.execute(f"""
            ALTER TABLE {schema}.mimir_paper_portfolio
            ADD COLUMN IF NOT EXISTS mt5_ticket BIGINT;
        """)

        conn.commit()
        _db_initialized = True
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
                   stop_loss_pct, take_profit_pct, auto_exit_on_hold_days, us_stocks_only, 
                   mt5_magic, mt5_enabled, ignore_market_hours,
                   COALESCE(max_open_positions, 5) as max_open_positions,
                   COALESCE(min_conviction_score, 0.65) as min_conviction_score,
                   updated_at
            FROM {settings.mimir_schema}.mimir_paper_trading_config
            ORDER BY id ASC LIMIT 1
        """)
        row = cur.fetchone()
        if row:
            res = dict(row)
            if res.get("us_stocks_only") is None:
                res["us_stocks_only"] = True
            if res.get("mt5_magic") is None:
                res["mt5_magic"] = MIMIR_MAGIC
            if res.get("mt5_enabled") is None:
                res["mt5_enabled"] = True
            if res.get("ignore_market_hours") is None:
                res["ignore_market_hours"] = False
            if res.get("max_open_positions") is None:
                res["max_open_positions"] = 5
            if res.get("min_conviction_score") is None:
                res["min_conviction_score"] = 0.65
            return res
        return {
            "is_enabled": True,
            "execution_mode": "AUTO",
            "min_win_rate": 55.0,
            "min_sentiment_score": 0.0,
            "position_size_type": "FIXED_USD",
            "position_size_value": 500.0,
            "initial_capital": 100000.0,
            "stop_loss_pct": 3.0,
            "take_profit_pct": 6.0,
            "auto_exit_on_hold_days": True,
            "us_stocks_only": True,
            "mt5_magic": MIMIR_MAGIC,
            "mt5_enabled": True,
            "ignore_market_hours": False
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
                mt5_magic = COALESCE(%s, mt5_magic),
                mt5_enabled = COALESCE(%s, mt5_enabled),
                ignore_market_hours = COALESCE(%s, ignore_market_hours),
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
            updates.get("us_stocks_only"),
            updates.get("mt5_magic"),
            updates.get("mt5_enabled"),
            updates.get("ignore_market_hours")
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
    Scans pending trade signals in mimir_trade_signals, validates rules,
    and executes actual paper trades directly inside MetaTrader 5 (MT5).
    """
    config = get_paper_config()
    if not config.get("is_enabled"):
        return {"executed_count": 0, "message": "Paper trading is currently disabled in settings."}

    ignore_hours = config.get("ignore_market_hours", False)
    if not is_execution_allowed(ignore_market_hours=ignore_hours):
        return {
            "executed_count": 0,
            "message": "Paper trading execution paused: outside US regular market hours (09:30-16:00 ET) or during blackout window."
        }

    # Verify MT5 Terminal connection and AlgoTrading status
    mt5_status = get_terminal_and_account_status()
    if not mt5_status.get("connected"):
        return {
            "executed_count": 0,
            "message": f"MT5 Execution Error: {mt5_status.get('error', 'MT5 Terminal is not connected.')}"
        }

    if not mt5_status.get("trade_allowed"):
        return {
            "executed_count": 0,
            "message": (
                "MT5 AutoTrading is currently DISABLED in MetaTrader 5. "
                "Please click the 'Algo Trading' button in MT5 (or press Ctrl+E) to enable automated execution."
            )
        }

    us_only = config.get("us_stocks_only", True)
    min_win_rate = float(config.get("min_win_rate", 55.0))
    min_sentiment = float(config.get("min_sentiment_score", 0.0))
    sl_pct = float(config.get("stop_loss_pct", 4.5))
    tp_pct = float(config.get("take_profit_pct", 8.0))
    magic = int(config.get("mt5_magic", MIMIR_MAGIC))
    base_alloc = float(config.get("position_size_value", 500.0))
    max_open_pos = int(config.get("max_open_positions", 5))
    min_conviction = float(config.get("min_conviction_score", 0.65))

    # Get active open positions in MT5 to prevent duplicate positions and enforce concurrency
    open_positions = get_open_positions()
    open_tickers = {p["ticker"].upper() for p in open_positions}

    conn = get_db_connection_dict()
    cur = conn.cursor()
    executed_count = 0
    executed_details = []

    # Upgrade 1: Macro Regime Gate Check (SPY 50-day SMA and VIX volatility filter)
    try:
        from backend.app.services.macro_tracker import is_market_regime_bullish
        market_bullish, macro_reason = is_market_regime_bullish(conn=conn)
    except Exception as e:
        market_bullish, macro_reason = True, str(e)

    try:
        schema = settings.mimir_schema
        # Fetch pending alerts joined with ticker parameters (including target_price & stop_loss)
        cur.execute(f"""
            SELECT s.id, s.ticker, s.signal_type, s.trigger_price, s.rsi_value, s.sentiment_score,
                   s.support_level, s.resistance_level, s.reason, s.created_at,
                   COALESCE(s.conviction_score, 0.5) as conviction_score, s.catalyst_type,
                   s.target_price, s.stop_loss,
                   COALESCE(p.win_rate, 50.0) as win_rate
            FROM {schema}.mimir_trade_signals s
            LEFT JOIN {schema}.mimir_ticker_parameters p ON s.ticker = p.ticker
            WHERE s.status = 'PENDING'
            ORDER BY s.created_at ASC
        """)
        pending_alerts = cur.fetchall()

        gmt_plus_7 = timezone(timedelta(hours=7))
        now_local = datetime.now(gmt_plus_7)

        for alert in pending_alerts:
            alert_id = alert["id"]
            ticker = alert["ticker"].upper()
            signal_type = alert["signal_type"].upper()
            trigger_price = float(alert["trigger_price"])
            win_rate = float(alert["win_rate"])
            sentiment = float(alert["sentiment_score"] or 0.0)
            conviction = float(alert.get("conviction_score") or 0.5)
            cat_type = alert.get("catalyst_type") or ""
            reason_text = str(alert.get("reason") or "")

            # Filter rules
            if us_only and not is_us_stock(ticker):
                continue
            if win_rate < min_win_rate:
                continue
            if sentiment < min_sentiment:
                continue

            # Upgrade 1: Macro Regime Gate (Suppress BUY entries during broad market downtrends/panics)
            if signal_type == "BUY" and not market_bullish:
                print(f"[PAPER_TRADER] Suppressed alert #{alert_id} for {ticker}: {macro_reason}")
                continue

            # Upgrade 2: Portfolio Concurrency Cap (Limit simultaneous holdings to max_open_pos)
            if signal_type == "BUY" and len(open_tickers) >= max_open_pos:
                print(f"[PAPER_TRADER] Portfolio concurrency limit reached ({len(open_tickers)}/{max_open_pos} positions). Deferring entry for {ticker}.")
                continue

            # Daily Entry Pacing: Max 2 new entries admitted per day to prevent morning slot exhaustion
            if signal_type == "BUY":
                cur.execute(f"""
                    SELECT COUNT(*) FROM {schema}.mimir_paper_trade_log
                    WHERE entry_time::date = %s
                """, (now_local.date(),))
                today_count_row = cur.fetchone()
                today_entries = today_count_row[0] if today_count_row else 0
                if today_entries >= 2:
                    print(f"[PAPER_TRADER] Daily entry pacing limit reached ({today_entries}/2 trades today). Deferring entry for {ticker}.")
                    continue

            # Reject unmanaged naked alerts (must have predefined risk bounds)
            if not alert.get("stop_loss") and not alert.get("target_price"):
                print(f"[PAPER_TRADER] Suppressed naked alert #{alert_id} for {ticker}: Missing stop_loss and target_price.")
                continue

            # Upgrade 2: High-Conviction Selection Floor (Filter out low-conviction noise)
            if conviction < min_conviction and cat_type not in ["PRE_EARNINGS_BEAT", "SUPPLY_CHAIN_SPILLOVER"]:
                print(f"[PAPER_TRADER] Suppressed alert #{alert_id} for {ticker}: Conviction {conviction:.2f} < {min_conviction:.2f} threshold.")
                continue

            # Narrative Lifecycle Filter (Block PEAK and FADING narrative entries)
            if "PEAK Narrative" in reason_text or "FADING Narrative" in reason_text:
                print(f"[PAPER_TRADER] Suppressed alert #{alert_id} for {ticker}: Narrative in PEAK/FADING phase.")
                continue

            try:
                from backend.app.analytics.narrative_tracker import is_narrative_enterable
                if not is_narrative_enterable(ticker, conn=conn):
                    print(f"[PAPER_TRADER] Suppressed alert #{alert_id} for {ticker}: Narrative lifecycle stale or fading.")
                    continue
            except Exception:
                pass

            # Prevent position stacking for BUY
            if signal_type == "BUY" and ticker in open_tickers:
                continue

            # If SELL, only execute if we already hold an open position in MT5
            if signal_type == "SELL" and ticker not in open_tickers:
                continue

            # Update 3: Calculate Dynamic ATR-Based Volatility Stop Loss & Take Profit
            target_price = float(alert["target_price"]) if alert.get("target_price") else None
            stop_loss = float(alert["stop_loss"]) if alert.get("stop_loss") else None

            if stop_loss and trigger_price > 0:
                trade_sl_pct = abs(trigger_price - stop_loss) / trigger_price * 100.0
                trade_sl_pct = max(3.5, min(7.5, trade_sl_pct))
            else:
                trade_sl_pct, _ = get_ticker_atr_bounds(ticker, trigger_price, cur=cur)

            if target_price and trigger_price > 0:
                trade_tp_pct = abs(target_price - trigger_price) / trigger_price * 100.0
                trade_tp_pct = max(6.0, min(15.0, trade_tp_pct))
            else:
                _, trade_tp_pct = get_ticker_atr_bounds(ticker, trigger_price, cur=cur)

            # Ensure minimum 1:1.67 Risk/Reward ratio
            trade_tp_pct = max(trade_tp_pct, round(trade_sl_pct * 1.67, 2))

            # Dynamic Conviction Sizing (Kelly Criterion Scale)
            if conviction >= 0.80 or cat_type in ["PRE_EARNINGS_BEAT", "SUPPLY_CHAIN_SPILLOVER"]:
                multiplier = 3.0  # 3x Sizing for asymmetric home runs
            elif conviction >= 0.65:
                multiplier = 1.5  # 1.5x Sizing for high conviction
            else:
                multiplier = 1.0  # 1.0x Base Sizing

            trade_alloc = base_alloc * multiplier

            # Send order directly to MT5 with dynamic ATR volatility boundaries
            order_res = send_market_order(
                ticker=ticker,
                action=signal_type,
                target_usd=trade_alloc,
                sl_pct=trade_sl_pct,
                tp_pct=trade_tp_pct,
                comment=f"MIMIR:{alert_id}",
                magic=magic
            )

            if order_res.get("success"):
                mt5_ticket = order_res.get("ticket") or order_res.get("order_id")
                exec_price = float(order_res.get("price", trigger_price))
                exec_vol = float(order_res.get("volume", 1.0))

                # Insert into local audit log
                cur.execute(f"""
                    INSERT INTO {schema}.mimir_paper_trade_log
                    (signal_id, ticker, action, entry_price, quantity, entry_time, exit_reason, notes, mt5_ticket)
                    VALUES (%s, %s, %s, %s, %s, %s, 'ALERT_EXECUTION', %s, %s)
                """, (alert_id, ticker, signal_type, exec_price, exec_vol, now_local, alert.get("reason"), mt5_ticket))

                # Also record into paper portfolio table
                cur.execute(f"""
                    INSERT INTO {schema}.mimir_paper_portfolio 
                    (ticker, order_date, buy_price, quantity, transaction_type, mt5_ticket)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, (ticker, now_local, exec_price, exec_vol, signal_type, mt5_ticket))

                # Mark trade signal as APPROVED / AUTO_TRADED
                cur.execute(f"""
                    UPDATE {schema}.mimir_trade_signals
                    SET status = 'APPROVED', acted_at = %s
                    WHERE id = %s
                """, (now_local, alert_id))

                executed_count += 1
                open_tickers.add(ticker)
                executed_details.append({
                    "alert_id": alert_id,
                    "ticker": ticker,
                    "signal_type": signal_type,
                    "mt5_ticket": mt5_ticket,
                    "exec_price": exec_price,
                    "volume": exec_vol
                })
            else:
                executed_details.append({
                    "alert_id": alert_id,
                    "ticker": ticker,
                    "error": order_res.get("message")
                })

        conn.commit()
        return {
            "executed_count": executed_count,
            "executed_details": executed_details,
            "message": f"Successfully auto-executed {executed_count} paper trades directly in MT5."
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
    Monitors open MT5 positions for hold days expiration.
    (Stop Loss and Take Profit are managed natively inside MT5 on the broker side).
    """
    config = get_paper_config()
    auto_exit_hold = config.get("auto_exit_on_hold_days", True)
    if not auto_exit_hold:
        return {"closed_count": 0, "message": "Auto-exit on hold days is disabled."}

    # Update 2: Enforce market hours and retail blackout window on auto-exits
    ignore_hours = config.get("ignore_market_hours", False)
    if not is_execution_allowed(ignore_market_hours=ignore_hours):
        return {
            "closed_count": 0,
            "message": "Paper position exits paused: outside US regular market hours (09:30-16:00 ET) or during retail blackout windows (09:30-10:15 & 15:45-16:00 ET)."
        }

    positions = get_open_positions()
    if not positions:
        return {"closed_count": 0, "message": "No open MT5 positions to evaluate."}

    conn = get_db_connection_dict()
    cur = conn.cursor()
    closed_count = 0
    closed_details = []

    try:
        schema = settings.mimir_schema
        gmt_plus_7 = timezone(timedelta(hours=7))
        now_local = datetime.now(gmt_plus_7)

        for pos in positions:
            ticker = pos["ticker"].upper()
            open_time_str = pos.get("open_time")
            if not open_time_str:
                continue

            open_dt = datetime.strptime(open_time_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=gmt_plus_7)

            # Upgrade 3: Dynamic Breakeven Stop Ratchet
            # If position has gained >= +3.0% and SL is below entry, ratchet SL to breakeven (+0.5%)
            entry_price = float(pos.get("avg_entry_price", 0.0))
            curr_price = float(pos.get("current_price", 0.0))
            curr_sl = float(pos.get("sl", 0.0))
            pos_ticket = pos.get("ticket")
            is_buy = (pos.get("action") == "BUY")

            if entry_price > 0 and pos_ticket:
                gain_pct = ((curr_price - entry_price) / entry_price) * 100.0 if is_buy else ((entry_price - curr_price) / entry_price) * 100.0
                if gain_pct >= 3.0:
                    be_sl = round(entry_price * 1.005, 2) if is_buy else round(entry_price * 0.995, 2)
                    should_ratchet = (curr_sl < be_sl) if is_buy else (curr_sl > be_sl or curr_sl == 0.0)
                    if should_ratchet:
                        mod_res = modify_position_sltp(ticket=pos_ticket, sl=be_sl)
                        if mod_res.get("success"):
                            print(f"[PAPER_TRADER] Dynamic Breakeven: Ratcheted SL for {ticker} (#{pos_ticket}) to ${be_sl} (Locked +0.5% profit at {gain_pct:+.1f}% gain).")

            cur.execute(f"SELECT optimal_hold_days FROM {schema}.mimir_ticker_parameters WHERE ticker = %s", (ticker,))
            row = cur.fetchone()
            hold_days = int(row["optimal_hold_days"]) if row and row["optimal_hold_days"] else 10

            if open_dt + timedelta(days=hold_days) <= now_local:
                # Sanity check: verify live quote is reasonable before market close deal
                # Avoid closing into an erratic zero or >25% anomalous opening spread print
                curr_price = float(pos.get("current_price", 0.0))
                entry_price = float(pos.get("avg_entry_price", 0.0))
                if curr_price <= 0:
                    print(f"[PAPER_TRADER] Skipping auto-exit for {ticker}: Invalid quote (${curr_price}).")
                    continue
                if entry_price > 0 and (curr_price / entry_price) < 0.70:
                    print(f"[PAPER_TRADER] Warning: abnormal spread dip detected for {ticker} (entry {entry_price}, current {curr_price}). Deferring close.")
                    continue

                # Hold days expired: close position in MT5
                res = close_position(ticket=pos["ticket"])
                if res.get("success"):
                    closed_count += 1
                    closed_details.append({
                        "ticker": ticker,
                        "ticket": pos["ticket"],
                        "exit_reason": "HOLD_EXPIRATION"
                    })
                    # Log exit in DB
                    cur.execute(f"""
                        INSERT INTO {schema}.mimir_paper_trade_log
                        (ticker, action, entry_price, exit_price, quantity, entry_time, exit_time, exit_reason, realized_pnl, notes, mt5_ticket)
                        VALUES (%s, 'SELL', %s, %s, %s, %s, %s, 'HOLD_EXPIRATION', %s, 'Auto-closed on hold maturity', %s)
                    """, (ticker, pos["avg_entry_price"], pos["current_price"], pos["quantity"], open_dt, now_local, pos["unrealized_pnl"], pos["ticket"]))

        conn.commit()
        return {
            "closed_count": closed_count,
            "closed_details": closed_details,
            "message": f"Processed MT5 positions. Closed {closed_count} positions on hold maturity."
        }
    except Exception as e:
        conn.rollback()
        return {"closed_count": 0, "error": str(e)}
    finally:
        cur.close()
        conn.close()


def get_paper_trading_summary() -> Dict[str, Any]:
    """
    Returns live paper trading performance statistics, active open positions,
    and deal history retrieved DIRECTLY from MetaTrader 5 (MT5).
    """
    config = get_paper_config()
    mt5_status = get_terminal_and_account_status()
    open_positions = get_open_positions()
    closed_deals = get_closed_deals(days=90)

    # Compute open metrics
    total_open_value = sum(p["current_value"] for p in open_positions)
    total_unrealized_pnl = sum(p["unrealized_pnl"] for p in open_positions)

    # Compute closed deals metrics
    total_realized_pnl = sum(d["realized_pnl"] for d in closed_deals)
    total_closed_trades = len(closed_deals)
    win_count = sum(1 for d in closed_deals if d["realized_pnl"] > 0)
    win_rate_pct = (win_count / total_closed_trades * 100.0) if total_closed_trades > 0 else 0.0

    current_equity = mt5_status.get("equity", 0.0)
    cash_balance = mt5_status.get("balance", 0.0)
    margin_free = mt5_status.get("margin_free", 0.0)
    margin_used = mt5_status.get("margin", 0.0)

    initial_capital = float(config.get("initial_capital", 100000.0))
    total_pnl = current_equity - initial_capital
    total_pnl_pct = (total_pnl / initial_capital * 100.0) if initial_capital > 0 else 0.0

    # Format active positions dictionary for backward compatibility
    active_positions_dict = {}
    for p in open_positions:
        active_positions_dict[p["ticker"]] = p

    # Retrieve audit trade logs from database as supplementary log records
    conn = get_db_connection_dict()
    cur = conn.cursor()
    db_logs = []
    try:
        schema = settings.mimir_schema
        cur.execute(f"""
            SELECT id, signal_id, ticker, action, entry_price, exit_price, quantity,
                   entry_time, exit_time, exit_reason, realized_pnl, realized_pnl_pct, notes, mt5_ticket
            FROM {schema}.mimir_paper_trade_log
            ORDER BY id DESC
            LIMIT 50
        """)
        db_logs = [dict(r) for r in cur.fetchall()]
    except Exception as e:
        print(f"[PAPER_TRADER] Warning loading DB logs: {e}")
    finally:
        cur.close()
        conn.close()

    # Prioritize MT5 closed deals for logs, fallback to DB logs if deals empty
    display_logs = closed_deals if closed_deals else db_logs

    return {
        "config": config,
        "mt5_status": mt5_status,
        "initial_capital": initial_capital,
        "current_equity": current_equity,
        "cash_balance": cash_balance,
        "margin_free": margin_free,
        "margin_used": margin_used,
        "total_open_value": total_open_value,
        "total_pnl": total_pnl,
        "total_pnl_pct": total_pnl_pct,
        "total_realized_pnl": total_realized_pnl,
        "total_unrealized_pnl": total_unrealized_pnl,
        "total_trades_logged": len(display_logs),
        "total_closed_trades": total_closed_trades,
        "win_rate_pct": win_rate_pct,
        "active_positions": active_positions_dict,
        "active_positions_list": open_positions,
        "trade_logs": display_logs,
        "is_mt5_live": mt5_status.get("connected", False)
    }


def close_paper_position(ticker_or_ticket: Union[str, int]) -> Dict[str, Any]:
    """
    Manually closes an active paper position directly in MT5 by ticket or ticker.
    """
    ticket_val = None
    ticker_val = None

    if isinstance(ticker_or_ticket, int) or (isinstance(ticker_or_ticket, str) and ticker_or_ticket.isdigit()):
        ticket_val = int(ticker_or_ticket)
    else:
        ticker_val = str(ticker_or_ticket).strip().upper()

    res = close_position(ticket=ticket_val, ticker=ticker_val)
    if not res.get("success"):
        return res

    # Record close in local DB log
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        schema = settings.mimir_schema
        gmt_plus_7 = timezone(timedelta(hours=7))
        now_local = datetime.now(gmt_plus_7)

        cur.execute(f"""
            INSERT INTO {schema}.mimir_paper_trade_log
            (ticker, action, entry_price, exit_price, quantity, entry_time, exit_time, exit_reason, realized_pnl, notes, mt5_ticket)
            VALUES (%s, 'SELL', %s, %s, %s, %s, %s, 'MANUAL_CLOSE', %s, %s, %s)
        """, (
            res.get("symbol", ticker_val or ""),
            0.0,
            res.get("close_price", 0.0),
            res.get("volume", 0.0),
            now_local,
            now_local,
            res.get("profit", 0.0),
            f"Manually closed in MT5 position #{res.get('ticket')}",
            res.get("ticket")
        ))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"[PAPER_TRADER] Warning logging manual close: {e}")

    return res


def reset_paper_account() -> Dict[str, Any]:
    """
    Closes all open paper positions in MT5 and truncates local paper logs.
    """
    # 1. Close all MT5 positions
    close_res = close_all_positions()

    # 2. Reset database tables
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        schema = settings.mimir_schema
        cur.execute(f"TRUNCATE TABLE {schema}.mimir_paper_portfolio RESTART IDENTITY")
        cur.execute(f"TRUNCATE TABLE {schema}.mimir_paper_trade_log RESTART IDENTITY")
        
        cur.execute(f"""
            UPDATE {schema}.mimir_paper_trading_config
            SET initial_capital = 100000.0,
                position_size_value = 500.0,
                us_stocks_only = TRUE,
                updated_at = NOW()
        """)
        conn.commit()
        return {
            "success": True,
            "message": f"Reset complete: Closed {close_res.get('closed_count', 0)} MT5 positions and cleared audit logs."
        }
    except Exception as e:
        conn.rollback()
        return {"success": False, "error": str(e)}
    finally:
        cur.close()
        conn.close()


def edit_paper_position(ticker_or_ticket: Union[str, int], new_quantity: float = None, new_buy_price: float = None, sl: float = None, tp: float = None) -> Dict[str, Any]:
    """
    Modifies Stop Loss (sl) and Take Profit (tp) for an open MT5 position.
    """
    ticket_val = None
    if isinstance(ticker_or_ticket, int) or (isinstance(ticker_or_ticket, str) and ticker_or_ticket.isdigit()):
        ticket_val = int(ticker_or_ticket)
    else:
        # Find ticket from open positions by ticker
        positions = get_open_positions()
        t_clean = str(ticker_or_ticket).strip().upper()
        for p in positions:
            if p["ticker"].upper() == t_clean:
                ticket_val = p["ticket"]
                break

    if not ticket_val:
        return {"success": False, "message": f"Active position for '{ticker_or_ticket}' not found in MT5."}

    # In MT5, modifying an open position means updating SL and TP
    res = modify_position_sltp(ticket=ticket_val, sl=sl, tp=tp)
    return res


def edit_paper_signal(signal_id: int, trigger_price: Optional[float] = None, signal_type: Optional[str] = None) -> Dict[str, Any]:
    """Edits a pending trade signal's trigger price and/or signal type before execution."""
    if not signal_id:
        return {"success": False, "message": "Signal ID is required."}

    conn = get_db_connection_dict()
    cur = conn.cursor()
    try:
        schema = settings.mimir_schema
        cur.execute(f"SELECT id, ticker, signal_type, trigger_price, status FROM {schema}.mimir_trade_signals WHERE id = %s", (signal_id,))
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

        return {"success": True, "message": f"Signal #{signal_id} updated successfully.", "signal_id": signal_id}
    except Exception as e:
        conn.rollback()
        return {"success": False, "message": f"Error editing trade signal: {str(e)}"}
    finally:
        cur.close()
        conn.close()


def edit_paper_order_history(log_id: int, ticker: str, action: str, entry_price: float, exit_price: Optional[float] = None, quantity: float = 1.0, exit_reason: Optional[str] = None, notes: Optional[str] = None) -> Dict[str, Any]:
    """Edits an audit paper trade order history entry in PostgreSQL."""
    if not log_id:
        return {"success": False, "message": "Order Log ID is required."}
    conn = get_db_connection_dict()
    cur = conn.cursor()
    try:
        schema = settings.mimir_schema
        realized_pnl = None
        realized_pnl_pct = None
        if exit_price is not None and exit_price > 0 and entry_price > 0:
            realized_pnl = quantity * (exit_price - entry_price) if action.upper() in ("BUY", "LONG") else quantity * (entry_price - exit_price)
            realized_pnl_pct = ((exit_price - entry_price) / entry_price * 100.0)

        cur.execute(f"""
            UPDATE {schema}.mimir_paper_trade_log
            SET ticker = %s, action = %s, entry_price = %s, exit_price = %s, quantity = %s,
                exit_reason = %s, realized_pnl = %s, realized_pnl_pct = %s, notes = %s
            WHERE id = %s
        """, (ticker.upper(), action.upper(), entry_price, exit_price, quantity, exit_reason, realized_pnl, realized_pnl_pct, notes, log_id))
        conn.commit()
        return {"success": True, "message": f"Updated paper trade order #{log_id} successfully."}
    except Exception as e:
        conn.rollback()
        return {"success": False, "message": f"Error updating paper order: {str(e)}"}
    finally:
        cur.close()
        conn.close()


def delete_paper_order_history(log_id: int) -> Dict[str, Any]:
    """Deletes an audit paper trade order history entry from PostgreSQL."""
    if not log_id:
        return {"success": False, "message": "Order Log ID is required."}
    conn = get_db_connection_dict()
    cur = conn.cursor()
    try:
        schema = settings.mimir_schema
        cur.execute(f"DELETE FROM {schema}.mimir_paper_trade_log WHERE id = %s", (log_id,))
        conn.commit()
        return {"success": True, "message": f"Deleted paper trade order #{log_id} successfully."}
    except Exception as e:
        conn.rollback()
        return {"success": False, "message": f"Error deleting paper order: {str(e)}"}
    finally:
        cur.close()
        conn.close()
