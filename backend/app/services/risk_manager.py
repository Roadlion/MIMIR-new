# backend/app/services/risk_manager.py
"""
MIMIR Central Autonomous Risk Sentinel
======================================
Provides institutional-grade risk gates before any order execution:
1. Daily Portfolio Drawdown Circuit Breaker (-1.5% AUM daily loss kill-switch).
2. Daily Trade Frequency & Ticker Debounce Limiter (Max 5 trades/day, 1 trade/ticker/24h).
3. Broker Microstructure Spread Filter (Rejects trades if ask-bid spread > 0.05% of price).
4. Discord Emergency Telemetry & Database Audit Logging.
"""

import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Tuple, Optional
from zoneinfo import ZoneInfo

from ..database import get_db_connection
from ..config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# Default Risk Governance Parameters
MAX_DAILY_DRAWDOWN_PCT = 1.5        # 1.5% max account equity loss per day (-$1,400 on $93.7k)
MAX_TRADES_PER_DAY = 5              # Max 5 new executions fund-wide per calendar day
MAX_TRADES_PER_TICKER_24H = 1       # Strictly 1 entry per symbol per 24-hour window
MAX_SPREAD_RATIO = 0.0005           # Max 0.05% (5.0 basis points) ask-bid spread


def get_daily_portfolio_pnl(conn=None) -> Dict[str, Any]:
    """
    Computes today's net realized + floating PnL from MT5 or the database.
    Calendar day boundary is evaluated in America/New_York (regular US trading day).
    """
    now_ny = datetime.now(ZoneInfo('America/New_York'))
    today_start_ny = now_ny.replace(hour=0, minute=0, second=0, microsecond=0)
    today_start_utc = today_start_ny.astimezone(timezone.utc)

    # 1. Query MetaTrader 5 if connected
    realized_today = 0.0
    floating_pnl = 0.0
    account_equity = 93763.21  # default baseline
    mt5_active = False

    try:
        import MetaTrader5 as mt5
        if mt5.initialize(timeout=3000):
            acc = mt5.account_info()
            if acc:
                account_equity = float(acc.equity)
                floating_pnl = float(acc.profit)
                mt5_active = True

            # Query deals since today's start
            deals = mt5.history_deals_get(today_start_utc, datetime.now(timezone.utc) + timedelta(days=1))
            if deals:
                for d in deals:
                    # entry == 1 is OUT / exit deal
                    if d.entry == 1:
                        realized_today += (d.profit + d.swap + d.commission)
    except Exception as e:
        logger.debug(f"[RISK_SENTINEL] MT5 live PnL lookup note: {e}")

    # 2. Fallback to PostgreSQL paper trade log if MT5 deals not available
    if not mt5_active:
        close_conn = False
        if conn is None:
            conn = get_db_connection()
            close_conn = True
        try:
            cur = conn.cursor()
            cur.execute(f"""
                SELECT COALESCE(SUM(realized_pnl), 0)
                FROM {settings.mimir_schema}.mimir_paper_trade_log
                WHERE exit_time >= %s
            """, (today_start_utc,))
            row = cur.fetchone()
            realized_today = float(row[0]) if row else 0.0
            cur.close()
        except Exception as db_err:
            logger.warning(f"[RISK_SENTINEL] DB PnL lookup error: {db_err}")
        finally:
            if close_conn:
                conn.close()

    total_daily_pnl = realized_today + floating_pnl
    daily_loss_pct = 0.0
    if account_equity > 0:
        daily_loss_pct = (total_daily_pnl / account_equity) * 100.0

    return {
        "account_equity": account_equity,
        "realized_today": realized_today,
        "floating_pnl": floating_pnl,
        "total_daily_pnl": total_daily_pnl,
        "daily_pnl_pct": daily_loss_pct,
        "is_loss": total_daily_pnl < 0
    }


