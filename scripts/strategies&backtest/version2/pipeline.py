"""
Pipeline entry point.

Wires together the data fetching module and the strategies module:
pull historical bars from MT5, run a strategy over them, compute
simple backtest return metrics, and export the result to CSV.
"""

import MetaTrader5 as mt5
import matplotlib
matplotlib.use("Agg")  # Allow PNG export on servers without a desktop display.
import matplotlib.pyplot as plt
import pandas as pd
from datetime import datetime

import numpy as np
from data_fetching import add_features, fetch_multiple_symbols
from strategies import (
    Strategies,
    MeanReversionMLStrategy,
    DualHedgeSelectionStrategy,
    RLAdaptiveStrategy,
    MonteCarloValidator,
)

# Maps a strategy_name string -> a factory that builds that Strategy instance.
STRATEGY_FACTORIES = {
    "mean_reversion_ml": lambda: MeanReversionMLStrategy(
        ou_lookback=100,
        zscore_entry=1,
        zscore_exit=0.0,
        zscore_flip=2.5,
        ml_confidence=0.80,
        vol_stop_mult=3.0,
        max_risk_pct=0.1,
        train_ratio=0.25,
        retrain_interval=200,
    ),
    "dual_hedge": lambda: DualHedgeSelectionStrategy(
        ou_lookback=100,
        zscore_entry=1,
        hedge_trigger_loss_pct=0.025,
        cut_losing_leg_pct=0.02,
        winning_leg_profit_pct=0.01,
        trailing_stop_pct=0.02,
        take_profit_pct=0.02,
    ),
    "rl_adaptive": lambda: RLAdaptiveStrategy(
        total_timesteps=25_000,
        train_ratio=0.50,
    ),
}


def compute_backtest_stats(df: pd.DataFrame) -> dict:
    """Computes detailed trade metrics including Win Rate, Total Trades, Sharpe Ratio, and Drawdowns."""
    df_clean = df.dropna(subset=["strategy_return", "market_return"]).copy()
    exec_pos = df_clean["signal"].shift(1).fillna(0).values
    mkt_ret = df_clean["market_return"].values

    trades = []
    current_pnl = 0.0
    in_trade = False

    for i in range(len(exec_pos)):
        pos = exec_pos[i]
        ret = mkt_ret[i]

        if pos != 0:
            in_trade = True
            current_pnl += pos * ret
            next_pos = exec_pos[i + 1] if i + 1 < len(exec_pos) else 0.0
            if next_pos != pos:
                trades.append(current_pnl)
                current_pnl = 0.0
                in_trade = False

    if in_trade:
        trades.append(current_pnl)

    trades = np.array(trades)
    n_trades = len(trades)
    wins = trades[trades > 0]
    losses = trades[trades < 0]

    win_rate = (len(wins) / n_trades * 100.0) if n_trades > 0 else 0.0
    tot_win = wins.sum() if len(wins) > 0 else 0.0
    tot_loss = abs(losses.sum()) if len(losses) > 0 else 1e-8
    profit_factor = (tot_win / tot_loss) if tot_loss > 0 else 0.0

    strat_ret = df_clean["strategy_return"]
    cum_ret = (1 + strat_ret).cumprod()
    net_return = (cum_ret.iloc[-1] - 1) * 100.0 if len(cum_ret) > 0 else 0.0

    rolling_max = cum_ret.cummax()
    dd = (cum_ret - rolling_max) / rolling_max.where(rolling_max != 0)
    max_dd = dd.min() * 100.0 if len(dd) > 0 else 0.0

    std = strat_ret.std()
    sharpe = (strat_ret.mean() / std * np.sqrt(252 * 24)) if std > 0 else 0.0

    return {
        "n_trades": n_trades,
        "n_wins": len(wins),
        "n_losses": len(losses),
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "net_return": net_return,
        "max_drawdown": max_dd,
        "sharpe_ratio": sharpe,
    }


