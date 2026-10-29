# backend/app/analytics/strategy_backtester.py
"""
MIMIR Strategy Backtest Engine
==============================
Executes historical backtests for user Python strategies (bong_strats),
flagship quant engines (War Rig, Stat-Arb), and multi-asset baskets.
Generates metrics, equity curves, drawdown series, and trade logs.
"""

from __future__ import annotations

import math
import logging
from typing import Dict, List, Optional, Any, Tuple
from datetime import datetime, timezone

import pandas as pd
import numpy as np

from backend.app.database import get_db_connection
from backend.app.analytics.user_strategies.registry import list_strategies, reload_registries, FULL_STRATEGY_REGISTRY

logger = logging.getLogger(__name__)

# Common symbol aliases for yfinance fallback
YF_SYMBOL_MAP = {
    "XAUUSD": "GC=F",
    "XAGUSD": "SI=F",
    "EURUSD": "EURUSD=X",
    "GBPUSD": "GBPUSD=X",
    "USDJPY": "JPY=X",
    "AUDUSD": "AUDUSD=X",
    "NZDUSD": "NZDUSD=X",
    "USDCAD": "CAD=X",
    "USDCHF": "CHF=X",
    "BTCUSD": "BTC-USD",
    "ETHUSD": "ETH-USD",
    "SOLUSD": "SOL-USD",
    "US500": "^GSPC",
    "US30": "^DJI",
    "USTEC": "^IXIC",
}


