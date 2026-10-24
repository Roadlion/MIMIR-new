"""
MIMIR Quant Strategy Studio & Instant Runner.

Run interactively:
    python scripts/quick_strategy.py
    python -m bong_strats

Or via CLI arguments:
    python scripts/quick_strategy.py --strategy rsi_mean_reversion --symbol XAUUSD --timeframe H1
    python scripts/quick_strategy.py --list
    python scripts/quick_strategy.py --new my_momentum_strategy
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

# Add repo root to path
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import bong_strats
from bong_strats import (
    FULL_STRATEGY_REGISTRY,
    USER_STRATEGY_REGISTRY,
    BACKTEST_STRATEGY_REGISTRY,
    list_strategies,
    reload_registries,
    Strategy,
    QuickStrategy,
)


# =====================================================================
# DATA PROVIDER: MT5 with Yahoo Finance Fallback
# =====================================================================

YAHOO_SYMBOL_MAP = {
    "XAUUSD": "GC=F",
    "XAGUSD": "SI=F",
    "BTCUSD": "BTC-USD",
    "ETHUSD": "ETH-USD",
    "EURUSD": "EURUSD=X",
    "GBPUSD": "GBPUSD=X",
    "USDJPY": "JPY=X",
    "AUDUSD": "AUDUSD=X",
    "US500": "^GSPC",
    "US30": "^DJI",
    "NAS100": "^IXIC",
}

TIMEFRAME_TO_YFINANCE = {
    "M1": "1m",
    "M5": "5m",
    "M15": "15m",
    "M30": "30m",
    "H1": "1h",
    "H4": "1h",    # yfinance does not support 4h directly; use 1h
    "D1": "1d",
    "W1": "1wk",
}


def fetch_historical_bars(symbol: str, timeframe: str = "H1", bars: int = 1000) -> Tuple[pd.DataFrame, str]:
    """
    Attempts to fetch closed OHLCV bars from MetaTrader 5 terminal first.
    If MT5 is offline, closed, or symbol isn't listed, gracefully falls back to Yahoo Finance.
    Returns: (df, source_description)
    """
    clean_symbol = symbol.strip().upper()
    df_mt5 = _try_fetch_mt5(clean_symbol, timeframe, bars)
    if df_mt5 is not None and not df_mt5.empty:
        return df_mt5, "MetaTrader 5 (Local Terminal)"

    # Fallback to Yahoo Finance
    df_yf = _try_fetch_yahoo(clean_symbol, timeframe, bars)
    if df_yf is not None and not df_yf.empty:
        return df_yf, "Yahoo Finance (Online Historical API)"

    raise RuntimeError(
        f"Unable to retrieve data for '{symbol}' from either MT5 or Yahoo Finance. "
        "Please check your symbol spelling or internet connection."
    )


def _try_fetch_mt5(symbol: str, timeframe: str, bars: int) -> Optional[pd.DataFrame]:
    try:
        import MetaTrader5 as mt5
        # Only query if terminal is actively initialized
        if mt5.terminal_info() is None:
            if not mt5.initialize():
                return None

        candidates = [symbol, f"{symbol}.US", f"#{symbol}", symbol.replace(".", "")]
        broker_sym = next((c for c in candidates if mt5.symbol_info(c)), None)
        if not broker_sym:
            return None

        info = mt5.symbol_info(broker_sym)
        if info and not info.visible:
            mt5.symbol_select(broker_sym, True)

        tf_map = {
            "M1": mt5.TIMEFRAME_M1,
            "M5": mt5.TIMEFRAME_M5,
            "M15": mt5.TIMEFRAME_M15,
            "M30": mt5.TIMEFRAME_M30,
            "H1": mt5.TIMEFRAME_H1,
            "H4": mt5.TIMEFRAME_H4,
            "D1": mt5.TIMEFRAME_D1,
            "W1": mt5.TIMEFRAME_W1,
        }
        mt5_tf = tf_map.get(timeframe.upper(), mt5.TIMEFRAME_H1)
        rates = mt5.copy_rates_from_pos(broker_sym, mt5_tf, 0, bars + 50)
        if rates is None or len(rates) == 0:
            return None

        df = pd.DataFrame(rates)
        df["timestamp"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df = df.rename(columns={"tick_volume": "volume"}).drop(columns=["time", "real_volume"], errors="ignore")
        # Exclude currently forming bar
        df = df.iloc[:-1].tail(bars).reset_index(drop=True)
        return df[["timestamp", "open", "high", "low", "close", "volume"]]
    except Exception:
        return None


def _try_fetch_yahoo(symbol: str, timeframe: str, bars: int) -> Optional[pd.DataFrame]:
    try:
        import yfinance as yf
        yf_symbol = YAHOO_SYMBOL_MAP.get(symbol, symbol)
        yf_interval = TIMEFRAME_TO_YFINANCE.get(timeframe.upper(), "1h")

        # Select reasonable period
        if yf_interval in ("1m", "5m"):
            period = "7d"
        elif yf_interval in ("15m", "30m"):
            period = "30d"
        elif yf_interval == "1h":
            period = "60d"
        else:
            period = "2y"

        ticker = yf.Ticker(yf_symbol)
        df = ticker.history(period=period, interval=yf_interval)
        if df.empty:
            return None

        df = df.reset_index()
        col_map = {}
        for col in df.columns:
            c_low = str(col).lower()
            if "date" in c_low or "time" in c_low:
                col_map[col] = "timestamp"
            elif c_low == "open":
                col_map[col] = "open"
            elif c_low == "high":
                col_map[col] = "high"
            elif c_low == "low":
                col_map[col] = "low"
            elif c_low == "close":
                col_map[col] = "close"
            elif c_low == "volume":
                col_map[col] = "volume"

        df = df.rename(columns=col_map)
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        df = df[["timestamp", "open", "high", "low", "close", "volume"]].tail(bars).reset_index(drop=True)
        return df
    except Exception as exc:
        return None


# =====================================================================
# BACKTEST ENGINE & METRICS
# =====================================================================

def evaluate_strategy_backtest(
    strategy: Strategy,
    data: pd.DataFrame,
    commission_bps: float = 3.0,
) -> Dict[str, Any]:
    """
    Executes backtest replay of strategy signals on price data.
    Returns comprehensive metrics dictionary.
    """
    signals = strategy.generate_signals(data)
    if isinstance(signals, pd.DataFrame) and "primary_position" in signals.columns:
        sig_series = signals["primary_position"]
    else:
        sig_series = signals

    sig_series = pd.Series(sig_series).reindex(data.index).fillna(0.0)

    close_prices = data["close"].values
    timestamps = data["timestamp"].values if "timestamp" in data.columns else data.index
    n = len(data)

    trades: List[Dict[str, Any]] = []
    trade_id = 1
    in_pos = 0.0
    entry_price = 0.0
    entry_ts = None
    entry_idx = 0

    comm_rate = (commission_bps / 10000.0)

    for i in range(n):
        target_pos = float(sig_series.iloc[i])
        px = float(close_prices[i])
        ts = timestamps[i]

        # Position changed
        if target_pos != in_pos:
            # Close existing position
            if in_pos != 0.0:
                ret = ((px - entry_price) / entry_price) if in_pos > 0 else ((entry_price - px) / entry_price)
                net_ret = ret - (2 * comm_rate)
                trades.append({
                    "id": trade_id,
                    "direction": "LONG" if in_pos > 0 else "SHORT",
                    "entry_ts": entry_ts,
                    "exit_ts": ts,
                    "entry_price": entry_price,
                    "exit_price": px,
                    "return_pct": ret * 100.0,
                    "net_return_pct": net_ret * 100.0,
                    "bars_held": i - entry_idx,
                })
                trade_id += 1

            # Open new position
            if target_pos != 0.0:
                in_pos = target_pos
                entry_price = px
                entry_ts = ts
                entry_idx = i
            else:
                in_pos = 0.0

    # Close open trade at end of data
    if in_pos != 0.0:
        px = float(close_prices[-1])
        ret = ((px - entry_price) / entry_price) if in_pos > 0 else ((entry_price - px) / entry_price)
        net_ret = ret - (2 * comm_rate)
        trades.append({
            "id": trade_id,
            "direction": "LONG" if in_pos > 0 else "SHORT",
            "entry_ts": entry_ts,
            "exit_ts": timestamps[-1],
            "entry_price": entry_price,
            "exit_price": px,
            "return_pct": ret * 100.0,
            "net_return_pct": net_ret * 100.0,
            "bars_held": n - 1 - entry_idx,
        })

    df_trades = pd.DataFrame(trades)
    if df_trades.empty:
        return {
            "total_trades": 0,
            "win_rate": 0.0,
            "total_pnl_pct": 0.0,
            "net_pnl_pct": 0.0,
            "profit_factor": 0.0,
            "sharpe_ratio": 0.0,
            "max_drawdown_pct": 0.0,
            "avg_trade_bars": 0.0,
            "trades": df_trades,
        }

    net_returns = df_trades["net_return_pct"].values
    wins = net_returns[net_returns > 0]
    losses = net_returns[net_returns < 0]

    win_rate = (len(wins) / len(net_returns)) * 100.0 if len(net_returns) > 0 else 0.0
    total_pnl = float(df_trades["return_pct"].sum())
    net_pnl = float(df_trades["net_return_pct"].sum())

    gross_profit = float(wins.sum()) if len(wins) > 0 else 0.0
    gross_loss = float(abs(losses.sum())) if len(losses) > 0 else 1e-6
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    # Equity curve and Max Drawdown
    equity = np.cumprod(1.0 + (net_returns / 100.0))
    peak = np.maximum.accumulate(equity)
    drawdowns = (equity - peak) / peak
    max_dd = float(abs(drawdowns.min()) * 100.0) if len(drawdowns) > 0 else 0.0

    # Sharpe approximation
    std_ret = np.std(net_returns)
    sharpe = float((np.mean(net_returns) / std_ret) * np.sqrt(252)) if std_ret > 1e-8 else 0.0

    return {
        "total_trades": len(df_trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": win_rate,
        "total_pnl_pct": total_pnl,
        "net_pnl_pct": net_pnl,
        "profit_factor": profit_factor,
        "sharpe_ratio": sharpe,
        "max_drawdown_pct": max_dd,
        "avg_trade_bars": float(df_trades["bars_held"].mean()),
        "trades": df_trades,
    }


def print_backtest_report(
    symbol: str,
    timeframe: str,
    strat_name: str,
    data_source: str,
    bar_count: int,
    results: Dict[str, Any],
):
    """Prints a beautiful, easy-to-read ASCII performance dashboard."""
    trades = results["trades"]
    print("\n" + "=" * 65)
    print(f"       MIMIR QUANT STRATEGY BACKTEST RESULTS: {strat_name.upper()}")
    print("=" * 65)
    print(f" Instrument:    {symbol:<12} Timeframe:   {timeframe}")
    print(f" Data Source:   {data_source}")
    print(f" Sample Bars:   {bar_count} closed candles")
    print("-" * 65)
    print(f" Total Trades:          {results['total_trades']:<10} (Wins: {results.get('wins', 0)} | Losses: {results.get('losses', 0)})")
    print(f" Win Rate:              {results['win_rate']:>6.2f}%")
    print(f" Total Return (Gross):  {results['total_pnl_pct']:>6.2f}%")
    print(f" Total Return (Net):    {results['net_pnl_pct']:>6.2f}% (After commissions)")
    print(f" Profit Factor:         {results['profit_factor']:>6.2f}")
    print(f" Sharpe Ratio:          {results['sharpe_ratio']:>6.2f}")
    print(f" Max Drawdown:          {results['max_drawdown_pct']:>6.2f}%")
    print(f" Avg Trade Duration:    {results['avg_trade_bars']:>6.1f} bars")
    print("-" * 65)

    if not trades.empty:
        print("\nLast 5 Closed Trades:")
        tail_trades = trades.tail(5)[["id", "direction", "entry_price", "exit_price", "net_return_pct", "bars_held"]]
        print(tail_trades.to_string(index=False))
    print("=" * 65 + "\n")


# =====================================================================
# STRATEGY SCAFFOLDER (Instant 1-Click Code Generator)
# =====================================================================

STRATEGY_TEMPLATE = '''# bong_strats/strategies/{clean_name}.py
"""
{clean_name} Quantitative Strategy.
Generated by MIMIR Strategy Studio.
"""

from bong_strats.quick_strategy import QuickStrategy, BarIndicators
import pandas as pd


class {class_name}(QuickStrategy):
    name = "{clean_name}"
    description = "{clean_name} quantitative strategy"

    # Strategy Parameters (tunable)
    fast_period: int = 10
    slow_period: int = 30
    rsi_period: int = 14

    # Risk Controls (%)
    stop_loss_pct: float = 1.5
    take_profit_pct: float = 3.5
    trailing_stop_pct: float = 0.0

    def on_bar(self, bar: pd.Series, ind: BarIndicators):
        close = float(bar["close"])
        fast_ma = ind.sma_n(self.fast_period)
        slow_ma = ind.sma_n(self.slow_period)
        rsi = ind.rsi_n(self.rsi_period)

        # Example Entry Logic:
        # Bullish: Fast MA above Slow MA and RSI not overbought
        if fast_ma > slow_ma and rsi < 65:
            return "BUY"

        # Bearish: Fast MA below Slow MA
        if fast_ma < slow_ma and self.net_position > 0:
            return "CLOSE"

        return "HOLD"
'''


def scaffold_new_strategy(name: str) -> Path:
    """Generates a new, plug-and-play strategy file."""
    clean_name = name.lower().replace("-", "_").replace(" ", "_")
    class_name = "".join(word.capitalize() for word in clean_name.split("_")) + "Strategy"

    strat_dir = PROJECT_ROOT / "bong_strats" / "strategies"
    strat_dir.mkdir(parents=True, exist_ok=True)
    target_path = strat_dir / f"{clean_name}.py"

    if target_path.exists():
        print(f"[!] Strategy file already exists at: {target_path}")
        return target_path

    code = STRATEGY_TEMPLATE.format(clean_name=clean_name, class_name=class_name)
    target_path.write_text(code, encoding="utf-8")
    print(f"\n[SUCCESS] Created new strategy template at:\n  --> {target_path}\n")
    reload_registries()
    return target_path


# =====================================================================
# INTERACTIVE STUDIO
# =====================================================================

DEFAULT_ASSET_CHOICES = [
    ("XAUUSD", "Gold / US Dollar"),
    ("EURUSD", "Euro / US Dollar"),
    ("GBPUSD", "British Pound / US Dollar"),
    ("BTCUSD", "Bitcoin / USD"),
    ("NVDA", "Nvidia Corporation"),
    ("AAPL", "Apple Inc."),
    ("TSLA", "Tesla Inc."),
]

DEFAULT_TIMEFRAMES = ["M5", "M15", "H1", "H4", "D1"]


def run_interactive_studio():
    """Interactive CLI menu for strategy exploration, testing, and creation."""
    print("=" * 65)
    print("       MIMIR QUANT STRATEGY STUDIO & INSTANT RUNNER")
    print("=" * 65)

    catalog = list_strategies()
    print(f"\nDiscovered {len(catalog)} Quantitative Strategies:")
    for idx, s in enumerate(catalog, 1):
        status_tag = "[LIVE READY]" if s["is_live_ready"] else "[BACKTEST ONLY]"
        print(f"  [{idx}] {s['id']:<26} {status_tag:<16} {s['description']}")

    print(f"  [N] [CREATE NEW STRATEGY TEMPLATE]")
    print(f"  [Q] Exit")

    choice = input("\nSelect strategy [1-{}] or N/Q: ".format(len(catalog))).strip().upper()
    if choice == "Q":
        return
    if choice == "N":
        s_name = input("Enter new strategy name (e.g. trend_breakout): ").strip()
        if s_name:
            scaffold_new_strategy(s_name)
        return

    try:
        chosen_idx = int(choice) - 1
        strat_info = catalog[chosen_idx]
    except Exception:
        print("[!] Invalid selection.")
        return

    strat_id = strat_info["id"]
    strat_cls = FULL_STRATEGY_REGISTRY[strat_id]
    strategy_instance = strat_cls()

    # Asset selection
    print("\nSelect Trading Instrument:")
    for a_idx, (sym, label) in enumerate(DEFAULT_ASSET_CHOICES, 1):
        print(f"  [{a_idx}] {sym:<10} ({label})")
    print("  [C] Custom Ticker (Enter any symbol)")

    a_choice = input(f"Choose asset [1-{len(DEFAULT_ASSET_CHOICES)}] or symbol (default XAUUSD): ").strip()
    if a_choice.upper() == "C":
        symbol = input("Enter custom symbol: ").strip().upper()
    elif a_choice.isdigit() and 1 <= int(a_choice) <= len(DEFAULT_ASSET_CHOICES):
        symbol = DEFAULT_ASSET_CHOICES[int(a_choice) - 1][0]
    elif a_choice:
        symbol = a_choice.upper()
    else:
        symbol = "XAUUSD"

    # Timeframe selection
    print(f"\nSelect Timeframe: {', '.join(DEFAULT_TIMEFRAMES)}")
    tf = input("Choose timeframe (default H1): ").strip().upper() or "H1"

    # Bar count
    bars_in = input("Historical bar count (default 1000): ").strip()
    bars = int(bars_in) if bars_in.isdigit() else 1000

    print(f"\n[+] Fetching market bars for {symbol} ({tf})...")
    try:
        data, source = fetch_historical_bars(symbol, tf, bars=bars)
    except Exception as exc:
        print(f"[ERROR] {exc}")
        return

    print(f"[+] Running backtest on {len(data)} bars ({source})...")
    res = evaluate_strategy_backtest(strategy_instance, data)
    print_backtest_report(symbol, tf, strat_id, source, len(data), res)

    # Next steps menu
    print("Next Actions:")
    print("  [1] Launch Live / Paper Trader in MT5 with this strategy")
    print("  [2] Create a new strategy from template")
    print("  [3] Run another backtest")
    print("  [Q] Exit")

    next_act = input("\nChoose action [1-3] or Q: ").strip().upper()
    if next_act == "1":
        print(f"\n[+] Launching standalone MT5 live trader for {symbol} using {strat_id}...")
        import subprocess
        python_exe = sys.executable
        subprocess.run([python_exe, "scripts/standalone_mt5_live_trader.py"])
    elif next_act == "2":
        s_name = input("Enter new strategy name: ").strip()
        if s_name:
            scaffold_new_strategy(s_name)


# =====================================================================
# CLI PARSER
# =====================================================================

def main():
    parser = argparse.ArgumentParser(description="MIMIR Fast Quant Strategy Studio & Backtester")
    parser.add_argument("--strategy", "-s", type=str, help="Strategy identifier name")
    parser.add_argument("--symbol", "-y", type=str, default="XAUUSD", help="Symbol to test (e.g. XAUUSD, NVDA, BTCUSD)")
    parser.add_argument("--timeframe", "-t", type=str, default="H1", help="Timeframe (M1, M5, M15, H1, D1)")
    parser.add_argument("--bars", "-b", type=int, default=1000, help="Bar count to download")
    parser.add_argument("--list", "-l", action="store_true", help="List all discovered strategies")
    parser.add_argument("--new", "-n", type=str, help="Scaffold a new strategy with the given name")

    args = parser.parse_args()

    if args.list:
        print("Discovered Strategies:")
        for s in list_strategies():
            tag = "LIVE" if s["is_live_ready"] else "BACKTEST"
            print(f"  [{tag:<8}] {s['id']:<26} {s['description']}")
        return

    if args.new:
        scaffold_new_strategy(args.new)
        return

    if args.strategy:
        strat_key = args.strategy.lower().strip()
        if strat_key not in FULL_STRATEGY_REGISTRY:
            print(f"[!] Strategy '{args.strategy}' not found. Available: {', '.join(FULL_STRATEGY_REGISTRY.keys())}")
            return
        cls = FULL_STRATEGY_REGISTRY[strat_key]
        strat = cls()
        data, src = fetch_historical_bars(args.symbol, args.timeframe, bars=args.bars)
        res = evaluate_strategy_backtest(strat, data)
        print_backtest_report(args.symbol, args.timeframe, strat_key, src, len(data), res)
        return

    # No arguments provided -> run interactive studio
    run_interactive_studio()


if __name__ == "__main__":
    main()
