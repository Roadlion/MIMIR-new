"""
Data fetching module.

Handles all interaction with the MetaTrader 5 terminal: connecting,
selecting symbols, and pulling historical OHLCV bars.
"""

try:
    import MetaTrader5 as mt5
    HAS_MT5 = True
except ImportError:
    mt5 = None
    HAS_MT5 = False

import pandas as pd
import numpy as np
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
    if not HAS_MT5:
        print(f"[{symbol}] MetaTrader5 module not installed; cannot fetch live broker data.")
        return None

    rates = mt5.copy_rates_range(symbol, timeframe, start_date, end_date)

    if rates is None or len(rates) == 0:
        print(f"[{symbol}] No data found or error: {mt5.last_error()}")
        return None


    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s")
    df["symbol"] = symbol
    df.rename(columns={"tick_volume": "volume"}, inplace=True)

    print(
        f"[{symbol}] Successfully pulled {len(df)} bars "
        f"({df['time'].iloc[0]} -> {df['time'].iloc[-1]})."
    )

    return df

def add_features(df):
    """Add leakage-safe, bar-level indicators available to MLStrategy.

    All values at timestamp ``t`` use data available no later than ``t``.
    Select any of these column names through ``MLStrategy(feature_cols=[...])``.
    """
    df = df.copy()
    close = df["close"]
    returns = close.pct_change()

    df["return_1"] = returns
    df["ma_ratio_10"] = close / close.rolling(10).mean() - 1
    df["volatility_20"] = returns.rolling(20).std()

    # Bollinger features: percent_b is 0 at the lower band and 1 at the
    # upper band. bb_reversal_score is positive near/below the lower band and
    # negative near/above the upper band; PPO can learn whether to act on it.
    bb_middle = close.rolling(20).mean()
    bb_std = close.rolling(20).std()
    df["bb_upper_20_2"] = bb_middle + 2 * bb_std
    df["bb_lower_20_2"] = bb_middle - 2 * bb_std
    band_range = (df["bb_upper_20_2"] - df["bb_lower_20_2"]).where(lambda value: value != 0)
    df["bb_percent_b_20_2"] = (close - df["bb_lower_20_2"]) / band_range
    df["bb_reversal_score"] = (1 - 2 * df["bb_percent_b_20_2"]).clip(-1, 1)
    df["bb_bandwidth_20_2"] = band_range / bb_middle.where(bb_middle != 0)

    bw_mean = df["bb_bandwidth_20_2"].rolling(100, min_periods=20).mean()
    bw_std = df["bb_bandwidth_20_2"].rolling(100, min_periods=20).std().replace(0, np.nan)
    df["bb_bandwidth_zscore"] = (df["bb_bandwidth_20_2"] - bw_mean) / bw_std

    delta = close.diff()
    gains = delta.clip(lower=0).rolling(14).mean()
    losses = (-delta.clip(upper=0)).rolling(14).mean()
    relative_strength = gains / losses.where(losses != 0)
    df["rsi_14"] = 100 - (100 / (1 + relative_strength))

    # --- ATR calculation ---
    high = df["high"] if "high" in df.columns else close
    low = df["low"] if "low" in df.columns else close
    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    df["atr_14"] = tr.rolling(14, min_periods=1).mean()
    atr_ma = df["atr_14"].rolling(20, min_periods=5).mean().replace(0, np.nan)
    df["atr_ratio"] = (df["atr_14"] / atr_ma).fillna(1.0)

    volume_mean = df["volume"].rolling(20).mean()
    volume_std = df["volume"].rolling(20).std().where(lambda value: value != 0)
    df["volume_zscore_20"] = (df["volume"] - volume_mean) / volume_std

    # --- OU mean-reversion features ---
    # Rolling Ornstein-Uhlenbeck calibration for Z-score and reversion speed.
    ou_lookback = 100
    log_close = np.log(close.where(close > 0, np.nan))

    ou_zscores = np.full(len(close), np.nan)
    ou_thetas = np.full(len(close), np.nan)

    for i in range(ou_lookback, len(close)):
        window = log_close.iloc[i - ou_lookback : i + 1].dropna()
        if len(window) < 20:
            continue
        dx = np.diff(window.values)
        x_lag = window.values[:-1]
        X_design = np.column_stack([np.ones(len(x_lag)), x_lag])
        try:
            coeffs, _, _, _ = np.linalg.lstsq(X_design, dx, rcond=None)
        except np.linalg.LinAlgError:
            continue
        a, b = coeffs
        theta = max(-b, 1e-8)
        mu = a / theta if theta > 1e-8 else window.mean()
        sigma = np.std(dx - X_design @ coeffs)
        sample_std = np.std(window.values)
        if theta > 0.05:
            eq_std = sigma / np.sqrt(2 * theta)
            eq_std = np.clip(eq_std, 0.3 * sample_std, 3.0 * sample_std)
        else:
            eq_std = sample_std
        if eq_std > 1e-10:
            ou_zscores[i] = (log_close.iloc[i] - mu) / eq_std
        ou_thetas[i] = theta

    df["ou_zscore"] = ou_zscores
    df["ou_theta"] = ou_thetas
    df["zscore_velocity"] = pd.Series(ou_zscores).diff().values
    df["ewma_vol"] = returns.ewm(span=20, min_periods=10).std()

    # --- Volatility Regime Label ---
    ewma_vol_ma = df["ewma_vol"].rolling(50, min_periods=10).mean().replace(0, np.nan)
    vol_spike_ratio = (df["ewma_vol"] / ewma_vol_ma).fillna(1.0)

    regime = pd.Series("STABLE", index=df.index)
    regime[df["bb_bandwidth_zscore"] < -1.0] = "SQUEEZE"
    regime[df["atr_ratio"] > 1.15] = "EXPANDING"
    regime[vol_spike_ratio > 2.2] = "EXHAUSTION"
    df["volatility_regime"] = regime

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