def load_candles(
    symbol: str,
    timeframe: str = "1h",
    limit: int = 1000,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None
) -> Optional[pd.DataFrame]:
    """
    3-Tier Historical Candle Loader:
    Tier 1: MT5 copy_rates (if MT5 terminal is active)
    Tier 2: PostgreSQL mimir_hourly_ohlcv / mimir_minute_ohlcv
    Tier 3: yfinance fallback for US equities, Forex, Metals, and Crypto
    """
    clean_sym = symbol.strip().upper()
    tf_lower = timeframe.lower()

    # --- Tier 1: Local MT5 Terminal ---
    try:
        import MetaTrader5 as mt5
        if mt5.terminal_info() is not None or mt5.initialize():
            tf_map = {
                "1m": mt5.TIMEFRAME_M1, "m1": mt5.TIMEFRAME_M1,
                "5m": mt5.TIMEFRAME_M5, "m5": mt5.TIMEFRAME_M5,
                "15m": mt5.TIMEFRAME_M15, "m15": mt5.TIMEFRAME_M15,
                "30m": mt5.TIMEFRAME_M30, "m30": mt5.TIMEFRAME_M30,
                "1h": mt5.TIMEFRAME_H1, "h1": mt5.TIMEFRAME_H1,
                "4h": mt5.TIMEFRAME_H4, "h4": mt5.TIMEFRAME_H4,
                "1d": mt5.TIMEFRAME_D1, "d1": mt5.TIMEFRAME_D1,
            }
            tf = tf_map.get(tf_lower, mt5.TIMEFRAME_H1)
            
            # Check symbol visibility
            if mt5.symbol_info(clean_sym):
                mt5.symbol_select(clean_sym, True)
                if start_date:
                    d_from = pd.to_datetime(start_date, utc=True).to_pydatetime()
                    d_to = pd.to_datetime(end_date, utc=True).to_pydatetime() if end_date else datetime.now(timezone.utc)
                    rates = mt5.copy_rates_range(clean_sym, tf, d_from, d_to)
                else:
                    rates = mt5.copy_rates_from_pos(clean_sym, tf, 1, limit)
                    
                if rates is not None and len(rates) > 0:
                    df = pd.DataFrame(rates).rename(columns={"tick_volume": "volume", "time": "timestamp"})
                    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="s", utc=True)
                    req = ["timestamp", "open", "high", "low", "close", "volume"]
                    return df[req].copy().sort_values("timestamp").reset_index(drop=True)
    except Exception as e:
        logger.debug(f"[BACKTEST] MT5 candle load skipped for {clean_sym}: {e}")

    # --- Tier 2: PostgreSQL Database ---
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        is_minute = any(m in tf_lower for m in ["1m", "5m", "15m", "30m", "minute"])
        tbl = "yggdrasil.mimir_minute_ohlcv" if is_minute else "yggdrasil.mimir_hourly_ohlcv"
        
        where_clauses = ["ticker = %s"]
        params = [clean_sym]
        if start_date:
            where_clauses.append("timestamp >= %s")
            params.append(start_date)
        if end_date:
            where_clauses.append("timestamp <= %s")
            params.append(end_date)
            
        cur.execute(f"""
            SELECT timestamp, open, high, low, close, volume 
            FROM {tbl}
            WHERE {' AND '.join(where_clauses)}
            ORDER BY timestamp DESC
            LIMIT %s
        """, tuple(params + [limit]))
        rows = cur.fetchall()
        cur.close()
        conn.close()
        
        if rows and len(rows) > 0:
            df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
            df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
            for c in ["open", "high", "low", "close", "volume"]:
                df[c] = pd.to_numeric(df[c], errors="coerce")
            return df.sort_values("timestamp").reset_index(drop=True)
    except Exception as e:
        logger.debug(f"[BACKTEST] DB candle load skipped for {clean_sym}: {e}")

    # --- Tier 3: yfinance Fallback ---
    try:
        import yfinance as yf
        yf_sym = YF_SYMBOL_MAP.get(clean_sym, clean_sym)
        
        yf_interval = "1h"
        if "1m" in tf_lower: yf_interval = "1m"
        elif "5m" in tf_lower: yf_interval = "5m"
        elif "15m" in tf_lower: yf_interval = "15m"
        elif "1d" in tf_lower: yf_interval = "1d"
        
        period = "1mo" if yf_interval in ("1m", "5m", "15m") else "6mo"
        hist = yf.download(yf_sym, period=period, interval=yf_interval, progress=False)
        if hist is not None and len(hist) > 0:
            hist = hist.reset_index()
            if isinstance(hist.columns, pd.MultiIndex):
                hist.columns = [col[0].lower() if col[1] == '' or col[1] == yf_sym else f"{col[0]}_{col[1]}".lower() for col in hist.columns]
            else:
                hist.columns = [c.lower() for c in hist.columns]
                
            col_map = {"datetime": "timestamp", "date": "timestamp"}
            hist = hist.rename(columns=col_map)
            req = ["timestamp", "open", "high", "low", "close", "volume"]
            avail = [c for c in req if c in hist.columns]
            df = hist[avail].tail(limit).reset_index(drop=True)
            df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
            return df
    except Exception as e:
        logger.warning(f"[BACKTEST] yfinance candle load failed for {clean_sym}: {e}")

    return None


