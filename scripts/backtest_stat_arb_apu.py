"""Institutional Statistical Arbitrage APU Backtester.

Empirically tests the MIMIR Stat-Arb APU across the 12 active pairs in APU_PAIRS:
  - Tier 1 Titans (Duopolies: NVDA:AMD, MSFT:GOOGL, AAPL:MSFT, CVX:XOM, GS:MS, KO:PEP)
  - Macro Metals & Indices (XAUUSD:XAGUSD, US500:US30)
  - Tier 2 Niche Commodities & Baskets (CORN:WEAT, BDRY:SBLK, URA:NLR, GDX:GDXJ)

Compares:
  1. Pure Quantitative Baseline (Online Kalman Filter + ADF Stationarity)
  2. Full APU with Sentiment Circuit Breaker (DeepSeek NLP Delta-S Vetoes & Boosts)

Incorporates institutional friction:
  - 0.5 bps commission + 0.5 bps bid-ask crossing per leg (4.0 bps round-trip per pair).
"""

from __future__ import annotations

import argparse
import datetime
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Safe console output encoding on Windows
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except AttributeError:
        pass

from backend.app.analytics.stat_arb_apu import APU_PAIRS
from backend.app.analytics.user_strategies.indicators import (
    KalmanPairEstimator,
    OUCalibrator,
    compute_adf_stat,
)
from backend.app.database import get_db_connection
from backend.app.config import get_settings

settings = get_settings()


def load_mt5_bars(symbol: str, timeframe: str = "H1", bars: int = 4000) -> pd.DataFrame:
    """Download completed OHLCV bars from MT5."""
    import MetaTrader5 as mt5

    if mt5.terminal_info() is None and not mt5.initialize():
        raise RuntimeError("Could not connect to MT5 terminal.")

    clean_symbol = symbol.strip().upper()
    candidates = [
        clean_symbol,
        f"{clean_symbol}.US",
        f"#{clean_symbol}",
        clean_symbol.replace(".", ""),
        f"{clean_symbol.replace('.', '')}.US",
    ]
    broker_symbol = next((c for c in candidates if mt5.symbol_info(c)), None)
    if not broker_symbol:
        raise ValueError(f"Could not resolve '{symbol}' in MT5 Market Watch.")

    info = mt5.symbol_info(broker_symbol)
    if info is not None and not info.visible:
        mt5.symbol_select(broker_symbol, True)

    tf_map = {
        "M5": mt5.TIMEFRAME_M5,
        "M15": mt5.TIMEFRAME_M15,
        "M30": mt5.TIMEFRAME_M30,
        "H1": mt5.TIMEFRAME_H1,
        "H4": mt5.TIMEFRAME_H4,
        "D1": mt5.TIMEFRAME_D1,
    }
    tf = tf_map.get(timeframe.upper(), mt5.TIMEFRAME_H1)
    rates = mt5.copy_rates_from_pos(broker_symbol, tf, 1, bars)
    if rates is None or len(rates) == 0:
        raise RuntimeError(f"MT5 returned no history for {broker_symbol}.")

    df = pd.DataFrame(rates).rename(columns={"tick_volume": "volume", "time": "timestamp"})
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="s", utc=True)
    return df[["timestamp", "open", "high", "low", "close", "volume"]].copy().reset_index(drop=True)


def load_historical_sentiment(tickers: list[str]) -> pd.DataFrame:
    """Fetch all sentiment impacts with published timestamps from PostgreSQL."""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(f"""
            SELECT si.ticker, a.published_ts, si.sentiment_score
            FROM {settings.mimir_schema}.mimir_sentiment_impacts si
            JOIN {settings.mimir_schema}.mimir_raw_articles a ON a.id = si.article_id
            WHERE si.ticker = ANY(%s)
              AND a.published_ts IS NOT NULL
            ORDER BY a.published_ts ASC
        """, (tickers,))
        rows = cur.fetchall()
        cur.close()
        conn.close()
        if not rows:
            return pd.DataFrame(columns=["ticker", "published_ts", "sentiment_score"])
        df = pd.DataFrame(rows, columns=["ticker", "published_ts", "sentiment_score"])
        df["published_ts"] = pd.to_datetime(df["published_ts"], utc=True)
        df["sentiment_score"] = df["sentiment_score"].astype(float)
        return df
    except Exception as e:
        print(f"[WARN] Failed to load sentiment from DB: {e}")
        return pd.DataFrame(columns=["ticker", "published_ts", "sentiment_score"])


