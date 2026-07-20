import MetaTrader5 as mt5
import pandas as pd
from datetime import datetime


def fetch_historical_data(symbol, timeframe, start_date, end_date):
    """
    Fetches historical price data for a single symbol and timeframe.

    Args:
        symbol (str): The trading symbol (e.g., "BTCUSD").
        timeframe (int): The timeframe for the bars (e.g., mt5.TIMEFRAME_H1).
        start_date (datetime): The start date for the data range.
        end_date (datetime): The end date for the data range.

    Returns:
        pd.DataFrame or None: DataFrame of historical price data, or None if
        the pull failed / returned no rows.
    """
    rates = mt5.copy_rates_range(symbol, timeframe, start_date, end_date)

    if rates is None or len(rates) == 0:
        print(f"[{symbol}] No data found or error: {mt5.last_error()}")
        return None

    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s")
    df["symbol"] = symbol

    print(
        f"[{symbol}] Successfully pulled {len(df)} bars "
        f"({df['time'].iloc[0]} -> {df['time'].iloc[-1]})."
    )

    return df


def fetch_multiple_symbols(
    symbols, timeframe, start_date, end_date, terminal_path=None
):
    """
    Fetches historical price data for multiple symbols over the same
    timeframe and date range, in a single MT5 session.

    Args:
        symbols (list[str]): Trading symbols, e.g. ["BTCUSD", "ETHUSD", "XAUUSD"].
        timeframe (int): mt5 timeframe constant (e.g. mt5.TIMEFRAME_H1).
        start_date (datetime): Start of the range.
        end_date (datetime): End of the range.
        terminal_path (str, optional): Path to terminal64.exe if you need to
            point at a specific broker terminal instead of the default one.

    Returns:
        dict[str, pd.DataFrame]: symbol -> DataFrame (only symbols that
        returned data are included).
    """
    init_ok = mt5.initialize(path=terminal_path) if terminal_path else mt5.initialize()
    if not init_ok:
        print(f"initialize() failed, error code: {mt5.last_error()}")
        return {}

    try:
        results = {}
        for symbol in symbols:
            # copy_rates_range needs the symbol to be selected/visible first
            if not mt5.symbol_select(symbol, True):
                print(
                    f"[{symbol}] symbol_select failed, error code: {mt5.last_error()}"
                )
                continue

            df = fetch_historical_data(symbol, timeframe, start_date, end_date)
            if df is not None:
                results[symbol] = df
    finally:
        # always shut down even if something above raises
        mt5.shutdown()

    return results


if __name__ == "__main__":
    # Example usage
    symbols = ["BTCUSD", "ETHUSD", "NVDA.US"]
    tf = mt5.TIMEFRAME_H1
    start = datetime(2026, 1, 1, 0, 0)
    end = datetime(2026, 6, 15, 0, 0)

    data = fetch_multiple_symbols(symbols, tf, start, end)

    for sym, df in data.items():
        print(f"\n{sym}:")
        print(df.head())

    # Optional: combine into one long-format DataFrame
    if data:
        combined = pd.concat(data.values(), ignore_index=True)
        output_file = "combined_historical_data.csv"
        combined.to_csv(output_file, index=False)
        print("\nCombined shape:", combined.shape)

"""
Strategy framework.

Design:
- `Strategy` is an abstract base class. Every concrete strategy implements
  `generate_signals(data)` and returns a pd.Series of positions/signals
  indexed the same as the input data. Convention used here:
      1  -> long
     -1  -> short
      0  -> flat
  (Swap this convention for continuous position sizing if you prefer -
   just keep it consistent across strategies so they're comparable.)

- `Strategies` is a registry/orchestrator. It doesn't contain trading logic
  itself - it holds strategy instances, runs them, and lets you compare or
  combine their output. This keeps each strategy's logic isolated and
  independently testable.
"""

from abc import ABC, abstractmethod
import pandas as pd
import numpy as np

# ---------------------------------------------------------------------------
# Base interface
# ---------------------------------------------------------------------------


class Strategy(ABC):
    """Common interface every strategy must implement."""

    name: str = "base_strategy"

    @abstractmethod
    def generate_signals(self, data: pd.DataFrame) -> pd.Series:
        """
        Args:
            data: OHLCV DataFrame, indexed by time, with at least a 'close'
                  column (and 'symbol' if multi-symbol).

        Returns:
            pd.Series of {-1, 0, 1} aligned to data.index.
        """
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Mean reversion
# ---------------------------------------------------------------------------