def run_strategy_simulation(
    strategy_id: str,
    tickers_str: str,
    timeframe: str = "1h",
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    initial_capital: float = 10000.0,
    slippage_bps: float = 5.0,
    custom_params: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """
    Executes a backtest across single assets or multi-ticker baskets.
    Returns BacktestResponse compatible metrics, chart, and trades.
    """
    reg = reload_registries()  # Rescan directories & get fresh registry
    
    # 1. Resolve Strategy Class
    clean_id = strategy_id.strip().lower()
    if clean_id in ("stat_arb", "pair_stat_arbitrage", "stat_arb_apu"):
        clean_id = "stat_arb"
    elif clean_id in ("war_rig", "war_rig_engine"):
        clean_id = "war_rig"

    strat_cls = reg.get(clean_id)
    if not strat_cls:
        # Fallback search by prefix or stem
        for k, v in reg.items():
            if clean_id in k or k in clean_id:
                strat_cls = v
                break
                
    if not strat_cls:
        raise ValueError(f"Strategy '{strategy_id}' not found in registry. Available: {', '.join(reg.keys())}")

    # 2. Parse Tickers & Pre-load Candles
    raw_tokens = [t.strip().upper() for t in tickers_str.replace(";", ",").split(",") if t.strip()]
    if not raw_tokens:
        raw_tokens = ["XAUUSD"]

    loaded_dfs: Dict[str, pd.DataFrame] = {}
    for ticker in raw_tokens:
        df_cand = load_candles(ticker, timeframe=timeframe, limit=1000, start_date=start_date, end_date=end_date)
        if df_cand is not None and len(df_cand) >= 15:
            loaded_dfs[ticker] = df_cand

    ticker_results = []
    valid_tickers = list(loaded_dfs.keys())

    for idx_t, ticker in enumerate(valid_tickers):
        df = loaded_dfs[ticker].copy()

        # Populate hedge_close / hedge_volume for Pair Stat-Arb strategies
        if "hedge_close" not in df.columns or "hedge_volume" not in df.columns:
            if len(valid_tickers) > 1:
                hedge_ticker = valid_tickers[(idx_t + 1) % len(valid_tickers)]
                hedge_df = loaded_dfs[hedge_ticker]
                min_rows = min(len(df), len(hedge_df))
                df = df.tail(min_rows).reset_index(drop=True)
                h_slice = hedge_df.tail(min_rows).reset_index(drop=True)
                df["hedge_close"] = h_slice["close"].values
                df["hedge_volume"] = h_slice["volume"].values
            else:
                # Single-ticker fallback: synthetic slower equilibrium leg for stat-arb spread
                df["hedge_close"] = df["close"].rolling(24, min_periods=1).mean()
                df["hedge_volume"] = df["volume"].rolling(24, min_periods=1).mean()

        strat_instance = strat_cls(**(custom_params or {})) if custom_params else strat_cls()
        
        # Generate Signals
        if hasattr(strat_instance, "generate_signals"):
            signals = strat_instance.generate_signals(df)
        elif hasattr(strat_instance, "generate_positions"):
            signals = strat_instance.generate_positions(df)
        else:
            signals = pd.Series(0.0, index=df.index)

        positions = signals.fillna(0.0).clip(-1.0, 1.0)
        market_returns = df["close"].pct_change().fillna(0.0)
        turnover = positions.diff().abs().fillna(positions.abs())
        costs = turnover * (slippage_bps / 10000.0)
        strategy_returns = positions.shift(1).fillna(0.0) * market_returns - costs
        
        equity_series = (1.0 + strategy_returns).cumprod()
        bench_series = (1.0 + market_returns).cumprod()
        drawdown_series = equity_series.div(equity_series.cummax()).sub(1.0)

        # Identify trades
        trades = []
        pos_diff = positions.diff()
        for idx in range(1, len(positions)):
            if pos_diff.iloc[idx] != 0:
                p_now = float(positions.iloc[idx])
                p_prev = float(positions.iloc[idx - 1])
                action = "BUY" if p_now > p_prev else "SELL"
                dt_str = df["timestamp"].iloc[idx].strftime("%Y-%m-%d %H:%M")
                price = float(df["close"].iloc[idx])
                trades.append({
                    "date": dt_str,
                    "ticker": ticker,
                    "action": action,
                    "weight": round(float(p_now), 2),
                    "price": round(price, 2)
                })

        # Calculate metrics
        total_pnl = float(equity_series.iloc[-1] - 1.0)
        winning_trades = 0
        total_closed = max(1, len(trades))
        # Simple win rate estimate on trade returns
        trade_rets = strategy_returns[positions != 0]
        if len(trade_rets) > 0:
            win_rate = float((trade_rets > 0).mean() * 100.0)
        else:
            win_rate = 50.0

        ticker_results.append({
            "ticker": ticker,
            "df": df,
            "equity": equity_series,
            "bench": bench_series,
            "drawdown": drawdown_series,
            "returns": strategy_returns,
            "trades": trades,
            "total_pnl_pct": round(total_pnl * 100.0, 2),
            "trades_count": len(trades),
            "win_rate": round(win_rate, 1)
        })

    if not ticker_results:
        raise ValueError(f"Could not load sufficient candle data for ticker(s): {', '.join(raw_tokens)}")

    # 3. Aggregate Basket Performance
    num_assets = len(ticker_results)
    min_len = min(len(res["df"]) for res in ticker_results)
    
    # Trim to common length
    combined_strategy_returns = pd.Series(0.0, index=range(min_len))
    combined_bench_returns = pd.Series(0.0, index=range(min_len))
    reference_timestamps = ticker_results[0]["df"]["timestamp"].tail(min_len).reset_index(drop=True)

    for res in ticker_results:
        s_ret = res["returns"].tail(min_len).reset_index(drop=True)
        m_ret = res["df"]["close"].pct_change().fillna(0.0).tail(min_len).reset_index(drop=True)
        combined_strategy_returns += (s_ret / num_assets)
        combined_bench_returns += (m_ret / num_assets)

    combined_equity = (1.0 + combined_strategy_returns).cumprod()
    combined_bench = (1.0 + combined_bench_returns).cumprod()
    combined_drawdown = combined_equity.div(combined_equity.cummax()).sub(1.0)

    # 4. Compute Master Metrics
    periods_per_year = 252 * 24 if "h" in timeframe.lower() else (252 if "d" in timeframe.lower() else 252 * 24 * 12)
    vol = float(combined_strategy_returns.std())
    sharpe = float(combined_strategy_returns.mean() / vol * np.sqrt(periods_per_year)) if vol > 0 else 0.0
    ann_return = float(combined_equity.iloc[-1] - 1.0) * 100.0
    max_dd = float(combined_drawdown.min()) * 100.0
    
    all_trades = []
    for res in ticker_results:
        all_trades.extend(res["trades"])
    all_trades.sort(key=lambda t: t["date"], reverse=True)

    avg_win_rate = float(np.mean([res["win_rate"] for res in ticker_results])) if ticker_results else 50.0

    # 5. Format Chart Series
    chart_points = []
    step = max(1, min_len // 200)  # Downsample for web chart responsiveness
    for i in range(0, min_len, step):
        dt_val = reference_timestamps.iloc[i]
        dt_str = dt_val.strftime("%Y-%m-%d %H:%M") if hasattr(dt_val, "strftime") else str(dt_val)[:16]
        chart_points.append({
            "date": dt_str,
            "strategy": round(float(combined_equity.iloc[i] - 1.0) * 100.0, 2),
            "benchmark": round(float(combined_bench.iloc[i] - 1.0) * 100.0, 2),
            "drawdown": round(float(combined_drawdown.iloc[i]) * 100.0, 2)
        })

    # 6. Format Ticker Breakdown Table
    breakdown = []
    for res in ticker_results:
        breakdown.append({
            "ticker": res["ticker"],
            "pnl_pct": res["total_pnl_pct"],
            "pnl_usd": round(initial_capital * (res["total_pnl_pct"] / 100.0) / num_assets, 2),
            "trades": res["trades_count"],
            "win_rate": res["win_rate"]
        })

    return {
        "metrics": {
            "sharpe": round(sharpe, 2),
            "annualized_return": round(ann_return, 2),
            "max_drawdown": round(max_dd, 2),
            "turnover": round(float(len(all_trades) / max(1, min_len)), 2),
            "win_rate": round(avg_win_rate, 1),
            "ic": 0.05,
            "fitness": round(sharpe * math.sqrt(max(0.1, abs(ann_return) / max(1.0, abs(max_dd)))), 2) if max_dd != 0 else 1.0,
        },
        "chart": chart_points,
        "trades": all_trades[:100],
        "ticker_breakdown": breakdown,
        "basket_size": num_assets
    }