def get_sentiment_for_window(
    sentiment_df: pd.DataFrame,
    ticker: str,
    as_of: pd.Timestamp,
    lookback_hours: int = 24,
) -> float:
    """Get average sentiment for a ticker in the lookback window before as_of."""
    if sentiment_df.empty:
        return 0.0
    start_ts = as_of - pd.Timedelta(hours=lookback_hours)
    mask = (
        (sentiment_df["ticker"] == ticker)
        & (sentiment_df["published_ts"] >= start_ts)
        & (sentiment_df["published_ts"] <= as_of)
    )
    matches = sentiment_df.loc[mask, "sentiment_score"]
    return float(matches.mean()) if not matches.empty else 0.0


def simulate_apu_pair(
    p_df: pd.DataFrame,
    h_df: pd.DataFrame,
    ticker1: str,
    ticker2: str,
    sentiment_df: pd.DataFrame,
    use_sentiment_circuit_breaker: bool = True,
    entry_z: float = 1.8,
    exit_z: float = 0.3,
    stop_z: float = 3.5,
    adf_threshold: float = -2.2,
    lookback_spread: int = 60,
    friction_bps: float = 1.0,  # 0.5 bps commission + 0.5 bps spread crossing per leg
) -> tuple[dict, pd.DataFrame, list[dict], list[dict]]:
    """Simulate Stat-Arb APU execution on an aligned pair bar-by-bar."""
    # Synchronize timestamps
    merged = pd.merge(
        p_df[["timestamp", "close"]].rename(columns={"close": "p_close"}),
        h_df[["timestamp", "close"]].rename(columns={"close": "h_close"}),
        on="timestamp",
        how="inner",
    ).sort_values("timestamp").reset_index(drop=True)

    n = len(merged)
    if n < lookback_spread + 30:
        return {}, merged, [], []

    p_prices = merged["p_close"].values.astype(float)
    h_prices = merged["h_close"].values.astype(float)
    p_log = np.log(p_prices)
    h_log = np.log(h_prices)

    # 1. Kalman Filter Estimation
    kalman = KalmanPairEstimator(delta=1e-4, R=1e-3)
    k_alpha, k_beta, k_spread, k_std, k_z_raw = kalman.fit_series(p_log, h_log)

    # Standardized rolling z-score of Kalman spread (distance from rolling mean in sigmas)
    spread_s = pd.Series(k_spread)
    spread_mean = spread_s.rolling(lookback_spread, min_periods=lookback_spread).mean()
    spread_std = spread_s.rolling(lookback_spread, min_periods=lookback_spread).std().replace(0, np.nan)
    k_z = ((spread_s - spread_mean) / spread_std).fillna(0.0).values

    merged["beta"] = k_beta
    merged["spread"] = k_spread
    merged["zscore"] = k_z

    # Rolling ADF and OU Half-Life
    adf_arr = np.full(n, np.nan)
    hl_arr = np.full(n, np.nan)
    for i in range(lookback_spread, n):
        win = k_spread[i - lookback_spread : i + 1]
        adf_arr[i] = compute_adf_stat(win)
        hl_arr[i] = OUCalibrator.half_life(win)

    merged["adf_stat"] = adf_arr
    merged["half_life"] = hl_arr

    # 2. Bar-by-bar trade simulation
    in_trade = False
    direction = ""
    entry_idx = 0
    entry_p_px = 0.0
    entry_h_px = 0.0
    active_beta = 1.0
    max_hold_bars = 48
    trade_id = 0

    trades: list[dict] = []
    vetoed_trades: list[dict] = []

    # Position series for portfolio evaluation
    p_pos = np.zeros(n)
    h_pos = np.zeros(n)

    # Pre-filter sentiment for pair tickers to optimize lookup speed
    pair_sent = sentiment_df[sentiment_df["ticker"].isin([ticker1, ticker2])].copy()

    for i in range(lookback_spread, n):
        ts = merged["timestamp"].iloc[i]
        z = k_z[i]
        beta_val = k_beta[i]
        adf_val = adf_arr[i]
        hl_val = hl_arr[i]

        p_px = p_prices[i]
        h_px = h_prices[i]

        if in_trade:
            bars_held = i - entry_idx
            exit_reason = None

            # Convergence
            if (direction == "LONG_SPREAD" and z >= -exit_z) or (direction == "SHORT_SPREAD" and z <= exit_z):
                exit_reason = "TARGET_REVERSION"
            # Stop Loss
            elif abs(z) >= stop_z:
                exit_reason = "STOP_LOSS"
            # Time Stop (OU Half-Life)
            elif bars_held >= max_hold_bars:
                exit_reason = "HALF_LIFE_EXPIRED"

            if exit_reason:
                # Calculate return
                if direction == "LONG_SPREAD":
                    # Long Primary, Short Hedge
                    p_ret = (p_px - entry_p_px) / entry_p_px
                    h_ret = (entry_h_px - h_px) / entry_h_px
                else:
                    # Short Primary, Long Hedge
                    p_ret = (entry_p_px - p_px) / entry_p_px
                    h_ret = (h_px - entry_h_px) / entry_h_px

                # Dollar-neutral weighted return
                gross_ret = 0.5 * p_ret + 0.5 * h_ret
                # Round-trip friction on both legs (4 legs total = 4 * friction_bps)
                friction_drag = (4 * friction_bps) / 10_000
                net_ret = gross_ret - friction_drag

                trades.append({
                    "trade_id": trade_id,
                    "ticker1": ticker1,
                    "ticker2": ticker2,
                    "direction": direction,
                    "entry_time": merged["timestamp"].iloc[entry_idx],
                    "exit_time": ts,
                    "bars_held": bars_held,
                    "entry_primary": entry_p_px,
                    "exit_primary": p_px,
                    "entry_hedge": entry_h_px,
                    "exit_hedge": h_px,
                    "hedge_ratio": active_beta,
                    "gross_return_pct": gross_ret,
                    "net_return_pct": net_ret,
                    "exit_reason": exit_reason,
                    "entry_z": k_z[entry_idx],
                    "exit_z": z,
                })
                in_trade = False
            else:
                # Maintain active positions
                p_pos[i] = 1.0 if direction == "LONG_SPREAD" else -1.0
                h_pos[i] = -active_beta if direction == "LONG_SPREAD" else active_beta

        elif pd.notna(z) and pd.notna(adf_val) and beta_val > 0:
            # Entry condition: Z divergence + Stationary regime
            is_stationary = adf_val <= adf_threshold

            candidate_dir = None
            if z <= -entry_z and is_stationary:
                candidate_dir = "LONG_SPREAD"
            elif z >= entry_z and is_stationary:
                candidate_dir = "SHORT_SPREAD"

            if candidate_dir:
                # Sentiment Circuit Breaker Check
                s1 = get_sentiment_for_window(pair_sent, ticker1, ts, lookback_hours=24)
                s2 = get_sentiment_for_window(pair_sent, ticker2, ts, lookback_hours=24)
                s_delta = s1 - s2

                veto = False
                veto_reason = ""

                if candidate_dir == "LONG_SPREAD":
                    # Buying T1, Selling T2.
                    # VETO: T1 price collapsed on bad fundamental news (< -0.30)
                    if s1 < -0.30:
                        veto = True
                        veto_reason = f"VETO_FALLING_KNIFE: {ticker1} news sentiment={s1:+.2f} < -0.30"
                elif candidate_dir == "SHORT_SPREAD":
                    # Shorting T1, Buying T2.
                    # VETO: T1 price surged on huge positive catalyst (> +0.40)
                    if s1 > 0.40:
                        veto = True
                        veto_reason = f"VETO_BREAKOUT_CATALYST: {ticker1} news sentiment={s1:+.2f} > +0.40"

                if veto and use_sentiment_circuit_breaker:
                    vetoed_trades.append({
                        "ticker1": ticker1,
                        "ticker2": ticker2,
                        "timestamp": ts,
                        "direction": candidate_dir,
                        "zscore": z,
                        "sentiment_t1": s1,
                        "sentiment_t2": s2,
                        "reason": veto_reason,
                    })
                else:
                    # Execute entry
                    trade_id += 1
                    in_trade = True
                    direction = candidate_dir
                    entry_idx = i
                    entry_p_px = p_px
                    entry_h_px = h_px
                    active_beta = beta_val
                    # Dynamic holding period capped at 2.5 * half-life
                    if pd.notna(hl_val) and hl_val > 1:
                        max_hold_bars = int(np.clip(2.5 * hl_val, 12, 120))
                    else:
                        max_hold_bars = 48

                    p_pos[i] = 1.0 if direction == "LONG_SPREAD" else -1.0
                    h_pos[i] = -active_beta if direction == "LONG_SPREAD" else active_beta

    # Continuous equity curve evaluation
    p_returns = merged["p_close"].pct_change().fillna(0.0)
    h_returns = merged["h_close"].pct_change().fillna(0.0)

    p_pos_series = pd.Series(p_pos, index=merged.index)
    h_pos_series = pd.Series(h_pos, index=merged.index)

    turnover = (
        p_pos_series.diff().abs().fillna(p_pos_series.abs())
        + h_pos_series.diff().abs().fillna(h_pos_series.abs())
    )
    costs = turnover * friction_bps / 10_000

    # Half capital on each leg
    bar_returns = 0.5 * (p_pos_series.shift(1).fillna(0.0) * p_returns + h_pos_series.shift(1).fillna(0.0) * h_returns) - costs
    equity = (1.0 + bar_returns).cumprod()
    drawdown = equity.div(equity.cummax()).sub(1.0)
    volatility = bar_returns.std()
    periods_per_year = 252 * 7  # approx 1,764 H1 bars per year
    sharpe = (bar_returns.mean() / volatility * np.sqrt(periods_per_year)) if volatility > 0 else 0.0

    # Trade statistics
    trade_df = pd.DataFrame(trades)
    if not trade_df.empty:
        wins = trade_df["net_return_pct"] > 0
        win_rate = float(wins.mean())
        pos_sum = trade_df.loc[wins, "net_return_pct"].sum()
        neg_sum = abs(trade_df.loc[~wins, "net_return_pct"].sum())
        profit_factor = float(pos_sum / neg_sum) if neg_sum > 0 else 999.0
        avg_ret = float(trade_df["net_return_pct"].mean())
        avg_bars = float(trade_df["bars_held"].mean())
    else:
        win_rate = 0.0
        profit_factor = 0.0
        avg_ret = 0.0
        avg_bars = 0.0

    metrics = {
        "ticker1": ticker1,
        "ticker2": ticker2,
        "pair": f"{ticker1}:{ticker2}",
        "total_trades": len(trades),
        "vetoed_trades": len(vetoed_trades),
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "cumulative_return": float(equity.iloc[-1] - 1.0),
        "max_drawdown": float(drawdown.min()),
        "sharpe_ratio": float(sharpe),
        "avg_trade_return": avg_ret,
        "avg_bars_held": avg_bars,
        "market_exposure": float(((p_pos_series != 0) | (h_pos_series != 0)).mean()),
    }

    merged["equity"] = equity
    return metrics, merged, trades, vetoed_trades