def is_circuit_breaker_active(conn=None) -> Tuple[bool, str]:
    """
    Checks if an emergency circuit breaker is currently active.
    If today's drawdown exceeds MAX_DAILY_DRAWDOWN_PCT, automatically trips it.
    """
    close_conn = False
    if conn is None:
        conn = get_db_connection()
        close_conn = True

    try:
        cur = conn.cursor()
        now_ny = datetime.now(ZoneInfo('America/New_York'))
        today_start_utc = now_ny.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)

        # 1. Check if already tripped today in database
        cur.execute(f"""
            SELECT id, reason, daily_loss_usd, daily_loss_pct, tripped_at
            FROM {settings.mimir_schema}.mimir_circuit_breaker
            WHERE is_active = TRUE AND tripped_at >= %s
            ORDER BY tripped_at DESC LIMIT 1
        """, (today_start_utc,))
        row = cur.fetchone()
        if row:
            msg = f"Circuit breaker ACTIVE (tripped at {row[4].strftime('%H:%M:%S UTC')}: Loss ${float(row[2]):,.2f} / {float(row[3]):.2f}%). Trading halted."
            return True, msg

        # 2. Check live today's PnL against limit
        pnl_data = get_daily_portfolio_pnl(conn=conn)
        daily_pnl_pct = pnl_data["daily_pnl_pct"]
        total_daily_pnl = pnl_data["total_daily_pnl"]
        equity = pnl_data["account_equity"]

        if daily_pnl_pct <= -MAX_DAILY_DRAWDOWN_PCT:
            reason = f"Daily portfolio loss exceeded -{MAX_DAILY_DRAWDOWN_PCT:.1f}% limit (Actual: ${total_daily_pnl:+,.2f} / {daily_pnl_pct:.2f}% on ${equity:,.2f} equity)"
            logger.critical(f"[RISK_SENTINEL EMERGENCY] {reason}. TRIPPING CIRCUIT BREAKER!")

            # Record to DB
            cur.execute(f"""
                INSERT INTO {settings.mimir_schema}.mimir_circuit_breaker
                    (reason, daily_loss_usd, daily_loss_pct, account_equity, is_active)
                VALUES (%s, %s, %s, %s, TRUE)
            """, (reason, abs(total_daily_pnl), abs(daily_pnl_pct), equity))
            conn.commit()

            # Dispatch emergency Discord alert if configured
            try:
                from .discord_notifier import send_macro_alert
                send_macro_alert(
                    title="🚨 MIMIR RISK SENTINEL: EMERGENCY CIRCUIT BREAKER TRIPPED",
                    description=f"{reason}\n\n**All new orders are immediately blocked for the remainder of the trading session.**",
                    color=0xFF0000
                )
            except Exception:
                pass

            return True, reason

        return False, "Circuit breaker normal (within risk limits)."

    except Exception as e:
        logger.error(f"[RISK_SENTINEL] Circuit breaker check error: {e}")
        return False, f"Check error: {e}"
    finally:
        cur.close()
        if close_conn:
            conn.close()


def check_trade_pacing_and_rate_limits(ticker: str, conn=None) -> Tuple[bool, str]:
    """
    Enforces trade frequency pacing:
    - Max 5 total new orders fund-wide per calendar day.
    - Max 1 entry per ticker per 24 hours (anti-whipsaw debounce).
    """
    close_conn = False
    if conn is None:
        conn = get_db_connection()
        close_conn = True

    try:
        cur = conn.cursor()
        now_ny = datetime.now(ZoneInfo('America/New_York'))
        today_start_utc = now_ny.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        one_day_ago_utc = datetime.now(timezone.utc) - timedelta(hours=24)

        # 1. Total fund entries today
        cur.execute(f"""
            SELECT COUNT(*) 
            FROM {settings.mimir_schema}.mimir_paper_trade_log
            WHERE entry_time >= %s AND action = 'BUY'
        """, (today_start_utc,))
        total_today = cur.fetchone()[0]

        if total_today >= MAX_TRADES_PER_DAY:
            return False, f"Fund daily trade limit reached ({total_today}/{MAX_TRADES_PER_DAY} entries today). Pacing active."

        # 2. Ticker 24-hour debounce check
        cur.execute(f"""
            SELECT COUNT(*) 
            FROM {settings.mimir_schema}.mimir_paper_trade_log
            WHERE ticker = %s AND entry_time >= %s AND action = 'BUY'
        """, (ticker.strip().upper(), one_day_ago_utc))
        ticker_recent = cur.fetchone()[0]

        if ticker_recent >= MAX_TRADES_PER_TICKER_24H:
            return False, f"Ticker {ticker} already entered within past 24 hours ({ticker_recent}/{MAX_TRADES_PER_TICKER_24H}). Debounce active."

        return True, "Trade pacing clear."

    except Exception as e:
        logger.error(f"[RISK_SENTINEL] Pacing check error: {e}")
        return True, f"Pacing check warning: {e}"
    finally:
        cur.close()
        if close_conn:
            conn.close()


def check_microstructure_spread(ticker: str, ask_price: float, bid_price: float, max_spread_pct: float = 0.05) -> Tuple[bool, float]:
    """
    Verifies that the current broker ask-bid spread is <= max_spread_pct (default 0.05% / 5 bps) of midpoint price.
    Prevents execution into illiquid spreads or opening-bell gaps.
    Returns:
        (is_allowed: bool, spread_pct: float)
    """
    if ask_price <= 0 or bid_price <= 0:
        return False, 999.0

    spread = abs(ask_price - bid_price)
    mid_price = (ask_price + bid_price) / 2.0
    spread_pct = (spread / mid_price) * 100.0 if mid_price > 0 else 999.0

    if spread_pct > max_spread_pct:
        return False, spread_pct

    return True, spread_pct