def plot_backtest(df: pd.DataFrame, output_filename: str, initial_value: float = 10_000):
    """Save price/trade markers and strategy-vs-market portfolio value chart."""
    chart_data = df.copy()
    chart_data["plot_time"] = pd.to_datetime(chart_data["time"])
    chart_data = chart_data.sort_values("plot_time")

    stats = compute_backtest_stats(df)

    # A signal is acted on at the next bar in the backtest calculation, so the
    # markers use the same shifted position used by strategy_return.
    chart_data["executed_position"] = chart_data["signal"].shift(1).fillna(0)
    previous_position = chart_data["executed_position"].shift(1).fillna(0)
    long_entries = (chart_data["executed_position"] > 0) & (previous_position <= 0)
    short_entries = (chart_data["executed_position"] < 0) & (previous_position >= 0)
    closes = (previous_position != 0) & (chart_data["executed_position"] == 0)

    chart_data["market_value"] = initial_value * (
        1 + chart_data["market_return"].fillna(0)
    ).cumprod()
    chart_data["strategy_value"] = initial_value * (
        1 + chart_data["strategy_return"].fillna(0)
    ).cumprod()

    figure, (price_axis, value_axis) = plt.subplots(
        2, 1, sharex=True, figsize=(15, 9), height_ratios=[2, 1]
    )
    price_axis.plot(chart_data["plot_time"], chart_data["close"], label="Market price", color="black")
    price_axis.scatter(
        chart_data.loc[long_entries, "plot_time"], chart_data.loc[long_entries, "close"],
        marker="^", color="green", s=45 + 70 * chart_data.loc[long_entries, "executed_position"].abs(),
        label="Open long (size = exposure)", zorder=3,
    )
    price_axis.scatter(
        chart_data.loc[short_entries, "plot_time"], chart_data.loc[short_entries, "close"],
        marker="v", color="red", s=45 + 70 * chart_data.loc[short_entries, "executed_position"].abs(),
        label="Open short (size = exposure)", zorder=3,
    )
    price_axis.scatter(
        chart_data.loc[closes, "plot_time"], chart_data.loc[closes, "close"],
        marker="x", color="royalblue", s=50, label="Close position", zorder=3,
    )
    price_axis.set_ylabel("Price")
    price_axis.set_title(
        f"Market price and strategy trades | Win Rate: {stats['win_rate']:.1f}% "
        f"({stats['n_wins']}/{stats['n_trades']} trades) | Net Ret: {stats['net_return']:+.2f}% | Max DD: {stats['max_drawdown']:.2f}%"
    )
    price_axis.grid(alpha=0.25)
    price_axis.legend(ncol=4)

    value_axis.plot(chart_data["plot_time"], chart_data["market_value"], label="Buy and hold", color="gray")
    value_axis.plot(
        chart_data["plot_time"], chart_data["strategy_value"], label="Strategy portfolio", color="tab:blue"
    )
    value_axis.axhline(initial_value, color="black", linewidth=0.8, linestyle="--")
    value_axis.set_ylabel("Portfolio value")
    value_axis.set_xlabel("Time")
    value_axis.grid(alpha=0.25)
    value_axis.legend()

    figure.tight_layout()
    figure.savefig(output_filename, dpi=150, bbox_inches="tight")
    plt.close(figure)
    print(f"Backtest chart saved to {output_filename}")