class MeanReversionStrategy(Strategy):
    """
    Z-score mean reversion on a rolling window: go long when price is
    `entry_z` std devs below its rolling mean, short when above, exit
    back toward `exit_z`.
    """

    name = "mean_reversion"

    def __init__(self, lookback: int = 20, entry_z: float = 2.0, exit_z: float = 0.5):
        self.lookback = lookback
        self.entry_z = entry_z
        self.exit_z = exit_z

    def generate_signals(self, data: pd.DataFrame) -> pd.Series:
        close = data["close"]
        rolling_mean = close.rolling(self.lookback).mean()
        rolling_std = close.rolling(self.lookback).std()
        zscore = (close - rolling_mean) / rolling_std

        signals = pd.Series(0, index=data.index)
        signals[zscore < -self.entry_z] = 1  # oversold -> long
        signals[zscore > self.entry_z] = -1  # overbought -> short
        # flatten once it reverts back near the mean
        signals[zscore.abs() < self.exit_z] = 0
        signals = signals.replace(0, np.nan).ffill().fillna(0)

        return signals


# ---------------------------------------------------------------------------
# ML-based
# ---------------------------------------------------------------------------


class MLStrategy(Strategy):
    """
    Wraps a trained sklearn/XGBoost-style classifier that predicts
    direction (or return sign) from a feature set. This class only
    handles inference + signal mapping; training/feature engineering
    should happen upstream and the fitted model passed in.
    """

    name = "ml"

    def __init__(
        self,
        model,
        feature_cols: list,
        threshold: float = 0.5,
        long_class=1,
        short_class=0,
    ):
        """
        Args:
            model: fitted classifier exposing .predict_proba() or .predict()
            feature_cols: columns in `data` used as model input
            threshold: confidence threshold for predict_proba-based models
            long_class / short_class: which model output label maps to
                long / short (set short_class=None for a long-only model)
        """
        self.model = model
        self.feature_cols = feature_cols
        self.threshold = threshold
        self.long_class = long_class
        self.short_class = short_class

    def generate_signals(self, data: pd.DataFrame) -> pd.Series:
        X = data[self.feature_cols].dropna()
        signals = pd.Series(0, index=data.index)

        if hasattr(self.model, "predict_proba"):
            proba = self.model.predict_proba(X)
            classes = list(self.model.classes_)
            long_idx = classes.index(self.long_class)
            long_conf = proba[:, long_idx]

            preds = pd.Series(0, index=X.index)
            preds[long_conf >= self.threshold] = 1
            if self.short_class is not None:
                short_idx = classes.index(self.short_class)
                short_conf = proba[:, short_idx]
                preds[short_conf >= self.threshold] = -1
        else:
            raw = self.model.predict(X)
            preds = pd.Series(raw, index=X.index).map(
                lambda p: (
                    1 if p == self.long_class else (-1 if p == self.short_class else 0)
                )
            )

        signals.loc[preds.index] = preds
        return signals


