"""
Shared indicator utilities used by every strategy: OU mean-reversion
calibration and volatility estimation. Extracted from strategies.py so
static strategies, the RL tuner, and the live runner all share one
implementation instead of duplicating it.
"""

import numpy as np
import pandas as pd


class OUCalibrator:
    """Calibrate Ornstein-Uhlenbeck parameters from a price series.

    The OU process models mean reversion:
        dX_t = theta * (mu - X_t) * dt  +  sigma * dW_t

    Parameters are estimated via OLS on first-differences of log-prices.
    """

    @staticmethod
    def calibrate(log_prices: np.ndarray, dt: float = 1.0):
        n = len(log_prices)
        if n < 5:
            return 0.0, log_prices[-1] if n else 0.0, 0.01

        dx = np.diff(log_prices)
        x_lag = log_prices[:-1]

        X_design = np.column_stack([np.ones(len(x_lag)), x_lag])
        try:
            coeffs, _, _, _ = np.linalg.lstsq(X_design, dx, rcond=None)
        except np.linalg.LinAlgError:
            return 0.0, np.mean(log_prices), 0.01

        a, b = coeffs
        theta = max(-b / dt, 1e-8)
        mu = a / (theta * dt) if theta > 1e-8 else np.mean(log_prices)
        residual_std = np.std(dx - X_design @ coeffs)
        sigma = residual_std / np.sqrt(dt) if dt > 0 else residual_std

        return theta, mu, sigma

    @staticmethod
    def zscore(log_price: float, mu: float, sigma: float, theta: float, sample_std: float = None):
        if sample_std is not None and sample_std > 1e-8:
            if theta > 0.05:
                eq_std = sigma / np.sqrt(2 * theta)
                eq_std = np.clip(eq_std, 0.3 * sample_std, 3.0 * sample_std)
            else:
                eq_std = sample_std
        else:
            eq_std = sigma / np.sqrt(2 * theta) if theta > 1e-8 else sigma

        if eq_std < 1e-10:
            return 0.0
        return (log_price - mu) / eq_std


class VolatilityModel:
    """EWMA and simplified GARCH(1,1) volatility estimation."""

    @staticmethod
    def ewma(returns: pd.Series, span: int = 20) -> pd.Series:
        return returns.ewm(span=span, min_periods=max(span // 2, 2)).std()

    @staticmethod
    def garch_forecast(returns: np.ndarray, alpha: float = 0.10, beta: float = 0.85) -> float:
        if len(returns) < 5:
            return float(np.std(returns)) if len(returns) > 0 else 0.01

        var_unc = float(np.var(returns))
        omega = var_unc * max(1.0 - alpha - beta, 0.01)

        sigma2 = var_unc
        for r in returns:
            sigma2 = omega + alpha * r * r + beta * sigma2

        return np.sqrt(sigma2)


def compute_ou_features(df: pd.DataFrame, lookback: int = 100) -> pd.DataFrame:
    """Add ou_zscore / ou_theta columns to df if not already present.
    Shared by every strategy's generate_signals() backtest path."""
    if "ou_zscore" in df.columns:
        return df

    calibrator = OUCalibrator()
    close = df["close"]
    log_close = np.log(close.where(close > 0, np.nan))
    zscores = np.full(len(df), np.nan)
    thetas = np.full(len(df), np.nan)

    for i in range(lookback, len(df)):
        win = log_close.iloc[i - lookback: i + 1].dropna().values
        if len(win) < 20:
            continue
        theta, mu, sigma = calibrator.calibrate(win)
        zscores[i] = calibrator.zscore(log_close.iloc[i], mu, sigma, theta, sample_std=np.std(win))
        thetas[i] = theta

    df["ou_zscore"] = zscores
    df["ou_theta"] = thetas
    return df


def compute_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gains = delta.clip(lower=0).rolling(period).mean()
    losses = (-delta.clip(upper=0)).rolling(period).mean()
    relative_strength = gains / losses.where(losses != 0)
    return 100 - (100 / (1 + relative_strength))


def compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high = df["high"] if "high" in df.columns else df["close"]
    low = df["low"] if "low" in df.columns else df["close"]
    close = df["close"]
    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.rolling(period, min_periods=1).mean()


def compute_bollinger_features(
    df: pd.DataFrame, period: int = 20, std_mult: float = 2.0
) -> pd.DataFrame:
    df = df.copy()
    close = df["close"]
    middle = close.rolling(period, min_periods=1).mean()
    std = close.rolling(period, min_periods=1).std().fillna(0.0)
    upper = middle + std_mult * std
    lower = middle - std_mult * std
    band_range = (upper - lower).replace(0, np.nan)
    percent_b = (close - lower) / band_range
    bandwidth = band_range / middle.replace(0, np.nan)

    bw_mean = bandwidth.rolling(100, min_periods=20).mean()
    bw_std = bandwidth.rolling(100, min_periods=20).std().replace(0, np.nan)
    bandwidth_zscore = (bandwidth - bw_mean) / bw_std

    df["bb_middle"] = middle
    df["bb_upper"] = upper
    df["bb_lower"] = lower
    df["bb_percent_b"] = percent_b.fillna(0.5)
    df["bb_bandwidth"] = bandwidth.fillna(0.0)
    df["bb_bandwidth_zscore"] = bandwidth_zscore.fillna(0.0)
    return df


def compute_volatility_regime(df: pd.DataFrame) -> pd.Series:
    """
    Classify market volatility regimes:
    - SQUEEZE: Low volatility (bandwidth zscore < -1.0)
    - EXPANDING: Volatility expanding rapidly (ATR / ATR_ma > 1.15)
    - EXHAUSTION: EWMA Volatility spike (> 2.2x MA)
    - STABLE: Normal ranging conditions
    """
    if "bb_bandwidth_zscore" not in df.columns:
        df = compute_bollinger_features(df)
    if "atr_14" not in df.columns:
        df["atr_14"] = compute_atr(df)
    if "ewma_vol" not in df.columns:
        df["ewma_vol"] = VolatilityModel.ewma(df["close"].pct_change(), span=20)

    bw_z = df.get("bb_bandwidth_zscore", pd.Series(0.0, index=df.index))
    atr = df.get("atr_14", pd.Series(0.0, index=df.index))
    atr_ma = atr.rolling(20, min_periods=5).mean().replace(0, np.nan)
    atr_ratio = (atr / atr_ma).fillna(1.0)

    ewma_vol = df.get("ewma_vol", pd.Series(0.0, index=df.index))
    ewma_vol_ma = ewma_vol.rolling(50, min_periods=10).mean().replace(0, np.nan)
    vol_spike_ratio = (ewma_vol / ewma_vol_ma).fillna(1.0)

    regime = pd.Series("STABLE", index=df.index)
    regime[bw_z < -1.0] = "SQUEEZE"
    regime[atr_ratio > 1.15] = "EXPANDING"
    regime[vol_spike_ratio > 2.2] = "EXHAUSTION"
    return regime

