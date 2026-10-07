# backend/app/analytics/user_strategies/indicators.py
"""
Shared indicator utilities for user strategies: OU mean-reversion
calibration and volatility estimation.
"""

import numpy as np
import pandas as pd


class OUCalibrator:
    """Calibrate Ornstein-Uhlenbeck parameters from a price series.

    The OU process models mean reversion:
        dX_t = theta * (mu - X_t) * dt  +  sigma * dW_t
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
    def zscore(
        log_price: float,
        mu: float,
        sigma: float,
        theta: float,
        sample_std: float = None,
    ):
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

    @staticmethod
    def half_life(series: np.ndarray, dt: float = 1.0) -> float:
        """Estimate mean-reversion half-life (in bars) from an Ornstein-Uhlenbeck fit.

        tau = ln(2) / theta
        Returns np.nan if series is non-stationary or exploding (theta <= 0).
        """
        arr = np.asarray(series, dtype=float)
        arr = arr[~np.isnan(arr)]
        if len(arr) < 10:
            return np.nan
        dx = np.diff(arr)
        x_lag = arr[:-1]
        X = np.column_stack([np.ones(len(x_lag)), x_lag])
        try:
            coeffs, _, _, _ = np.linalg.lstsq(X, dx, rcond=None)
            theta = -coeffs[1] / dt
            if theta > 1e-6:
                return float(np.log(2.0) / theta)
            return np.nan
        except Exception:
            return np.nan


def compute_adf_stat(series: np.ndarray, lags: int = 1) -> float:
    """Calculate Augmented Dickey-Fuller (ADF) t-statistic.

    Tests null hypothesis H0: series has a unit root (non-stationary).
    A more negative t-statistic indicates stronger rejection of unit root
    (e.g., t < -2.86 implies stationary at 5% significance level).
    """
    arr = np.asarray(series, dtype=float)
    arr = arr[~np.isnan(arr)]
    n = len(arr)
    if n < lags + 12:
        return 0.0

    dy = np.diff(arr)
    Y = dy[lags:]
    k = len(Y)

    X_cols = [np.ones(k), arr[lags : lags + k]]
    for lag in range(1, lags + 1):
        X_cols.append(dy[lags - lag : lags - lag + k])

    X = np.column_stack(X_cols)
    try:
        beta, _, _, _ = np.linalg.lstsq(X, Y, rcond=None)
        residuals = Y - X @ beta
        dof = max(k - X.shape[1], 1)
        sigma2 = np.sum(residuals**2) / dof
        cov = sigma2 * np.linalg.inv(X.T @ X)
        se_gamma = np.sqrt(max(cov[1, 1], 1e-12))
        return float(beta[1] / se_gamma)
    except Exception:
        return 0.0


class KalmanPairEstimator:
    """Online 2-state Kalman Filter for cointegrated pair tracking.

    Estimates dynamic alpha_t (intercept) and beta_t (hedge ratio):
        y_t = alpha_t + beta_t * x_t + epsilon_t
    Smoothly updates each bar without fixed lookback window cutoff lag.
    """

    def __init__(self, delta: float = 1e-4, R: float = 1e-3):
        self.delta = float(delta)  # process noise variance
        self.R = float(R)          # measurement noise variance

    def fit_series(
        self, y: np.ndarray, x: np.ndarray
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """Process price histories and return (alpha, beta, spread, spread_std, zscore)."""
        y_arr = np.asarray(y, dtype=float)
        x_arr = np.asarray(x, dtype=float)
        n = len(y_arr)

        alpha = np.zeros(n)
        beta = np.zeros(n)
        spread = np.zeros(n)
        spread_std = np.zeros(n)

        # State vector [alpha, beta]
        theta = np.zeros(2)
        P = np.eye(2) * 1.0
        Q = np.eye(2) * self.delta

        for t in range(n):
            if np.isnan(y_arr[t]) or np.isnan(x_arr[t]):
                alpha[t] = theta[0]
                beta[t] = theta[1]
                continue

            H = np.array([1.0, x_arr[t]])
            y_hat = H @ theta
            error = y_arr[t] - y_hat
            F = H @ P @ H.T + self.R

            K = (P @ H.T) / F
            theta = theta + K * error
            P = (np.eye(2) - np.outer(K, H)) @ P + Q

            alpha[t] = theta[0]
            beta[t] = theta[1]
            spread[t] = error
            spread_std[t] = np.sqrt(max(F, 1e-8))

        zscore = np.where(spread_std > 1e-8, spread / spread_std, 0.0)
        return alpha, beta, spread, spread_std, zscore


class VolatilityModel:
    """EWMA and simplified GARCH(1,1) volatility estimation."""

    @staticmethod
    def ewma(returns: pd.Series, span: int = 20) -> pd.Series:
        return returns.ewm(span=span, min_periods=max(span // 2, 2)).std()

    @staticmethod
    def garch_forecast(
        returns: np.ndarray, alpha: float = 0.10, beta: float = 0.85
    ) -> float:
        if len(returns) < 5:
            return float(np.std(returns)) if len(returns) > 0 else 0.01

        var_unc = float(np.var(returns))
        omega = var_unc * max(1.0 - alpha - beta, 0.01)

        sigma2 = var_unc
        for r in returns:
            sigma2 = omega + alpha * r * r + beta * sigma2

        return np.sqrt(sigma2)


def compute_ou_features(df: pd.DataFrame, lookback: int = 100) -> pd.DataFrame:
    """Add ou_zscore / ou_theta columns to df if not already present."""
    if "ou_zscore" in df.columns:
        return df

    calibrator = OUCalibrator()
    close = df["close"]
    log_close = np.log(close.where(close > 0, np.nan))
    zscores = np.full(len(df), np.nan)
    thetas = np.full(len(df), np.nan)

    for i in range(lookback, len(df)):
        win = log_close.iloc[i - lookback : i + 1].dropna().values
        if len(win) < 20:
            continue
        theta, mu, sigma = calibrator.calibrate(win)
        zscores[i] = calibrator.zscore(
            log_close.iloc[i], mu, sigma, theta, sample_std=np.std(win)
        )
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
