"""Reusable MT5 backtest runner for the user-strategy registry.

Examples:
    python scripts/backtest_user_strategies.py XAUUSD --timeframe H1 --strategy volume_exhaustion_reversal
    python scripts/backtest_user_strategies.py BTCUSD --timeframe M15 --all --commission-bps 5
    python scripts/backtest_user_strategies.py XAUUSD --hedge-symbol XAGUSD --strategy pair_stat_arbitrage --commission-bps 5

The runner downloads *closed* OHLCV bars from the local MetaTrader 5 terminal.
It never sends an order or modifies MT5 state.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.analytics.user_strategies.static_strategies import (  # noqa: E402
    BACKTEST_STRATEGY_REGISTRY,
    USER_STRATEGY_REGISTRY,
    PairStatArbitrageStrategy,
)
from backend.app.analytics.user_strategies.indicators import (  # noqa: E402
    OUCalibrator,
    compute_adf_stat,
)
from backend.app.analytics.user_strategies.pair_screener import (  # noqa: E402
    screen_pairs,
    print_screened_pairs_table,
    UNIVERSES,
)


def available_strategies() -> dict:
    """Combine safe backtest strategies with existing static strategies."""
    return {**USER_STRATEGY_REGISTRY, **BACKTEST_STRATEGY_REGISTRY}


def load_mt5_data(
    symbol: str,
    timeframe: str,
    bars: int = 2000,
    start_date: str | None = None,
    end_date: str | None = None,
) -> tuple[pd.DataFrame, str]:
    """Download completed OHLCV bars from MT5.

    Supports two modes:
      - Date range: pass start_date (and optionally end_date) as 'YYYY-MM-DD'.
      - Bar count:  pass bars (default 2000), loads most recent N completed bars.
    """
    import datetime as _dt

    try:
        import MetaTrader5 as mt5
    except ImportError as exc:
        raise RuntimeError("MetaTrader5 is not installed in this Python environment.") from exc

    if mt5.terminal_info() is None and not mt5.initialize():
        raise RuntimeError("Could not connect to the local MetaTrader 5 terminal. Open MT5 and log in first.")

    clean_symbol = symbol.strip().upper()
    candidates = [
        clean_symbol,
        f"{clean_symbol}.US",
        f"#{clean_symbol}",
        clean_symbol.replace(".", ""),
        f"{clean_symbol.replace('.', '')}.US",
    ]
    broker_symbol = next((candidate for candidate in candidates if mt5.symbol_info(candidate)), None)
    if not broker_symbol:
        raise ValueError(f"Could not resolve '{symbol}' in MT5 Market Watch.")
    symbol_info = mt5.symbol_info(broker_symbol)
    if symbol_info is not None and not symbol_info.visible:
        mt5.symbol_select(broker_symbol, True)

    timeframe_map = {
        "M1": mt5.TIMEFRAME_M1,
        "M5": mt5.TIMEFRAME_M5,
        "M15": mt5.TIMEFRAME_M15,
        "M30": mt5.TIMEFRAME_M30,
        "H1": mt5.TIMEFRAME_H1,
        "H4": mt5.TIMEFRAME_H4,
        "D1": mt5.TIMEFRAME_D1,
        "W1": mt5.TIMEFRAME_W1,
    }
    frame_name = timeframe.upper()
    if frame_name not in timeframe_map:
        raise ValueError(f"Unsupported timeframe '{timeframe}'. Use one of: {', '.join(timeframe_map)}.")

    if start_date:
        d_from = pd.to_datetime(start_date, utc=True).to_pydatetime()
        d_to = (
            pd.to_datetime(end_date, utc=True).to_pydatetime()
            if end_date
            else _dt.datetime.now(_dt.timezone.utc)
        )
        rates = mt5.copy_rates_range(broker_symbol, timeframe_map[frame_name], d_from, d_to)
    else:
        # Position 0 is the unfinished candle. Start at 1 to avoid repainting.
        rates = mt5.copy_rates_from_pos(broker_symbol, timeframe_map[frame_name], 1, bars)

    if rates is None or len(rates) == 0:
        raise RuntimeError(f"MT5 returned no {frame_name} history for {broker_symbol}: {mt5.last_error()}")

    data = pd.DataFrame(rates).rename(columns={"tick_volume": "volume", "time": "timestamp"})
    data["timestamp"] = pd.to_datetime(data["timestamp"], unit="s", utc=True)
    required = ["timestamp", "open", "high", "low", "close", "volume"]
    return data[required].copy().reset_index(drop=True), broker_symbol


def evaluate(signals: pd.Series, data: pd.DataFrame, commission_bps: float, periods_per_year: int) -> dict:
    positions = signals.fillna(0.0).clip(-1.0, 1.0)
    market_returns = data["close"].pct_change().fillna(0.0)
    turnover = positions.diff().abs().fillna(positions.abs())
    costs = turnover * commission_bps / 10_000
    strategy_returns = positions.shift(1).fillna(0.0) * market_returns - costs
    equity = (1.0 + strategy_returns).cumprod()
    drawdown = equity.div(equity.cummax()).sub(1.0)
    volatility = strategy_returns.std()
    sharpe = (
        strategy_returns.mean() / volatility * np.sqrt(periods_per_year)
        if volatility and not pd.isna(volatility)
        else 0.0
    )
    return {
        "return": equity.iloc[-1] - 1.0,
        "max_drawdown": drawdown.min(),
        "sharpe": sharpe,
        "trades": int((turnover > 0).sum()),
        "exposure": float((positions != 0).mean()),
    }


def evaluate_pair(positions: pd.DataFrame, data: pd.DataFrame, commission_bps: float, periods_per_year: int) -> tuple[dict, pd.Series]:
    """Value both pair legs, including turnover and costs on each leg."""
    primary = positions["primary_position"].fillna(0.0)
    hedge = positions["hedge_position"].fillna(0.0)
    primary_returns = data["close"].pct_change().fillna(0.0)
    hedge_returns = data["hedge_close"].pct_change().fillna(0.0)
    turnover = primary.diff().abs().fillna(primary.abs()) + hedge.diff().abs().fillna(hedge.abs())
    returns = (
        primary.shift(1).fillna(0.0) * primary_returns
        + hedge.shift(1).fillna(0.0) * hedge_returns
        - turnover * commission_bps / 10_000
    )
    equity = (1.0 + returns).cumprod()
    drawdown = equity.div(equity.cummax()).sub(1.0)
    volatility = returns.std()
    sharpe = returns.mean() / volatility * np.sqrt(periods_per_year) if volatility and not pd.isna(volatility) else 0.0
    return {
        "return": equity.iloc[-1] - 1.0,
        "max_drawdown": drawdown.min(),
        "sharpe": sharpe,
        "trades": int((turnover > 0).sum()),
        "exposure": float(((primary != 0) | (hedge != 0)).mean()),
    }, equity


def evaluate_buy_and_hold(data: pd.DataFrame, periods_per_year: int, close_column: str = "close") -> tuple[dict, pd.Series]:
    """Unlevered benchmark, before execution costs."""
    returns = data[close_column].pct_change().fillna(0.0)
    equity = (1.0 + returns).cumprod()
    drawdown = equity.div(equity.cummax()).sub(1.0)
    volatility = returns.std()
    sharpe = returns.mean() / volatility * np.sqrt(periods_per_year) if volatility and not pd.isna(volatility) else 0.0
    return {
        "return": equity.iloc[-1] - 1.0,
        "max_drawdown": drawdown.min(),
        "sharpe": sharpe,
        "trades": 1,
        "exposure": 1.0,
    }, equity


TIMEFRAME_MINUTES = {
    "M1": 1,
    "M5": 5,
    "M15": 15,
    "M30": 30,
    "H1": 60,
    "H4": 240,
    "D1": 1440,
    "W1": 10080,
}

DEFAULT_5_PAIRS = [
    ("XAUUSD", "XAGUSD", "Gold / Silver (Precious Metals)"),
    ("EURUSD", "GBPUSD", "Euro / British Pound (Major FX)"),
    ("AUDUSD", "NZDUSD", "Aussie / Kiwi Dollar (Commodity FX)"),
    ("XOM", "CVX", "ExxonMobil / Chevron (Energy Equities)"),
    ("US500", "US30", "S&P 500 / Dow Jones 30 (US Indices)"),
]


def align_pair_data(primary: pd.DataFrame, hedge: pd.DataFrame) -> pd.DataFrame:
    """Keep only bars closed for both instruments at the same timestamp."""
    return primary.merge(
        hedge[["timestamp", "open", "high", "low", "close", "volume"]].rename(
            columns={
                "open": "hedge_open",
                "high": "hedge_high",
                "low": "hedge_low",
                "close": "hedge_close",
                "volume": "hedge_volume",
            }
        ),
        on="timestamp",
        how="inner",
        validate="one_to_one",
    ).sort_values("timestamp").reset_index(drop=True)


def compute_macro_pair_features(
    primary_macro: pd.DataFrame,
    hedge_macro: pd.DataFrame,
    macro_lookback: int = 60,
) -> pd.DataFrame:
    """Compute rolling OLS beta, alpha, spread mean, spread std, and correlation on macro timeframe."""
    macro = primary_macro.merge(
        hedge_macro[["timestamp", "close"]].rename(columns={"close": "hedge_close"}),
        on="timestamp",
        how="inner",
    ).sort_values("timestamp").reset_index(drop=True)

    p_log = np.log(macro["close"].astype(float))
    h_log = np.log(macro["hedge_close"].astype(float))

    h_var = h_log.rolling(macro_lookback, min_periods=macro_lookback).var()
    beta = p_log.rolling(macro_lookback, min_periods=macro_lookback).cov(h_log).div(h_var)
    alpha = p_log.rolling(macro_lookback, min_periods=macro_lookback).mean() - beta * h_log.rolling(macro_lookback, min_periods=macro_lookback).mean()
    spread = p_log - alpha - beta * h_log

    macro["macro_beta"] = beta
    macro["macro_alpha"] = alpha
    macro["macro_spread_mean"] = spread.rolling(macro_lookback, min_periods=macro_lookback).mean()
    macro["macro_spread_std"] = spread.rolling(macro_lookback, min_periods=macro_lookback).std().replace(0, np.nan)
    macro["macro_correlation"] = p_log.rolling(macro_lookback, min_periods=macro_lookback).corr(h_log)

    # Rolling OU half-life and ADF stationarity t-statistic
    half_lives = np.full(len(macro), np.nan)
    adf_stats = np.full(len(macro), np.nan)
    spread_vals = spread.values

    for i in range(macro_lookback, len(macro)):
        win = spread_vals[i - macro_lookback : i + 1]
        half_lives[i] = OUCalibrator.half_life(win)
        adf_stats[i] = compute_adf_stat(win)

    macro["macro_half_life"] = half_lives
    macro["macro_adf_stat"] = adf_stats

    return macro[[
        "timestamp",
        "macro_beta",
        "macro_alpha",
        "macro_spread_mean",
        "macro_spread_std",
        "macro_correlation",
        "macro_half_life",
        "macro_adf_stat",
    ]].dropna(subset=["macro_beta", "macro_spread_std"]).reset_index(drop=True)


def align_multi_timeframe_pair(
    primary_exec: pd.DataFrame,
    hedge_exec: pd.DataFrame,
    primary_macro: pd.DataFrame | None = None,
    hedge_macro: pd.DataFrame | None = None,
    macro_lookback: int = 60,
) -> pd.DataFrame:
    """Align lower-timeframe execution bars with higher-timeframe macro features using merge_asof (backward direction)."""
    exec_df = align_pair_data(primary_exec, hedge_exec)
    if primary_macro is None or hedge_macro is None:
        return exec_df

    macro_features = compute_macro_pair_features(primary_macro, hedge_macro, macro_lookback=macro_lookback)
    aligned = pd.merge_asof(
        exec_df.sort_values("timestamp"),
        macro_features.sort_values("timestamp"),
        on="timestamp",
        direction="backward",
    )
    return aligned.dropna(subset=["macro_beta", "macro_spread_std"]).reset_index(drop=True)


def build_single_execution_ledger(signals: pd.Series, data: pd.DataFrame) -> pd.DataFrame:
    """Return simulated close-of-bar orders for a single-asset strategy."""
    positions = signals.fillna(0.0).clip(-1.0, 1.0)
    delta = positions.diff().fillna(positions)
    changed = delta.abs() > 1e-12
    rows = []

    for i in np.flatnonzero(changed.to_numpy()):
        before = float(positions.iloc[i] - delta.iloc[i])
        after = float(positions.iloc[i])
        if before == 0.0 and after > 0:
            action = "OPEN_LONG"
        elif before == 0.0 and after < 0:
            action = "OPEN_SHORT"
        elif before > 0 and after == 0.0:
            action = "CLOSE_LONG"
        elif before < 0 and after == 0.0:
            action = "CLOSE_SHORT"
        elif before < 0 and after > 0:
            action = "REVERSE_TO_LONG"
        elif before > 0 and after < 0:
            action = "REVERSE_TO_SHORT"
        else:
            action = "REBALANCE"

        rows.append({
            "execution_timestamp": data["timestamp"].iloc[i] if "timestamp" in data else data.index[i],
            "action": action,
            "price": float(data["close"].iloc[i]),
            "order_delta": float(delta.iloc[i]),
            "target_position": after,
            "reason": action,
        })
    return pd.DataFrame(rows)


def extract_single_round_trips(ledger: pd.DataFrame, commission_bps: float = 0.0) -> pd.DataFrame:
    """Group sequential entries and exits into round-trip trades with accounting metrics."""
    if ledger.empty:
        return pd.DataFrame()

    trades = []
    trade_id = 0
    in_trade = False
    direction = ""
    entry_time = None
    entry_price = 0.0
    entry_idx = 0

    for i, row in ledger.iterrows():
        action = row["action"]
        px = row["price"]
        ts = row["execution_timestamp"]

        if action in ["OPEN_LONG", "OPEN_SHORT"]:
            trade_id += 1
            in_trade = True
            direction = "LONG" if action == "OPEN_LONG" else "SHORT"
            entry_time = ts
            entry_price = px
            entry_idx = i

        elif action in ["CLOSE_LONG", "CLOSE_SHORT"] and in_trade:
            ret = (px - entry_price) / entry_price if direction == "LONG" else (entry_price - px) / entry_price
            net_ret = ret - (2 * commission_bps / 10_000)
            trades.append({
                "trade_id": trade_id,
                "direction": direction,
                "entry_time": entry_time,
                "exit_time": ts,
                "entry_price": entry_price,
                "exit_price": px,
                "return_pct": ret,
                "net_return_pct": net_ret,
                "exit_reason": action,
            })
            in_trade = False

        elif action in ["REVERSE_TO_LONG", "REVERSE_TO_SHORT"] and in_trade:
            ret = (px - entry_price) / entry_price if direction == "LONG" else (entry_price - px) / entry_price
            net_ret = ret - (2 * commission_bps / 10_000)
            trades.append({
                "trade_id": trade_id,
                "direction": direction,
                "entry_time": entry_time,
                "exit_time": ts,
                "entry_price": entry_price,
                "exit_price": px,
                "return_pct": ret,
                "net_return_pct": net_ret,
                "exit_reason": "REVERSAL",
            })
            # Start reverse trade
            trade_id += 1
            direction = "LONG" if action == "REVERSE_TO_LONG" else "SHORT"
            entry_time = ts
            entry_price = px
            in_trade = True

    return pd.DataFrame(trades)


def build_pair_execution_ledger(positions: pd.DataFrame, data: pd.DataFrame) -> pd.DataFrame:
    """Return every simulated close-of-bar order needed to reach the target pair position."""
    primary_target = positions["primary_position"].fillna(0.0)
    hedge_target = positions["hedge_position"].fillna(0.0)
    primary_delta = primary_target.diff().fillna(primary_target)
    hedge_delta = hedge_target.diff().fillna(hedge_target)
    changed = (primary_delta.abs() > 1e-12) | (hedge_delta.abs() > 1e-12)
    rows = []

    reasons = positions["signal_reason"] if "signal_reason" in positions else pd.Series("", index=positions.index)
    active_hrs = (
        positions["active_hedge_ratio"]
        if "active_hedge_ratio" in positions
        else positions["hedge_ratio"]
    )

    for i in np.flatnonzero(changed.to_numpy()):
        before_primary = float(primary_target.iloc[i] - primary_delta.iloc[i])
        after_primary = float(primary_target.iloc[i])
        if before_primary == 0.0 and after_primary > 0:
            action = "ENTER_LONG_SPREAD"
        elif before_primary == 0.0 and after_primary < 0:
            action = "ENTER_SHORT_SPREAD"
        elif before_primary > 0 and after_primary == 0.0:
            action = "EXIT_LONG_SPREAD"
        elif before_primary < 0 and after_primary == 0.0:
            action = "EXIT_SHORT_SPREAD"
        elif abs(after_primary) > abs(before_primary):
            action = "SCALE_IN_SPREAD"
        elif abs(after_primary) < abs(before_primary) and after_primary != 0.0:
            action = "SCALE_OUT_SPREAD"
        elif abs(primary_delta.iloc[i]) <= 1e-12:
            action = "HEDGE_REBALANCE"
        else:
            action = "POSITION_REBALANCE"

        reason = str(reasons.iloc[i]) if pd.notna(reasons.iloc[i]) else ""
        hr_val = float(active_hrs.iloc[i]) if pd.notna(active_hrs.iloc[i]) else float(positions["hedge_ratio"].iloc[i])

        row = {
            "execution_timestamp": data["timestamp"].iloc[i] if "timestamp" in data else data.index[i],
            "action": action,
            "primary_price": float(data["close"].iloc[i]),
            "hedge_price": float(data["hedge_close"].iloc[i]),
            "primary_order_delta": float(primary_delta.iloc[i]),
            "hedge_order_delta": float(hedge_delta.iloc[i]),
            "primary_target_position": after_primary,
            "hedge_target_position": float(hedge_target.iloc[i]),
            "hedge_ratio": hr_val,
            "spread_zscore": float(positions["spread_zscore"].iloc[i]),
            "rolling_correlation": float(positions["rolling_correlation"].iloc[i]),
            "relative_orderflow_proxy": float(positions["relative_orderflow_proxy"].iloc[i]) if "relative_orderflow_proxy" in positions else 0.0,
            "reason": reason if reason else action,
        }
        rows.append(row)
    return pd.DataFrame(rows)


def extract_pair_round_trips(ledger: pd.DataFrame, commission_bps: float = 0.0) -> pd.DataFrame:
    """Group pair enter and exit orders into round-trip trades with return breakdown."""
    if ledger.empty:
        return pd.DataFrame()

    trades = []
    trade_id = 0
    in_trade = False
    direction = ""
    entry_time = None
    entry_p_px = 0.0
    entry_h_px = 0.0
    hr = 1.0

    for _, row in ledger.iterrows():
        action = row["action"]
        ts = row["execution_timestamp"]
        p_px = row["primary_price"]
        h_px = row["hedge_price"]

        if action in ["ENTER_LONG_SPREAD", "ENTER_SHORT_SPREAD"]:
            trade_id += 1
            in_trade = True
            direction = "LONG_SPREAD" if action == "ENTER_LONG_SPREAD" else "SHORT_SPREAD"
            entry_time = ts
            entry_p_px = p_px
            entry_h_px = h_px
            hr = row["hedge_ratio"]

        elif action in ["EXIT_LONG_SPREAD", "EXIT_SHORT_SPREAD"] and in_trade:
            # Primary leg return
            p_ret = (p_px - entry_p_px) / entry_p_px if direction == "LONG_SPREAD" else (entry_p_px - p_px) / entry_p_px
            # Hedge leg return
            h_ret = (entry_h_px - h_px) / entry_h_px if direction == "LONG_SPREAD" else (h_px - entry_h_px) / entry_h_px
            # Combined return (equal dollar weighting on entry)
            tot_ret = 0.5 * p_ret + 0.5 * h_ret
            net_ret = tot_ret - (4 * commission_bps / 10_000)

            trades.append({
                "trade_id": trade_id,
                "direction": direction,
                "entry_time": entry_time,
                "exit_time": ts,
                "entry_primary": entry_p_px,
                "exit_primary": p_px,
                "entry_hedge": entry_h_px,
                "exit_hedge": h_px,
                "hedge_ratio": hr,
                "primary_ret": p_ret,
                "hedge_ret": h_ret,
                "net_return_pct": net_ret,
                "exit_reason": row.get("reason", action),
            })
            in_trade = False

    return pd.DataFrame(trades)


def print_trade_audit_table(strategy_name: str, round_trips: pd.DataFrame, max_display: int = 25) -> None:
    """Print a clean, structured trade log and win/loss statistics to stdout."""
    print(f"\n{'=' * 95}")
    print(f"TRADE EXECUTION AUDIT LOG: {strategy_name.upper()}")
    print(f"{'=' * 95}")

    if round_trips.empty:
        print("  No completed round-trip trades during this test window.")
        print(f"{'=' * 95}\n")
        return

    n_trades = len(round_trips)
    wins = round_trips["net_return_pct"] > 0
    win_rate = wins.mean()
    pos_ret = round_trips.loc[wins, "net_return_pct"]
    neg_ret = round_trips.loc[~wins, "net_return_pct"]
    profit_factor = (pos_ret.sum() / abs(neg_ret.sum())) if neg_ret.sum() != 0 else float("inf")
    avg_ret = round_trips["net_return_pct"].mean()

    display_df = round_trips.tail(max_display).copy()

    # Format for display
    print(f"{'#':<3} | {'Direction':<12} | {'Entry Timestamp':<20} | {'Exit Timestamp':<20} | {'Net Ret':<8} | {'Exit Reason':<18}")
    print(f"{'-' * 95}")

    for _, tr in display_df.iterrows():
        tid = tr["trade_id"]
        direct = tr["direction"]
        e_time = str(tr["entry_time"])[:19]
        x_time = str(tr["exit_time"])[:19]
        ret_str = f"{tr['net_return_pct']:+.2%}"
        reason = str(tr["exit_reason"])
        print(f"{tid:<3} | {direct:<12} | {e_time:<20} | {x_time:<20} | {ret_str:<8} | {reason:<18}")

    if n_trades > max_display:
        print(f"  ... [Showing last {max_display} of {n_trades} trades]")

    print(f"{'-' * 95}")
    print(f"Summary: {n_trades} trades | Win Rate: {win_rate:.1%} | Profit Factor: {profit_factor:.2f} | Avg Return: {avg_ret:+.2%}")
    print(f"{'=' * 95}\n")


def run_single_pair(
    p_symbol: str,
    h_symbol: str,
    pair_label: str,
    args: argparse.Namespace,
    periods_per_year: int,
) -> tuple[dict, pd.Series, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Run multi-timeframe pair strategy on a single asset pair."""
    exec_tf = args.timeframe.upper()
    macro_tf = args.macro_timeframe.upper()
    exec_mins = TIMEFRAME_MINUTES.get(exec_tf, 5)
    macro_mins = TIMEFRAME_MINUTES.get(macro_tf, 60)

    sd = getattr(args, "start_date", None)
    ed = getattr(args, "end_date", None)

    # 1. Load execution bars
    p_exec, p_broker = load_mt5_data(p_symbol, exec_tf, args.bars, start_date=sd, end_date=ed)
    h_exec, h_broker = load_mt5_data(h_symbol, exec_tf, args.bars, start_date=sd, end_date=ed)

    # 2. Multi-timeframe macro data loading & backward alignment
    if macro_tf != exec_tf:
        macro_bars = int(args.bars * (exec_mins / macro_mins)) + args.macro_lookback + 100
        p_macro, _ = load_mt5_data(p_symbol, macro_tf, macro_bars, start_date=sd, end_date=ed)
        h_macro, _ = load_mt5_data(h_symbol, macro_tf, macro_bars, start_date=sd, end_date=ed)
        aligned = align_multi_timeframe_pair(
            p_exec, h_exec, p_macro, h_macro, macro_lookback=args.macro_lookback
        )
    else:
        aligned = align_pair_data(p_exec, h_exec)

    if len(aligned) < 50:
        raise RuntimeError(f"Fewer than 50 synchronized bars available for {p_broker}/{h_broker}.")

    # 3. Strategy execution
    # On M5 (5 min), default orderflow is 12 bars (1 hr), max holding 144 bars (12 hrs)
    strat_kwargs = {
        "rebalance_on_drift": args.rebalance_drift,
        "max_adf_stat": getattr(args, "max_adf_stat", -2.2),
        "scale_in": not getattr(args, "no_scale_in", False),
        "estimator": getattr(args, "estimator", "ols"),
        "use_dynamic_half_life": True,
    }
    if exec_mins <= 5:
        strat_kwargs["orderflow_lookback"] = 12
        strat_kwargs["max_holding_bars"] = 144
    elif exec_mins <= 15:
        strat_kwargs["orderflow_lookback"] = 16
        strat_kwargs["max_holding_bars"] = 96

    total_friction_bps = args.commission_bps + getattr(args, "bid_ask_spread_bps", 0.0)

    strategy = PairStatArbitrageStrategy(**strat_kwargs)
    positions = strategy.generate_pair_positions(aligned)
    metrics, equity = evaluate_pair(positions, aligned, total_friction_bps, periods_per_year)
    ledger = build_pair_execution_ledger(positions, aligned)
    round_trips = extract_pair_round_trips(ledger, total_friction_bps)

    # Tag pair label
    metrics["pair"] = pair_label
    metrics["round_trip_trades"] = len(round_trips)
    metrics["win_rate"] = (round_trips["net_return_pct"] > 0).mean() if not round_trips.empty else 0.0

    return metrics, equity, ledger, round_trips, aligned


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest registered MIMIR user strategies with closed MT5 bars.")
    parser.add_argument("symbol", nargs="?", default=None, help="Primary symbol in MT5 Market Watch (or omit if using --pairs)")
    parser.add_argument("--hedge-symbol", help="Second MT5 symbol; required for pair_stat_arbitrage when testing a single pair")
    parser.add_argument("--pairs", help="Comma-separated pairs (e.g. 'XAUUSD:XAGUSD,EURUSD:GBPUSD') or '5pairs' / 'default5' for standard 5 institutional pairs")
    parser.add_argument("--benchmark-symbol", help="Optional buy-and-hold benchmark, e.g. SPY for the S&P 500 ETF")
    parser.add_argument("--timeframe", default="M5", help="Execution timeframe: M1, M5, M15, M30, H1, H4, D1 (default: M5)")
    parser.add_argument("--macro-timeframe", default="H1", help="Higher timeframe for macro OLS cointegration estimation (default: H1)")
    parser.add_argument("--macro-lookback", type=int, default=60, help="Macro rolling OLS window size (default: 60 macro bars)")
    parser.add_argument("--bars", type=int, default=2_000, help="Number of completed execution candles (default: 2000 M5 bars)")
    parser.add_argument("--start-date", default=None, help="Start date for backtest range, e.g. 2025-01-01 (overrides --bars)")
    parser.add_argument("--end-date", default=None, help="End date for backtest range, e.g. 2025-06-01 (default: now)")
    parser.add_argument("--strategy", choices=sorted(available_strategies()), help="Strategy to run (default: pair_stat_arbitrage when pair/pairs specified)")
    parser.add_argument("--all", action="store_true", help="Run every registered backtest-capable strategy")
    parser.add_argument("--commission-bps", type=float, default=0.5, help="One-way transaction cost in basis points (default: 0.5 bps)")
    parser.add_argument("--bid-ask-spread-bps", type=float, default=0.5, help="Simulated round-turn bid-ask crossing friction in bps (default: 0.5 bps)")
    parser.add_argument("--estimator", choices=["ols", "kalman"], default="ols", help="Hedge ratio estimator (ols or kalman, default: ols)")
    parser.add_argument("--max-adf-stat", type=float, default=-2.2, help="Max ADF t-stat threshold to confirm stationarity (default: -2.2)")
    parser.add_argument("--no-scale-in", action="store_true", help="Disable 2-stage scale-in and use single 1.0 unit sizing")
    parser.add_argument("--periods-per-year", type=int, default=None, help="Periods per year for Sharpe (auto-detected from timeframe if omitted)")
    parser.add_argument("--rebalance-drift", type=float, default=0.0, help="Tolerance for pair hedge rebalancing (default 0.0 = lock on entry)")
    parser.add_argument("--no-trades-table", action="store_true", help="Suppress console trade audit tables")
    parser.add_argument("--max-trades", type=int, default=15, help="Max trade entries to display in console table per pair (default: 15)")
    parser.add_argument("--output", type=Path, help="Optional CSV path for equity curves")
    parser.add_argument("--trades-output", type=Path, help="Optional CSV path for execution orders ledger")
    args = parser.parse_args()

    # ------------------------------------------------------------------
    # ONE-LINE INPUT PROMPT
    # When the user runs the script with no arguments at all, prompt them
    # for a single line of input.  Tokens are parsed flexibly:
    #
    #   <primary> [<hedge>] [<timeframe>] [<bars> OR <start_date> <end_date>]
    #   screen <universe>          (run pair screener)
    #
    # Examples the user can type:
    #   XAUUSD XAGUSD M5 2025-01-01 2025-06-01
    #   XAUUSD 5M 3000
    #   screen nasdaq
    #   default5                  (runs the built-in 5-pair portfolio)
    #   (just press Enter)        (also runs default5)
    # ------------------------------------------------------------------
    import re as _re

    TF_SHORTHAND = {
        "1M": "M1", "M1": "M1", "1MIN": "M1", "1": "M1",
        "5M": "M5", "M5": "M5", "5MIN": "M5", "5": "M5",
        "15M": "M15", "M15": "M15", "15MIN": "M15", "15": "M15",
        "30M": "M30", "M30": "M30", "30MIN": "M30", "30": "M30",
        "1H": "H1", "H1": "H1", "60M": "H1", "60": "H1", "1HR": "H1",
        "4H": "H4", "H4": "H4", "4HR": "H4",
        "5H": "H4", "5HR": "H4",
        "1D": "D1", "D1": "D1", "D": "D1", "DAILY": "D1",
        "1W": "W1", "W1": "W1", "W": "W1", "WEEKLY": "W1",
    }
    VALID_TF = set(TIMEFRAME_MINUTES.keys()) | set(TF_SHORTHAND.keys())

    DEFAULT_HEDGES = {
        "XAUUSD": "XAGUSD",
        "GOLD": "XAGUSD",
        "XAGUSD": "XAUUSD",
        "EURUSD": "GBPUSD",
        "GBPUSD": "EURUSD",
        "AUDUSD": "NZDUSD",
        "NZDUSD": "AUDUSD",
        "US500": "US30",
        "US30": "US500",
        "BTCUSD": "ETHUSD",
        "ETHUSD": "BTCUSD",
        "XOM": "CVX",
        "CVX": "XOM",
    }

    _DATE_RE = _re.compile(r"^\d{4}[-/.]\d{1,2}[-/.]\d{1,2}$")

    def _looks_like_timeframe(token: str) -> bool:
        return token.upper() in VALID_TF

    def _looks_like_date(token: str) -> bool:
        return bool(_DATE_RE.match(token))

    if not args.symbol and not args.pairs:
        universes_str = ", ".join(UNIVERSES.keys())
        print("\n+--------------------------------------------------------------+")
        print("|           MIMIR BACKTEST RUNNER - Quick Setup                |")
        print("+--------------------------------------------------------------+")
        print("|  Type your parameters on ONE line, separated by spaces:     |")
        print("|                                                              |")
        print("|    <primary> [hedge] [timeframe] [bars or dates]            |")
        print("|                                                              |")
        print("|  Examples:                                                   |")
        print("|    XAUUSD XAGUSD M5 2025-01-01 2025-06-01                   |")
        print("|    XAUUSD M5 3000            (auto-pairs with XAGUSD)       |")
        print("|    EURUSD GBPUSD H1                                         |")
        print("|    default5                  (run 5 built-in pairs)         |")
        print("|    screen nasdaq             (find best correlated pairs)   |")
        print(f"|    screen <universe>  choices: {universes_str:<22}|")
        print("|    (press Enter)             (same as default5)             |")
        print("+--------------------------------------------------------------+")
        line = input("\n>>> ").strip()

        if not line:
            args.pairs = "default5"
            print("[INFO] No input; running default 5-pair portfolio.")
        else:
            parts = line.split()
            first = parts[0].upper()

            # ---------- screen command ----------
            if first == "SCREEN":
                universe_key = parts[1].lower() if len(parts) > 1 else "all"
                if universe_key not in UNIVERSES:
                    print(f"[WARNING] Unknown universe '{universe_key}'. Using 'all'.")
                    universe_key = "all"

                # Optional: timeframe + date range for screening
                screen_tf = "H1"
                screen_bars = 500
                screen_start = None
                screen_end = None
                idx = 2
                while idx < len(parts):
                    token = parts[idx].upper()
                    if _looks_like_timeframe(token):
                        screen_tf = TF_SHORTHAND.get(token, token)
                    elif _looks_like_date(parts[idx]):
                        if screen_start is None:
                            screen_start = parts[idx]
                        else:
                            screen_end = parts[idx]
                    else:
                        try:
                            screen_bars = int(parts[idx])
                        except ValueError:
                            pass
                    idx += 1

                print(f"\n[SCREEN] Scanning universe '{universe_key}' on {screen_tf}...")
                screened = screen_pairs(
                    universe_key,
                    timeframe=screen_tf,
                    bars=screen_bars,
                    start_date=screen_start,
                    end_date=screen_end,
                    top_n=10,
                )
                print_screened_pairs_table(screened)

                # Ask user: backtest top N pairs?
                try:
                    pick = input("\nHow many top pairs to backtest? (0 to quit, Enter for top 3): ").strip()
                    n_pick = int(pick) if pick else 3
                except (ValueError, EOFError):
                    n_pick = 3

                if n_pick <= 0 or screened.empty:
                    print("[INFO] Exiting.")
                    return
                top = screened.head(n_pick)
                pair_specs = []
                for _, row in top.iterrows():
                    p1, p2 = row["primary"], row["hedge"]
                    pair_specs.append(f"{p1}:{p2}")
                args.pairs = ",".join(pair_specs)
                # Carry over the screening timeframe/dates to the backtest
                args.timeframe = screen_tf
                if screen_start:
                    args.start_date = screen_start
                    args.end_date = screen_end
                print(f"[CONFIG] Backtesting {n_pick} screened pairs: {args.pairs}")

            # ---------- default/all keywords ----------
            elif first in ("DEFAULT5", "5PAIRS", "DEFAULT", "ALL"):
                args.pairs = first.lower()

            # ---------- normal symbol input ----------
            else:
                args.symbol = first
                idx = 1

                # Second token: hedge symbol or timeframe?
                if idx < len(parts):
                    token = parts[idx].upper()
                    if _looks_like_timeframe(token):
                        args.timeframe = TF_SHORTHAND.get(token, token)
                        idx += 1
                    elif _looks_like_date(parts[idx]):
                        pass  # handled below
                    else:
                        args.hedge_symbol = token
                        idx += 1

                # Third token: timeframe (if not already set from token 2)
                if idx < len(parts):
                    token = parts[idx].upper()
                    if _looks_like_timeframe(token):
                        args.timeframe = TF_SHORTHAND.get(token, token)
                        idx += 1

                # Remaining tokens: bars count OR date range
                dates_found = []
                while idx < len(parts):
                    if _looks_like_date(parts[idx]):
                        dates_found.append(parts[idx])
                    else:
                        try:
                            args.bars = int(parts[idx])
                        except ValueError:
                            pass
                    idx += 1

                if len(dates_found) >= 1:
                    args.start_date = dates_found[0]
                if len(dates_found) >= 2:
                    args.end_date = dates_found[1]

        # If primary symbol provided without a hedge, auto-pair using known institutional pairs
        if args.symbol and not args.hedge_symbol and not args.strategy:
            auto_hedge = DEFAULT_HEDGES.get(args.symbol)
            if auto_hedge:
                args.hedge_symbol = auto_hedge
                print(f"[INFO] Auto-pairing {args.symbol} with hedge symbol {args.hedge_symbol}")

        # Print a confirmation of what was parsed
        if args.pairs:
            print(f"[CONFIG] Mode: multi-pair ({args.pairs})")
        elif args.symbol:
            hedge_str = args.hedge_symbol or "(none)"
            date_str = ""
            if args.start_date:
                date_str = f" | Date: {args.start_date} to {args.end_date or 'now'}"
            else:
                date_str = f" | Bars: {args.bars}"
            print(f"[CONFIG] Primary: {args.symbol} | Hedge: {hedge_str} | TF: {args.timeframe}{date_str}")

    # If still nothing supplied, fall back to the built-in default portfolio
    if not args.symbol and not args.pairs:
        args.pairs = "default5"
        print("[INFO] No symbol or pairs supplied; defaulting to multi-pair backtest using 'default5'.")


    exec_tf = args.timeframe.upper()
    exec_mins = TIMEFRAME_MINUTES.get(exec_tf, 5)
    periods_per_year = args.periods_per_year or int((252 * 24 * 60) / exec_mins)

    if args.bars < 50:
        parser.error("--bars must be at least 50")

    # =========================================================================
    # MULTI-PAIR PORTFOLIO MODE
    # =========================================================================
    if args.pairs:
        raw_pairs = args.pairs.strip().lower()
        if raw_pairs in ["default5", "5pairs", "5", "default", "all"]:
            pair_list = [(p1, p2, desc) for p1, p2, desc in DEFAULT_5_PAIRS]
        else:
            pair_list = []
            for item in args.pairs.split(","):
                parts = item.strip().split(":")
                if len(parts) == 2:
                    p1, p2 = parts[0].strip().upper(), parts[1].strip().upper()
                    pair_list.append((p1, p2, f"{p1} / {p2}"))
                else:
                    parser.error(f"Invalid pair format '{item}'. Use 'SYMBOL1:SYMBOL2'")

        print("\n" + "=" * 105)
        print(f"MULTI-PAIR STATISTICAL ARBITRAGE PORTFOLIO ({len(pair_list)} Pairs)")
        date_info = ""
        if args.start_date:
            date_info = f" | Range: {args.start_date} to {args.end_date or 'now'}"
        else:
            date_info = f" | Bars: {args.bars}"
        print(f"Execution Timeframe: {exec_tf} (Every {exec_mins} min){date_info} | Macro: {args.macro_timeframe.upper()} (Lookback: {args.macro_lookback})")
        print("=" * 105)

        pair_results = []
        equity_dict: dict[str, pd.Series] = {}
        all_ledgers: list[pd.DataFrame] = []
        round_trips_dict: dict[str, pd.DataFrame] = {}

        for p_sym, h_sym, desc in pair_list:
            print(f"-> Processing {desc} ({p_sym} vs {h_sym})...")
            try:
                metrics, equity, ledger, round_trips, aligned = run_single_pair(
                    p_sym, h_sym, desc, args, periods_per_year
                )
                pair_results.append(metrics)
                equity_dict[desc] = equity
                round_trips_dict[desc] = round_trips
                if not ledger.empty:
                    all_ledgers.append(ledger.assign(pair=desc))
            except Exception as exc:
                print(f"   [ERROR] Failed to run {desc}: {exc}")

        if not pair_results:
            print("[ERROR] No pairs completed successfully.")
            return

        # Compute Diversified Combined Portfolio Performance
        equity_df = pd.DataFrame(equity_dict)
        pair_rets = equity_df.pct_change().fillna(0.0)

        # 1. Equal-Weight Portfolio
        eq_returns = pair_rets.mean(axis=1)
        eq_equity = (1.0 + eq_returns).cumprod()
        eq_dd = eq_equity.div(eq_equity.cummax()).sub(1.0)
        eq_vol = eq_returns.std()
        eq_sharpe = (
            eq_returns.mean() / eq_vol * np.sqrt(periods_per_year)
            if eq_vol and not pd.isna(eq_vol)
            else 0.0
        )

        # 2. Risk-Parity (Inverse Volatility) Portfolio
        vols = pair_rets.std()
        inv_vols = 1.0 / vols.replace(0, np.nan)
        inv_vols = inv_vols.fillna(0.0)
        if inv_vols.sum() > 0:
            rp_weights = inv_vols / inv_vols.sum()
        else:
            rp_weights = pd.Series(1.0 / len(pair_rets.columns), index=pair_rets.columns)

        rp_returns = (pair_rets * rp_weights).sum(axis=1)
        rp_equity = (1.0 + rp_returns).cumprod()
        rp_dd = rp_equity.div(rp_equity.cummax()).sub(1.0)
        rp_vol = rp_returns.std()
        rp_sharpe = (
            rp_returns.mean() / rp_vol * np.sqrt(periods_per_year)
            if rp_vol and not pd.isna(rp_vol)
            else 0.0
        )

        total_round_trips = sum(len(rt) for rt in round_trips_dict.values())
        all_rt_returns = [r for rt in round_trips_dict.values() if not rt.empty for r in rt["net_return_pct"]]
        overall_win_rate = (np.array(all_rt_returns) > 0).mean() if all_rt_returns else 0.0

        eq_metrics = {
            "pair": f"PORTFOLIO_EQUAL_WEIGHT ({len(pair_results)} Pairs)",
            "return": eq_equity.iloc[-1] - 1.0,
            "max_drawdown": eq_dd.min(),
            "sharpe": eq_sharpe,
            "trades": sum(m["trades"] for m in pair_results),
            "exposure": float(np.mean([m["exposure"] for m in pair_results])),
            "round_trip_trades": total_round_trips,
            "win_rate": overall_win_rate,
        }

        rp_metrics = {
            "pair": f"PORTFOLIO_RISK_PARITY ({len(pair_results)} Pairs)",
            "return": rp_equity.iloc[-1] - 1.0,
            "max_drawdown": rp_dd.min(),
            "sharpe": rp_sharpe,
            "trades": sum(m["trades"] for m in pair_results),
            "exposure": float(np.mean([m["exposure"] for m in pair_results])),
            "round_trip_trades": total_round_trips,
            "win_rate": overall_win_rate,
        }

        # Print Portfolio Performance Table
        report_df = pd.DataFrame(pair_results + [eq_metrics, rp_metrics])
        cols = ["pair", "return", "max_drawdown", "sharpe", "trades", "round_trip_trades", "win_rate", "exposure"]
        display_df = report_df[cols].rename(columns={
            "pair": "Pair / Strategy",
            "return": "Return",
            "max_drawdown": "Max Drawdown",
            "sharpe": "Sharpe",
            "trades": "Orders",
            "round_trip_trades": "Round Trips",
            "win_rate": "Win Rate",
            "exposure": "Exposure",
        })

        print("\n" + "=" * 105)
        print("PORTFOLIO PERFORMANCE BREAKDOWN")
        print("=" * 105)
        formatted_df = display_df.copy()
        formatted_df["Return"] = formatted_df["Return"].apply(lambda v: f"{v:+.2%}")
        formatted_df["Max Drawdown"] = formatted_df["Max Drawdown"].apply(lambda v: f"{v:.2%}")
        formatted_df["Sharpe"] = formatted_df["Sharpe"].apply(lambda v: f"{v:.2f}")
        formatted_df["Win Rate"] = formatted_df["Win Rate"].apply(lambda v: f"{v:.1%}")
        formatted_df["Exposure"] = formatted_df["Exposure"].apply(lambda v: f"{v:.1%}")
        print(formatted_df.to_string(index=False))
        print("-" * 105)
        print("Risk-Parity Allocation Weights (Inverse Volatility):")
        for pair_col, w in rp_weights.items():
            print(f"   * {pair_col:<45}: {w:6.1%}")
        print("=" * 105)

        # Print individual trade audit logs if requested
        if not args.no_trades_table:
            for desc, rt in round_trips_dict.items():
                print_trade_audit_table(desc, rt, max_display=args.max_trades)

        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            equity_df["PORTFOLIO_EQUAL_WEIGHT"] = eq_equity
            equity_df["PORTFOLIO_RISK_PARITY"] = rp_equity
            equity_df.to_csv(args.output, index=False)
            print(f"Saved portfolio equity curves to {args.output}")

        if args.trades_output and all_ledgers:
            args.trades_output.parent.mkdir(parents=True, exist_ok=True)
            combined_ledger = pd.concat(all_ledgers, ignore_index=True)
            combined_ledger.to_csv(args.trades_output, index=False)
            print(f"Saved {len(combined_ledger)} simulated orders to {args.trades_output}")
        return

    # =========================================================================
    # SINGLE-PAIR OR SINGLE-STRATEGY MODE
    # =========================================================================
    if not args.symbol:
        parser.error("Specify 'symbol' or use '--pairs default5' for multi-pair backtesting.")

    strategy_name = args.strategy or ("pair_stat_arbitrage" if args.hedge_symbol else None)
    if not args.all and not strategy_name:
        parser.error("choose --strategy NAME, or provide --hedge-symbol, or use --all, or use --pairs default5")

    data, broker_symbol = load_mt5_data(args.symbol, exec_tf, args.bars, start_date=args.start_date, end_date=args.end_date)
    print(f"Loaded {len(data)} completed {exec_tf} bars for {broker_symbol} from MT5.")
    registry = available_strategies()
    selected = registry if args.all else {strategy_name: registry[strategy_name]}

    if "pair_stat_arbitrage" in selected:
        if not args.hedge_symbol:
            parser.error("--hedge-symbol is required for pair_stat_arbitrage")
        hedge_data, hedge_broker_symbol = load_mt5_data(args.hedge_symbol, exec_tf, args.bars, start_date=args.start_date, end_date=args.end_date)

        # Multi-timeframe backward merge
        macro_tf = args.macro_timeframe.upper()
        if macro_tf != exec_tf:
            macro_mins = TIMEFRAME_MINUTES.get(macro_tf, 60)
            macro_bars = int(args.bars * (exec_mins / macro_mins)) + args.macro_lookback + 100
            p_macro, _ = load_mt5_data(args.symbol, macro_tf, macro_bars, start_date=args.start_date, end_date=args.end_date)
            h_macro, _ = load_mt5_data(args.hedge_symbol, macro_tf, macro_bars, start_date=args.start_date, end_date=args.end_date)
            data = align_multi_timeframe_pair(
                data, hedge_data, p_macro, h_macro, macro_lookback=args.macro_lookback
            )
            print(f"Aligned {len(data)} {exec_tf} execution bars with {macro_tf} macro cointegration parameters.")
        else:
            data = align_pair_data(data, hedge_data)
            print(f"Aligned {len(data)} bars for pair {broker_symbol}/{hedge_broker_symbol}.")

        if len(data) < 50:
            raise RuntimeError("Fewer than 50 synchronized bars are available for the selected pair.")

    benchmark_symbol = broker_symbol
    benchmark_column = "close"
    if args.benchmark_symbol:
        benchmark_data, benchmark_symbol = load_mt5_data(args.benchmark_symbol, exec_tf, args.bars, start_date=args.start_date, end_date=args.end_date)
        data = data.merge(
            benchmark_data[["timestamp", "close"]].rename(columns={"close": "benchmark_close"}),
            on="timestamp",
            how="inner",
            validate="one_to_one",
        ).sort_values("timestamp").reset_index(drop=True)
        benchmark_column = "benchmark_close"
        print(f"Aligned {len(data)} bars against benchmark {benchmark_symbol}.")

    equity_curves = pd.DataFrame(index=data.index)
    results = []
    execution_ledgers: dict[str, pd.DataFrame] = {}
    round_trip_records: dict[str, pd.DataFrame] = {}

    for name, strategy_class in selected.items():
        try:
            if name == "pair_stat_arbitrage":
                strat_kwargs = {"rebalance_on_drift": args.rebalance_drift}
                if exec_mins <= 5:
                    strat_kwargs["orderflow_lookback"] = 12
                    strat_kwargs["max_holding_bars"] = 144
                strategy = strategy_class(**strat_kwargs)
                positions = strategy.generate_pair_positions(data)
                metrics, equity = evaluate_pair(positions, data, args.commission_bps, periods_per_year)
                ledger = build_pair_execution_ledger(positions, data)
                round_trips = extract_pair_round_trips(ledger, args.commission_bps)
            else:
                strategy = strategy_class()
                signals = strategy.generate_signals(data)
                metrics = evaluate(signals, data, args.commission_bps, periods_per_year)
                ledger = build_single_execution_ledger(signals, data)
                round_trips = extract_single_round_trips(ledger, args.commission_bps)
                positions = signals.fillna(0.0).clip(-1.0, 1.0)
                turnover = positions.diff().abs().fillna(positions.abs())
                returns = (
                    positions.shift(1).fillna(0.0) * data["close"].pct_change().fillna(0.0)
                    - turnover * args.commission_bps / 10_000
                )
                equity = (1.0 + returns).cumprod()

            execution_ledgers[name] = ledger
            round_trip_records[name] = round_trips
            metrics["round_trip_trades"] = len(round_trips)
            results.append({"strategy": name, **metrics})
            equity_curves[name] = equity
        except Exception as exc:
            results.append({"strategy": name, "error": str(exc)})

    benchmark_metrics, benchmark_equity = evaluate_buy_and_hold(data, periods_per_year, benchmark_column)
    benchmark_name = f"buy_and_hold_{benchmark_symbol}"
    benchmark_metrics["round_trip_trades"] = 1
    results.append({"strategy": benchmark_name, **benchmark_metrics})
    equity_curves[benchmark_name] = benchmark_equity

    print("\n" + "=" * 95)
    print("BACKTEST OVERVIEW RESULTS")
    print("=" * 95)
    report = pd.DataFrame(results)
    print(report.to_string(index=False, float_format=lambda value: f"{value:.2%}"))

    if not args.no_trades_table:
        for name, rt in round_trip_records.items():
            print_trade_audit_table(name, rt, max_display=args.max_trades)

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        equity_curves.to_csv(args.output, index=False)
        print(f"Saved equity curves to {args.output}")

    if args.trades_output:
        args.trades_output.parent.mkdir(parents=True, exist_ok=True)
        combined_ledger = pd.concat(
            [df.assign(strategy=s_name) for s_name, df in execution_ledgers.items()],
            ignore_index=True,
        )
        combined_ledger.to_csv(args.trades_output, index=False)
        print(f"Saved {len(combined_ledger)} simulated orders to {args.trades_output}")


if __name__ == "__main__":
    main()