def run_pipeline(
    symbols,
    timeframe,
    start_date,
    end_date,
    strategy_name="mean_reversion_ml",
    output_filename=None,
    raw_data_filename="combined_historical_data.csv",
    chart_filename=None,
    initial_portfolio_value: float = 10_000,
):
    """
    Fetches historical data, runs the given strategy, attaches
    signal/return metrics, and exports both the raw combined data and
    the final signal-annotated data to CSV.

    Args:
        strategy_name (str): key into STRATEGY_FACTORIES selecting which
            strategy to run (e.g. "mean_reversion_ml").
        output_filename (str, optional): defaults to
            "{strategy_name}_signals.csv" if not given.
        chart_filename (str, optional): defaults to
            "{strategy_name}_backtest.png".

    Returns:
        pd.DataFrame: the combined data with signal and return columns.
    """
    if strategy_name not in STRATEGY_FACTORIES:
        raise KeyError(
            f"Unknown strategy_name '{strategy_name}'. "
            f"Available: {list(STRATEGY_FACTORIES)}"
        )

    if output_filename is None:
        output_filename = f"{strategy_name}_signals.csv"
    if chart_filename is None:
        chart_filename = f"{strategy_name}_backtest.png"

    # 1. Fetch data
    data = fetch_multiple_symbols(symbols, timeframe, start_date, end_date)
    if not data:
        print("No data fetched, aborting pipeline.")
        return None

    # Features are calculated per symbol so rolling indicators cannot bleed
    # from one market into another.
    data = {symbol: add_features(frame) for symbol, frame in data.items()}
    combined = pd.concat(data.values(), ignore_index=True)
    combined.to_csv(raw_data_filename, index=False)
    print("\nCombined shape:", combined.shape)

    # 2. Register + run the selected strategy
    strategies = Strategies()
    strategies.register(STRATEGY_FACTORIES[strategy_name]())
    print(strategies)

    df = combined.copy()
    signals = strategies.run(strategy_name, df)

    # 3. Attach signals & metrics
    df["signal"] = signals
    df["market_return"] = df["close"].pct_change()
    df["strategy_return"] = df["signal"].shift(1) * df["market_return"]
    df["cum_strategy_return"] = (
        1 + df["strategy_return"].fillna(0)
    ).cumprod() - 1

    # 4. Export & Stats Summary
    df.to_csv(output_filename, index=False)
    print(f"Signals exported successfully to {output_filename}")
    if chart_filename:
        plot_backtest(df, chart_filename, initial_value=initial_portfolio_value)

    stats = compute_backtest_stats(df)
    print("\n" + "=" * 60)
    print(" 📊 STRATEGY BACKTEST PERFORMANCE SUMMARY")
    print("=" * 60)
    print(f" Total Trades:     {stats['n_trades']} ({stats['n_wins']} wins, {stats['n_losses']} losses)")
    print(f" Win Rate:         {stats['win_rate']:.2f}%")
    print(f" Net Return:       {stats['net_return']:+.2f}%")
    print(f" Max Drawdown:     {stats['max_drawdown']:.2f}%")
    print(f" Profit Factor:    {stats['profit_factor']:.2f}")
    print(f" Sharpe Ratio:     {stats['sharpe_ratio']:.2f}")
    print("=" * 60)

    return df


def run_monte_carlo_test(n_paths: int = 60, path_length: int = 2000):
    """Run Monte Carlo stress testing against synthetic price paths."""
    print("=" * 70)
    print(" 🎲 RUNNING MONTE CARLO STRESS TEST FOR MEAN REVERSION ML STRATEGY")
    print("=" * 70)
    validator = MonteCarloValidator(n_paths=n_paths, path_length=path_length)
    strategy = MeanReversionMLStrategy()
    results = validator.run(strategy, verbose=True)
    return results


if __name__ == "__main__":
    # --- 1. Run Monte Carlo Stress Test on synthetic unseen futures ---
    run_monte_carlo_test(n_paths=60, path_length=1500)

    # --- 2. Run Historical Backtest on MT5 Data ---
    symbols = ["BTCUSD"]
    tf = mt5.TIMEFRAME_H1  # Recommended H1 timeframe
    start = datetime(2024, 1, 1, 0, 0)
    end = datetime(2026, 7, 26, 0, 0)
    
    # Options: "dual_hedge", "mean_reversion_ml", or "rl_adaptive"
    strategy_name = "dual_hedge"

    df = run_pipeline(symbols, tf, start, end, strategy_name=strategy_name, initial_portfolio_value=10_000)

    if df is not None:
        print("\nLast 5 rows of backtest result:")
        print(df[df["symbol"] == symbols[0]].tail())
        print(df[df["symbol"] == symbols[0]][["market_return", "strategy_return", "cum_strategy_return"]].tail())