def run_full_apu_backtest(
    timeframe: str = "H1",
    bars: int = 4000,
    friction_bps: float = 1.0,
) -> dict:
    """Run backtest for all pairs across Pure Math vs Sentiment-Filtered APU."""
    print("=" * 110)
    print("MIMIR STAT-ARB APU INSTITUTIONAL BACKTEST ENGINE")
    print(f"Timeframe: {timeframe} | Historical Bars: {bars} | Institutional Friction: {friction_bps:.1f} bps/leg")
    print("=" * 110)

    # 1. Fetch universe tickers and load sentiment
    universe_tickers = sorted(list(set(
        [p["ticker1"] for p in APU_PAIRS] + [p["ticker2"] for p in APU_PAIRS]
    )))
    print(f"Loading historical DeepSeek NLP sentiment for {len(universe_tickers)} tickers...")
    sentiment_df = load_historical_sentiment(universe_tickers)
    print(f"-> Loaded {len(sentiment_df)} sentiment records.\n")

    baseline_results = []
    apu_results = []
    all_apu_trades: list[dict] = []
    all_vetoed_trades: list[dict] = []
    apu_equity_dict: dict[str, pd.Series] = {}

    for p in APU_PAIRS:
        t1, t2 = p["ticker1"], p["ticker2"]
        pair_label = f"{t1}:{t2}"
        desc = p["name"]
        print(f"-> Running pair: {desc} ({pair_label}) [{p['cluster']}]...")

        try:
            p_bars = load_mt5_bars(t1, timeframe=timeframe, bars=bars)
            h_bars = load_mt5_bars(t2, timeframe=timeframe, bars=bars)
        except Exception as e:
            print(f"   [SKIP] Failed to load data from MT5 for {pair_label}: {e}")
            continue

        # Run 1: Pure Quantitative Baseline (without sentiment circuit-breaker)
        m_base, _, _, _ = simulate_apu_pair(
            p_bars, h_bars, t1, t2, sentiment_df,
            use_sentiment_circuit_breaker=False,
            friction_bps=friction_bps,
        )
        if m_base:
            m_base["cluster"] = p["cluster"]
            m_base["type"] = p["type"]
            baseline_results.append(m_base)

        # Run 2: Full APU with DeepSeek Sentiment Circuit-Breaker
        m_apu, df_apu, trades_apu, vetoed_apu = simulate_apu_pair(
            p_bars, h_bars, t1, t2, sentiment_df,
            use_sentiment_circuit_breaker=True,
            friction_bps=friction_bps,
        )
        if m_apu:
            m_apu["cluster"] = p["cluster"]
            m_apu["type"] = p["type"]
            apu_results.append(m_apu)
            all_apu_trades.extend(trades_apu)
            all_vetoed_trades.extend(vetoed_apu)
            apu_equity_dict[pair_label] = df_apu["equity"]
            print(f"   [DONE] APU Return: {m_apu['cumulative_return']:+.2%} | Win Rate: {m_apu['win_rate']:.1%} | Trades: {m_apu['total_trades']} (Vetoed: {m_apu['vetoed_trades']}) | Sharpe: {m_apu['sharpe_ratio']:.2f}")

    if not apu_results:
        print("[ERROR] No pairs finished successfully.")
        return {}

    # 3. Portfolio aggregation
    eq_df = pd.DataFrame(apu_equity_dict).ffill().fillna(1.0)
    rets = eq_df.pct_change().fillna(0.0)

    # Equal Weight Portfolio
    eq_returns = rets.mean(axis=1)
    eq_equity = (1.0 + eq_returns).cumprod()
    eq_dd = eq_equity.div(eq_equity.cummax()).sub(1.0)
    eq_vol = eq_returns.std()
    periods_yr = 252 * 7
    eq_sharpe = (eq_returns.mean() / eq_vol * np.sqrt(periods_yr)) if eq_vol > 0 else 0.0

    # Risk-Parity Portfolio
    vols = rets.std()
    inv_vols = 1.0 / vols.replace(0, np.nan)
    rp_weights = (inv_vols / inv_vols.sum()).fillna(1.0 / len(rets.columns))
    rp_returns = (rets * rp_weights).sum(axis=1)
    rp_equity = (1.0 + rp_returns).cumprod()
    rp_dd = rp_equity.div(rp_equity.cummax()).sub(1.0)
    rp_vol = rp_returns.std()
    rp_sharpe = (rp_returns.mean() / rp_vol * np.sqrt(periods_yr)) if rp_vol > 0 else 0.0

    # Print Comparison Table
    print("\n" + "=" * 115)
    print("STAT-ARB APU: PERFORMANCE BREAKDOWN (SENTIMENT-AWARE vs PURE QUANT BASELINE)")
    print("=" * 115)
    print(f"{'Pair':<14} | {'Cluster':<22} | {'Trades':<6} | {'APU Ret':<8} | {'APU Win%':<8} | {'APU PF':<6} | {'APU DD':<7} | {'Base Win%':<9} | {'Vetoed':<6}")
    print("-" * 115)

    base_map = {b["pair"]: b for b in baseline_results}
    for m in apu_results:
        b = base_map.get(m["pair"], {})
        b_win = f"{b.get('win_rate', 0.0):.1%}" if b else "N/A"
        pf_str = f"{m['profit_factor']:.2f}" if m['profit_factor'] < 100 else ">99"
        print(
            f"{m['pair']:<14} | {m['cluster']:<22} | {m['total_trades']:<6} | "
            f"{m['cumulative_return']:+7.2%} | {m['win_rate']:7.1%} | {pf_str:<6} | "
            f"{m['max_drawdown']:7.2%} | {b_win:<9} | {m['vetoed_trades']:<6}"
        )

    print("-" * 115)
    # Overall summary metrics
    total_apu_trades = sum(m["total_trades"] for m in apu_results)
    total_vetoes = sum(m["vetoed_trades"] for m in apu_results)
    all_rets = [t["net_return_pct"] for t in all_apu_trades]
    portfolio_win_rate = (np.array(all_rets) > 0).mean() if all_rets else 0.0
    pos_rets = sum(r for r in all_rets if r > 0)
    neg_rets = abs(sum(r for r in all_rets if r < 0))
    portfolio_pf = (pos_rets / neg_rets) if neg_rets > 0 else 999.0

    print(f"PORTFOLIO EQUAL WEIGHT: Return: {eq_equity.iloc[-1] - 1.0:+.2%} | Max DD: {eq_dd.min():.2%} | Sharpe: {eq_sharpe:.2f} | Win Rate: {portfolio_win_rate:.1%} | Profit Factor: {portfolio_pf:.2f}")
    print(f"PORTFOLIO RISK PARITY : Return: {rp_equity.iloc[-1] - 1.0:+.2%} | Max DD: {rp_dd.min():.2%} | Sharpe: {rp_sharpe:.2f} | Total Trades: {total_apu_trades} | Sentiment Vetoes: {total_vetoes}")
    print("=" * 115)

    # 4. Display sample of completed trades
    if all_apu_trades:
        print("\n" + "=" * 115)
        print("SAMPLE APU TRADE EXECUTION AUDIT LOG (Last 20 Completed Round Trips)")
        print("=" * 115)
        print(f"{'#':<3} | {'Pair':<12} | {'Direction':<12} | {'Entry Time':<19} | {'Exit Time':<19} | {'Net Ret':<8} | {'Exit Reason':<18}")
        print("-" * 115)
        for i, t in enumerate(all_apu_trades[-20:], 1):
            p_name = f"{t['ticker1']}:{t['ticker2']}"
            e_t = str(t['entry_time'])[:19]
            x_t = str(t['exit_time'])[:19]
            ret_s = f"{t['net_return_pct']:+.2%}"
            print(f"{i:<3} | {p_name:<12} | {t['direction']:<12} | {e_t:<19} | {x_t:<19} | {ret_s:<8} | {t['exit_reason']:<18}")
        print("=" * 115)

    # 5. Display vetoed trades audit
    if all_vetoed_trades:
        print("\n" + "=" * 115)
        print("DEEPSEEK SENTIMENT CIRCUIT-BREAKER AUDIT (Protected Capital from Structural Breaks)")
        print("=" * 115)
        for i, v in enumerate(all_vetoed_trades[:15], 1):
            p_name = f"{v['ticker1']}:{v['ticker2']}"
            ts_str = str(v['timestamp'])[:19]
            print(f"{i:<2}. [{ts_str}] {p_name:<12} | Dir: {v['direction']:<12} | Z={v['zscore']:+.2f} | Reason: {v['reason']}")
        print("=" * 115)

    # 6. Save JSON output
    output_dir = PROJECT_ROOT / "outputs"
    output_dir.mkdir(exist_ok=True)
    results_payload = {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "timeframe": timeframe,
        "bars": bars,
        "friction_bps": friction_bps,
        "summary": {
            "total_pairs": len(apu_results),
            "total_trades": total_apu_trades,
            "total_vetoed_trades": total_vetoes,
            "overall_win_rate": float(portfolio_win_rate),
            "overall_profit_factor": float(portfolio_pf),
            "equal_weight_return": float(eq_equity.iloc[-1] - 1.0),
            "equal_weight_max_dd": float(eq_dd.min()),
            "equal_weight_sharpe": float(eq_sharpe),
            "risk_parity_return": float(rp_equity.iloc[-1] - 1.0),
            "risk_parity_max_dd": float(rp_dd.min()),
            "risk_parity_sharpe": float(rp_sharpe),
        },
        "pairs": apu_results,
        "baseline_pairs": baseline_results,
        "trades": all_apu_trades,
        "vetoed_trades": all_vetoed_trades,
    }
    out_file = output_dir / "stat_arb_apu_backtest_results.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(results_payload, f, indent=2, default=str)
    print(f"\nSaved full empirical backtest report to {out_file}")

    return results_payload


def main():
    parser = argparse.ArgumentParser(description="Empirically backtest the MIMIR Stat-Arb APU.")
    parser.add_argument("--timeframe", default="H1", help="Timeframe (e.g. H1, M15, M5, D1; default: H1)")
    parser.add_argument("--bars", type=int, default=4000, help="Number of completed bars to load (default: 4000)")
    parser.add_argument("--friction-bps", type=float, default=1.0, help="One-way friction in bps per leg (default: 1.0 bps)")
    args = parser.parse_args()

    run_full_apu_backtest(
        timeframe=args.timeframe,
        bars=args.bars,
        friction_bps=args.friction_bps,
    )


if __name__ == "__main__":
    main()
