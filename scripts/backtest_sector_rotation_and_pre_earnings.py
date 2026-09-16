"""
MIMIR Backtesting Engine: Sector Rotation Matrix & Pre-Earnings Beat
====================================================================
Empirically tests the performance of:
1. Sector Rotation Inflow vs Outflow: Does entering with a Sector Tailwind (RS >= +0.5%)
   beat entering with a Sector Headwind (RS <= -0.5%)?
2. Pre-Earnings Beat Catalysts: Win rate, average return, and profit factor of pre-earnings setups.
3. Home Run Asymmetric Profile (3x ATR TP, 1.5x ATR SL, 2.5:1 R/R + Breakeven Ratchet)
   vs Legacy Scalp (3% SL / 6% TP).

Eliminates look-ahead bias by evaluating forward daily OHLCV bars strictly after alert creation.
"""

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from collections import defaultdict

from backend.app.database import get_db_connection
from backend.app.config import get_settings
from backend.app.services.sector_rotation_service import SECTOR_ETFS, SUB_CATEGORY_TO_ETF

settings = get_settings()
schema = settings.mimir_schema


def load_market_data(conn):
    """Loads all daily price bars for all tickers, sector ETFs, and SPY."""
    print("Loading daily OHLCV bars for all assets from v_mimir_daily_ohlcv...")
    sql = f"""
        SELECT ticker, date, open, high, low, close, volume
        FROM {schema}.v_mimir_daily_ohlcv
        WHERE date >= '2025-01-01'
        ORDER BY ticker, date ASC
    """
    df = pd.read_sql(sql, conn)
    df['date'] = pd.to_datetime(df['date']).dt.date
    for col in ['open', 'high', 'low', 'close', 'volume']:
        df[col] = pd.to_numeric(df[col], errors='coerce')
    
    # Group by ticker for fast O(1) lookup
    bars_by_ticker = {t: grp.set_index('date').sort_index() for t, grp in df.groupby('ticker')}
    print(f"Loaded price history for {len(bars_by_ticker)} tickers.")
    return bars_by_ticker


def compute_sector_rs_matrix(bars_by_ticker):
    """Computes daily 5-day Relative Strength vs SPY for each sector ETF."""
    print("Pre-computing historical Sector Relative Strength (RS) vs SPY...")
    spy_df = bars_by_ticker.get('SPY')
    if spy_df is None or spy_df.empty:
        raise RuntimeError("SPY daily data missing.")

    rs_lookup = {}  # (sector_etf, date) -> rs_5d_pct
    spy_closes = spy_df['close']

    for etf in SECTOR_ETFS.keys():
        if etf not in bars_by_ticker:
            continue
        etf_closes = bars_by_ticker[etf]['close']
        common_dates = etf_closes.index.intersection(spy_closes.index).sort_values()
        
        ratio = etf_closes.loc[common_dates] / spy_closes.loc[common_dates]
        rs_5d = (ratio / ratio.shift(5) - 1.0) * 100.0
        
        for dt, val in rs_5d.dropna().items():
            rs_lookup[(etf, dt)] = float(val)

    print(f"Calculated {len(rs_lookup)} historical Sector RS observations.")
    return rs_lookup