class VolumeDeltaReversalStrategy(Strategy):
    """
    Bar-by-bar 1H volume-imbalance strategy with adaptive reversal,
    statistical take-profit, and vol-adaptive stop-loss.

    Expected `data` columns: 'open', 'high', 'low', 'close', 'volume',
    and optionally 'taker_buy_volume' (Binance-style), indexed by a
    DatetimeIndex at 1H frequency.
    """

    name = "volume_delta_reversal"

    def __init__(
        self,
        std_lookback: int = 24,  # bars, for rolling stop-loss vol
        sl_std_mult: float = 1.5,  # stop-loss = mult * rolling std
        tp_quantile: float = 0.5,  # quantile of |daily return| used as TP target
        daily_return_lookback_days: int = 30,
        volume_col: str = "volume",
        taker_buy_col: (
            str | None
        ) = "taker_buy_volume",  # set None to force the OHLC proxy
        min_bars_before_flip: int = 1,  # require adverse move to persist this many bars before flipping
    ):
        self.std_lookback = std_lookback
        self.sl_std_mult = sl_std_mult
        self.tp_quantile = tp_quantile
        self.daily_return_lookback_days = daily_return_lookback_days
        self.volume_col = volume_col
        self.taker_buy_col = taker_buy_col
        self.min_bars_before_flip = min_bars_before_flip

    # ------------------------------------------------------------------
    # Buy/sell volume delta per bar
    # ------------------------------------------------------------------
    def _compute_volume_delta(self, data: pd.DataFrame) -> pd.Series:
        if self.taker_buy_col and self.taker_buy_col in data.columns:
            buy_vol = data[self.taker_buy_col]
            sell_vol = data[self.volume_col] - buy_vol
        else:
            # Proxy: assume volume traded in the upper part of the bar's
            # range was buyer-initiated, lower part seller-initiated.
            rng = (data["high"] - data["low"]).replace(0, np.nan)
            buy_frac = (data["close"] - data["low"]) / rng
            buy_frac = buy_frac.fillna(0.5)  # doji bars: treat as balanced
            buy_vol = data[self.volume_col] * buy_frac
            sell_vol = data[self.volume_col] - buy_vol
        return buy_vol - sell_vol  # positive = net buying pressure

    # ------------------------------------------------------------------
    # Statistical take-profit target from trailing daily returns
    # ------------------------------------------------------------------
    def _daily_tp_target(self, data: pd.DataFrame) -> float:
        daily_close = data["close"].resample("1D").last().dropna()
        daily_ret = daily_close.pct_change().dropna()
        window = daily_ret.tail(self.daily_return_lookback_days)
        if len(window) < 5:
            return 0.01  # fallback 1% until enough history accumulates
        return float(window.abs().quantile(self.tp_quantile))

    # ------------------------------------------------------------------
    # Main signal loop (stateful: needs position/entry tracking, so this
    # is bar-by-bar rather than fully vectorized)
    # ------------------------------------------------------------------
    def generate_signals(self, data: pd.DataFrame) -> pd.Series:
        vol_delta = self._compute_volume_delta(data)
        returns = data["close"].pct_change()
        rolling_std = returns.rolling(self.std_lookback).std()
        tp_target = self._daily_tp_target(data)

        close = data["close"].values
        n = len(data)
        signals = pd.Series(0, index=data.index)

        position = 0  # -1, 0, 1
        entry_price = None
        expected_dir = 0
        bars_since_entry = 0

        for t in range(1, n):
            # --- manage an existing position: TP / SL / reversal check ---
            if position != 0:
                bars_since_entry += 1
                unrealized = (close[t] - entry_price) / entry_price * position

                sl_dist = (
                    rolling_std.iloc[t] * self.sl_std_mult
                    if not np.isnan(rolling_std.iloc[t])
                    else None
                )

                hit_tp = unrealized >= tp_target
                hit_sl = sl_dist is not None and unrealized <= -sl_dist

                price_move_sign = np.sign(close[t] - entry_price)
                went_against = (
                    bars_since_entry >= self.min_bars_before_flip
                    and price_move_sign != 0
                    and price_move_sign != expected_dir
                )

                if hit_tp or hit_sl:
                    position = 0
                    entry_price = None
                    bars_since_entry = 0
                elif went_against:
                    # flip straight to the opposite side
                    position = -position
                    expected_dir = position
                    entry_price = close[t]
                    bars_since_entry = 0

            # --- flat: evaluate a fresh volume-delta signal ---
            if position == 0:
                delta = vol_delta.iloc[t]
                if delta > 0:
                    position = 1
                elif delta < 0:
                    position = -1
                if position != 0:
                    expected_dir = position
                    entry_price = close[t]
                    bars_since_entry = 0

            signals.iloc[t] = position

        return signals


# ---------------------------------------------------------------------------
# Registry / orchestrator
# ---------------------------------------------------------------------------


class Strategies:
    """
    Registry that holds strategy instances and runs them against data.
    Keeps individual strategy logic decoupled from execution/combination
    logic - add a new strategy by instantiating it and calling `register`.
    """

    def __init__(self):
        self._strategies: dict[str, Strategy] = {}

    def register(self, strategy: Strategy):
        self._strategies[strategy.name] = strategy
        return self  # allow chaining

    def run(self, name: str, data: pd.DataFrame):
        if name not in self._strategies:
            raise KeyError(
                f"No strategy registered under '{name}'. "
                f"Available: {list(self._strategies)}"
            )
        return self._strategies[name].generate_signals(data)

    def run_all(self, data: pd.DataFrame) -> pd.DataFrame:
        """Run every registered strategy on the same data and return a
        DataFrame of signals, one column per strategy."""
        out = {}
        for name, strat in self._strategies.items():
            try:
                out[name] = strat.generate_signals(data)
            except Exception as e:
                print(f"[{name}] failed: {e}")
        return pd.DataFrame(out)

    def combine(self, data: pd.DataFrame, weights: dict = None) -> pd.Series:
        """
        Simple ensemble: weighted average of each strategy's signal,
        then sign() to get a final discrete position. Equal-weighted
        by default.
        """
        signals = self.run_all(data)
        if weights is None:
            weights = {name: 1.0 for name in signals.columns}

        weighted = sum(
            signals[name] * w for name, w in weights.items() if name in signals.columns
        )
        return np.sign(weighted)

    def __repr__(self):
        return f"Strategies({list(self._strategies)})"


# ---------------------------------------------------------------------------
# Example usage
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # dummy price data

    strategies = Strategies()
    strategies.register(MeanReversionStrategy(lookback=20, entry_z=2.0))
    # register stat-arb / ML strategies similarly once you have paired
    # data or a fitted model, e.g.:
    # strategies.register(StatArbCointegrationStrategy("close_a", "close_b"))
    # strategies.register(MLStrategy(model=my_fitted_model, feature_cols=[...]))

    print(strategies)
    signals = strategies.run("mean_reversion", df)
    print(signals.tail())