def simulate_trade(ticker, entry_date, entry_price, target_price, stop_loss, bars_by_ticker, max_holding_days=15):
    """
    Simulates a trade forward in time bar-by-bar:
    - Enforces Dynamic Breakeven Stop: if unrealized gain reaches >= +3%, SL is moved to entry + 0.5%
    - Checks daily High for TP hit
    - Checks daily Low for SL hit
    - Exits at close of day max_holding_days if neither hit
    """
    if ticker not in bars_by_ticker:
        return None

    df = bars_by_ticker[ticker]
    # Forward dates strictly after entry_date
    future_dates = [d for d in df.index if d > entry_date][:max_holding_days]
    if not future_dates:
        return None

    current_sl = stop_loss
    breakeven_activated = False

    for day_idx, d in enumerate(future_dates, 1):
        bar = df.loc[d]
        o, h, l, c = bar['open'], bar['high'], bar['low'], bar['close']

        # Check for Breakeven Ratchet (+3% gain)
        max_runup = (h - entry_price) / entry_price * 100.0
        if max_runup >= 3.0 and not breakeven_activated:
            current_sl = max(current_sl, entry_price * 1.005)
            breakeven_activated = True

        # Check Stop Loss hit
        if l <= current_sl:
            exit_price = min(o, current_sl)  # Account for gap downs below SL
            pnl_pct = (exit_price - entry_price) / entry_price * 100.0
            return {
                "exit_date": d,
                "exit_price": round(exit_price, 2),
                "pnl_pct": round(pnl_pct, 2),
                "holding_days": day_idx,
                "exit_reason": "BREAKEVEN_STOP" if breakeven_activated and exit_price >= entry_price else "STOP_LOSS",
                "win": pnl_pct > 0
            }

        # Check Take Profit hit
        if h >= target_price:
            exit_price = max(o, target_price)  # Account for gap ups above TP
            pnl_pct = (exit_price - entry_price) / entry_price * 100.0
            return {
                "exit_date": d,
                "exit_price": round(exit_price, 2),
                "pnl_pct": round(pnl_pct, 2),
                "holding_days": day_idx,
                "exit_reason": "TAKE_PROFIT",
                "win": True
            }

    # Hold expiration exit at last close
    last_d = future_dates[-1]
    last_c = df.loc[last_d]['close']
    pnl_pct = (last_c - entry_price) / entry_price * 100.0
    return {
        "exit_date": last_d,
        "exit_price": round(last_c, 2),
        "pnl_pct": round(pnl_pct, 2),
        "holding_days": len(future_dates),
        "exit_reason": "HOLD_EXPIRATION",
        "win": pnl_pct > 0
    }


def compute_metrics(trades_list):
    """Computes standard institutional performance metrics."""
    if not trades_list:
        return {
            "trades": 0, "wins": 0, "losses": 0, "win_rate": 0.0,
            "total_pnl": 0.0, "avg_pnl": 0.0, "profit_factor": 0.0,
            "payoff_ratio": 0.0, "max_dd": 0.0
        }

    pnls = [t["pnl_pct"] for t in trades_list]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    win_count = len(wins)
    loss_count = len(losses)
    total_trades = len(pnls)
    win_rate = (win_count / total_trades) * 100.0 if total_trades > 0 else 0.0

    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (99.9 if gross_profit > 0 else 0.0)

    avg_win = (sum(wins) / len(wins)) if wins else 0.0
    avg_loss = (abs(sum(losses)) / len(losses)) if losses else 0.0
    payoff_ratio = (avg_win / avg_loss) if avg_loss > 0 else 0.0

    # Equity curve and Max Drawdown
    equity = 100.0
    peak = 100.0
    max_dd = 0.0
    for p in pnls:
        equity *= (1.0 + p / 100.0)
        if equity > peak:
            peak = equity
        dd = (peak - equity) / peak * 100.0
        if dd > max_dd:
            max_dd = dd

    return {
        "trades": total_trades,
        "wins": win_count,
        "losses": loss_count,
        "win_rate": round(win_rate, 2),
        "total_pnl": round(sum(pnls), 2),
        "avg_pnl": round(float(np.mean(pnls)), 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "profit_factor": round(profit_factor, 2),
        "payoff_ratio": round(payoff_ratio, 2),
        "max_dd": round(max_dd, 2)
    }


def main():
    print("=" * 80)
    print("MIMIR BACKTEST: SECTOR ROTATION MATRIX & PRE-EARNINGS BEAT ENGINE")
    print("=" * 80)

    conn = get_db_connection()
    bars_by_ticker = load_market_data(conn)
    rs_lookup = compute_sector_rs_matrix(bars_by_ticker)

    # Fetch historical alerts with sentiment & sector info
    print("\nFetching historical signals from mimir_trade_signals...")
    sql_alerts = f"""
        SELECT s.id, s.ticker, s.signal_type, s.trigger_price, s.target_price, s.stop_loss,
               s.catalyst_type, s.sentiment_score, s.conviction_score, s.created_at,
               COALESCE(si.asset_sub_category, 'UNKNOWN') as sector
        FROM {schema}.mimir_trade_signals s
        LEFT JOIN LATERAL (
            SELECT asset_sub_category
            FROM {schema}.mimir_sentiment_impacts
            WHERE ticker = s.ticker AND asset_sub_category IS NOT NULL
            ORDER BY created_at DESC
            LIMIT 1
        ) si ON true
        WHERE s.signal_type = 'BUY'
          AND s.trigger_price IS NOT NULL AND s.trigger_price > 0
          AND s.created_at >= '2025-01-01'
        ORDER BY s.created_at ASC
    """
    df_alerts = pd.read_sql(sql_alerts, conn)
    conn.close()
    print(f"Loaded {len(df_alerts)} BUY signals to evaluate.")

    # Containers for benchmark cohorts
    trades_tailwind = []     # Sector RS >= +0.5% (Capital Inflow)
    trades_headwind = []     # Sector RS <= -0.5% (Capital Outflow)
    trades_pre_earnings = [] # PRE_EARNINGS_BEAT catalysts
    trades_home_run = []     # 3x ATR TP / 1.5x ATR SL Asymmetric setups

    for _, row in df_alerts.iterrows():
        ticker = row['ticker'].strip().upper()
        if ticker not in bars_by_ticker:
            continue

        created_dt = pd.to_datetime(row['created_at']).date()
        entry_price = float(row['trigger_price'])
        cat_type = str(row['catalyst_type'] or '')

        # Resolve sector ETF & Relative Strength on alert date
        sector = str(row['sector']).upper()
        etf = SUB_CATEGORY_TO_ETF.get(sector)
        rs_val = rs_lookup.get((etf, created_dt)) if etf else None

        # Price history up to alert date to compute ATR bounds
        df_hist = bars_by_ticker[ticker].loc[:created_dt]
        if len(df_hist) < 14:
            continue

        # Calculate 14-day ATR for Home Run setup
        tr = pd.concat([
            df_hist['high'] - df_hist['low'],
            (df_hist['high'] - df_hist['close'].shift(1)).abs(),
            (df_hist['low'] - df_hist['close'].shift(1)).abs()
        ], axis=1).max(axis=1)
        atr = float(tr.tail(14).mean())
        if np.isnan(atr) or atr <= 0:
            atr = entry_price * 0.03

        # 1. Standard Simulation with actual alert bounds (or 1.5x ATR SL / 2.5x ATR TP)
        sl_std = entry_price - max(0.035 * entry_price, min(0.075 * entry_price, 1.5 * atr))
        tp_std = entry_price + max(0.060 * entry_price, min(0.150 * entry_price, 2.5 * atr))

        res_std = simulate_trade(ticker, created_dt, entry_price, tp_std, sl_std, bars_by_ticker, max_holding_days=10)

        if res_std:
            # Classify by Sector Rotation Relative Strength
            if rs_val is not None:
                if rs_val >= 0.5:
                    trades_tailwind.append({**res_std, "ticker": ticker, "rs": rs_val, "sector": sector})
                elif rs_val <= -0.5:
                    trades_headwind.append({**res_std, "ticker": ticker, "rs": rs_val, "sector": sector})

        # 2. Home Run Simulation: 3x ATR TP, 1.5x ATR SL (Min 2.5:1 R/R, 15-day hold)
        sl_hr = entry_price - max(0.040 * entry_price, min(0.075 * entry_price, 1.5 * atr))
        tp_hr = entry_price + max(0.120 * entry_price, min(0.250 * entry_price, 3.0 * atr))
        tp_hr = max(tp_hr, entry_price + (entry_price - sl_hr) * 2.5)

        res_hr = simulate_trade(ticker, created_dt, entry_price, tp_hr, sl_hr, bars_by_ticker, max_holding_days=15)

        if res_hr:
            if rs_val is not None and rs_val >= 0.5:
                trades_home_run.append({**res_hr, "ticker": ticker, "rs": rs_val, "sector": sector})
            if cat_type == "PRE_EARNINGS_BEAT":
                trades_pre_earnings.append({**res_hr, "ticker": ticker})

    # Compute Metrics for Each Cohort
    m_tailwind = compute_metrics(trades_tailwind)
    m_headwind = compute_metrics(trades_headwind)
    m_pre_earnings = compute_metrics(trades_pre_earnings)
    m_home_run = compute_metrics(trades_home_run)

    print("\n" + "=" * 105)
    print("MIMIR STRATEGY BACKTEST MATRIX: SECTOR ROTATION & PRE-EARNINGS")
    print("=" * 105)
    print(f"{'Strategy / Cohort':<35} {'Trades':<8} {'Wins':<6} {'Losses':<8} {'Win Rate %':<12} {'Avg PnL %':<12} {'Profit Factor':<15} {'Payoff R/R':<12} {'Max DD %'}")
    print("-" * 105)
    print(f"{'1. Sector Headwind (RS <= -0.5%)':<35} {m_headwind['trades']:<8} {m_headwind['wins']:<6} {m_headwind['losses']:<8} {m_headwind['win_rate']:<12} {m_headwind['avg_pnl']:+6.2f}%      {m_headwind['profit_factor']:<15} {m_headwind['payoff_ratio']:<12} {m_headwind['max_dd']:.2f}%")
    print(f"{'2. Sector Tailwind (RS >= +0.5%)':<35} {m_tailwind['trades']:<8} {m_tailwind['wins']:<6} {m_tailwind['losses']:<8} {m_tailwind['win_rate']:<12} {m_tailwind['avg_pnl']:+6.2f}%      {m_tailwind['profit_factor']:<15} {m_tailwind['payoff_ratio']:<12} {m_tailwind['max_dd']:.2f}%")
    print(f"{'3. Pre-Earnings Beat Setups':<35} {m_pre_earnings['trades']:<8} {m_pre_earnings['wins']:<6} {m_pre_earnings['losses']:<8} {m_pre_earnings['win_rate']:<12} {m_pre_earnings['avg_pnl']:+6.2f}%      {m_pre_earnings['profit_factor']:<15} {m_pre_earnings['payoff_ratio']:<12} {m_pre_earnings['max_dd']:.2f}%")
    print(f"{'4. Home Run + Sector Tailwind':<35} {m_home_run['trades']:<8} {m_home_run['wins']:<6} {m_home_run['losses']:<8} {m_home_run['win_rate']:<12} {m_home_run['avg_pnl']:+6.2f}%      {m_home_run['profit_factor']:<15} {m_home_run['payoff_ratio']:<12} {m_home_run['max_dd']:.2f}%")
    print("=" * 105)

    print("\n--- EXIT REASON BREAKDOWN: HOME RUN + SECTOR TAILWIND ---")
    exit_counts = defaultdict(int)
    exit_pnls = defaultdict(list)
    for t in trades_home_run:
        exit_counts[t['exit_reason']] += 1
        exit_pnls[t['exit_reason']].append(t['pnl_pct'])

    for reason, count in sorted(exit_counts.items(), key=lambda x: x[1], reverse=True):
        avg_p = float(np.mean(exit_pnls[reason]))
        wr = sum(1 for p in exit_pnls[reason] if p > 0) / count * 100.0
        print(f"{reason:<20}: {count:>4} trades | Win Rate: {wr:5.1f}% | Avg Return: {avg_p:+5.2f}% | Total Return: {sum(exit_pnls[reason]):+7.2f}%")

if __name__ == "__main__":
    main()
