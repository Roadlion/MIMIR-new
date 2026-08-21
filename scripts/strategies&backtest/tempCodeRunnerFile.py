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
from time import perf_counter
import os
import pandas as pd
import numpy as np
from sklearn.ensemble import GradientBoostingClassifier

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
# Utility: Ornstein-Uhlenbeck Calibration
# ---------------------------------------------------------------------------


class OUCalibrator:
    """Calibrate Ornstein-Uhlenbeck parameters from a price series.

    The OU process models mean reversion:
        dX_t = theta * (mu - X_t) * dt  +  sigma * dW_t

    Parameters are estimated via OLS on first-differences of log-prices.
    """

    @staticmethod
    def calibrate(log_prices: np.ndarray, dt: float = 1.0):
        """Estimate (theta, mu, sigma) from a window of log-prices.

        Returns:
            theta  – speed of mean reversion (>0)
            mu     – long-run mean (in log-price space)
            sigma  – diffusion coefficient
        """
        n = len(log_prices)
        if n < 5:
            return 0.0, log_prices[-1] if n else 0.0, 0.01

        dx = np.diff(log_prices)
        x_lag = log_prices[:-1]

        # dX = a + b * X_{t-1}  →  b ≈ -theta*dt, a ≈ theta*mu*dt
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
        """How many equilibrium std-devs the current price is from the mean."""
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


# ---------------------------------------------------------------------------
# Utility: Volatility Models (EWMA + GARCH)
# ---------------------------------------------------------------------------


class VolatilityModel:
    """EWMA and simplified GARCH(1,1) volatility estimation."""

    @staticmethod
    def ewma(returns: pd.Series, span: int = 20) -> pd.Series:
        """Exponentially-weighted standard deviation of returns."""
        return returns.ewm(span=span, min_periods=max(span // 2, 2)).std()

    @staticmethod
    def garch_forecast(returns: np.ndarray,
                       alpha: float = 0.10,
                       beta: float = 0.85) -> float:
        """One-step-ahead GARCH(1,1) volatility forecast.

        σ²_t = ω + α·r²_{t-1} + β·σ²_{t-1}
        ω is set so the unconditional variance matches the sample.
        """
        if len(returns) < 5:
            return float(np.std(returns)) if len(returns) > 0 else 0.01

        var_unc = float(np.var(returns))
        omega = var_unc * max(1.0 - alpha - beta, 0.01)

        sigma2 = var_unc
        for r in returns:
            sigma2 = omega + alpha * r * r + beta * sigma2

        return np.sqrt(sigma2)


# ---------------------------------------------------------------------------
# Mean Reversion + ML Strategy
# ---------------------------------------------------------------------------


class MeanReversionMLStrategy(Strategy):
    """Hybrid mean-reversion strategy.

    1. **Alpha** – OU Z-score flags when price is far from equilibrium.
    2. **ML filter** – GradientBoosting classifier estimates the
       probability that a reversion trade will be profitable.
    3. **Entry** – only when |Z| > *zscore_entry* AND P(win) > *ml_confidence*.
    4. **Exit** – take-profit when Z reverts past *zscore_exit*,
       volatility-adaptive stop-loss via GARCH forecast,
       and a hard capital-guard.
    """

    name: str = "mean_reversion_ml"

    # Feature columns expected by the ML model.
    _FEATURE_COLS = [
        "ou_zscore", "abs_zscore", "zscore_velocity", "ou_theta",
        "ewma_vol", "rsi_14", "volume_zscore_20", "return_1",
        "bb_percent_b_20_2",
    ]

    def __init__(
        self,
        ou_lookback: int = 100,
        zscore_entry: float = 1.5,
        zscore_exit: float = 0.0,
        zscore_flip: float = 2.5,
        ml_confidence: float = 0.50,
        vol_stop_mult: float = 2.0,
        max_risk_pct: float = 0.02,
        train_ratio: float = 0.25,
        retrain_interval: int = 200,
        ewma_span: int = 20,
        label_horizon: int = 5,
    ):
        self.ou_lookback = ou_lookback
        self.zscore_entry = zscore_entry
        self.zscore_exit = zscore_exit
        self.zscore_flip = zscore_flip
        self.ml_confidence = ml_confidence
        self.vol_stop_mult = vol_stop_mult
        self.max_risk_pct = max_risk_pct
        self.train_ratio = train_ratio
        self.retrain_interval = retrain_interval
        self.ewma_span = ewma_span
        self.label_horizon = label_horizon

        self._model: GradientBoostingClassifier | None = None
        self._calibrator = OUCalibrator()
        self._vol = VolatilityModel()

    # ----- feature engineering -----

    def _ensure_features(self, df: pd.DataFrame) -> pd.DataFrame:
        """Make sure all required columns exist.

        If the pipeline already called ``add_features()`` the OU columns
        are present.  For Monte-Carlo / standalone usage we compute them
        here so the strategy is self-contained.
        """
        close = df["close"]
        returns = close.pct_change()

        # OU features (skip if already present from data_fetching)
        if "ou_zscore" not in df.columns:
            log_close = np.log(close.where(close > 0, np.nan))
            zscores = np.full(len(close), np.nan)
            thetas = np.full(len(close), np.nan)

            for i in range(self.ou_lookback, len(close)):
                win = log_close.iloc[i - self.ou_lookback : i + 1].dropna().values
                if len(win) < 20:
                    continue
                theta, mu, sigma = self._calibrator.calibrate(win)
                zscores[i] = self._calibrator.zscore(
                    log_close.iloc[i], mu, sigma, theta, sample_std=np.std(win)
                )
                thetas[i] = theta

            df["ou_zscore"] = zscores
            df["ou_theta"] = thetas

        if "zscore_velocity" not in df.columns:
            df["zscore_velocity"] = pd.Series(df["ou_zscore"].values).diff().values

        if "ewma_vol" not in df.columns:
            df["ewma_vol"] = self._vol.ewma(returns, span=self.ewma_span)

        # Derived
        df["abs_zscore"] = np.abs(df["ou_zscore"])

        # Standard indicators the pipeline normally provides
        if "return_1" not in df.columns:
            df["return_1"] = returns

        if "rsi_14" not in df.columns:
            delta = close.diff()
            gains = delta.clip(lower=0).rolling(14).mean()
            losses = (-delta.clip(upper=0)).rolling(14).mean()
            rs = gains / losses.where(losses != 0)
            df["rsi_14"] = 100 - (100 / (1 + rs))

        if "volume_zscore_20" not in df.columns:
            if "volume" in df.columns:
                vol_mean = df["volume"].rolling(20).mean()
                vol_std = df["volume"].rolling(20).std().where(lambda v: v != 0)
                df["volume_zscore_20"] = (df["volume"] - vol_mean) / vol_std
            else:
                df["volume_zscore_20"] = 0.0

        if "bb_percent_b_20_2" not in df.columns:
            bb_mid = close.rolling(20).mean()
            bb_std = close.rolling(20).std()
            bb_upper = bb_mid + 2 * bb_std
            bb_lower = bb_mid - 2 * bb_std
            band_range = (bb_upper - bb_lower).where(lambda v: v != 0)
            df["bb_percent_b_20_2"] = (close - bb_lower) / band_range

        return df

    # ----- labelling -----

    def _create_labels(self, df: pd.DataFrame) -> pd.Series:
        """Label each bar 1 if a reversion trade over the next *horizon*
        bars would have been profitable, else 0.
        """
        future_ret = df["close"].pct_change(self.label_horizon).shift(
            -self.label_horizon
        )
        z = df["ou_zscore"]

        label = pd.Series(0, index=df.index, dtype=int)

        long_mask = z < -self.zscore_entry
        label[long_mask & (future_ret > 0)] = 1

        short_mask = z > self.zscore_entry
        label[short_mask & (future_ret < 0)] = 1

        return label

    # ----- ML training / inference -----

    def _train(self, X: pd.DataFrame, y: pd.Series) -> None:
        valid = X.notna().all(axis=1) & y.notna()
        X, y = X[valid], y[valid]
        if len(X) < 30 or y.nunique() < 2:
            self._model = None
            return
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            clf = GradientBoostingClassifier(
                n_estimators=100,
                max_depth=4,
                learning_rate=0.1,
                subsample=0.8,
                min_samples_leaf=10,
                random_state=42,
            )
            clf.fit(X, y)
        self._model = clf

    def _predict_proba(self, row: pd.DataFrame) -> float:
        if self._model is None:
            return 0.5
        if row.isna().any(axis=1).iloc[0]:
            return 0.5
        try:
            p = self._model.predict_proba(row)[0]
            return p[1] if len(p) >= 2 else p[0]
        except Exception:
            return 0.5

    # ----- main signal loop -----

    def generate_signals(self, data: pd.DataFrame) -> pd.Series:
        t0 = perf_counter()
        df = data.copy()
        df = self._ensure_features(df)

        feat_cols = [c for c in self._FEATURE_COLS if c in df.columns]
        labels = self._create_labels(df)

        n = len(df)
        train_end = max(int(n * self.train_ratio), self.ou_lookback + 50)
        if train_end >= n - 10:
            print("[MeanReversionML] Data too short for walk-forward; "
                  "falling back to pure Z-score.")
            return self._pure_zscore(df)

        signals = pd.Series(0.0, index=df.index)

        # Initial training
        self._train(df.iloc[:train_end][feat_cols], labels.iloc[:train_end])
        print(f"[MeanReversionML] Initial ML train on bars 0–{train_end} "
              f"({'model OK' if self._model else 'no model'})")

        position = 0.0
        entry_price = 0.0
        entry_vol = 0.0
        bars_since_train = 0

        for i in range(train_end, n):
            z = df["ou_zscore"].iloc[i]
            price = df["close"].iloc[i]

            # Periodic re-training
            bars_since_train += 1
            if bars_since_train >= self.retrain_interval:
                self._train(df.iloc[:i][feat_cols], labels.iloc[:i])
                bars_since_train = 0

            # Skip if Z-score is NaN (insufficient OU data)
            if np.isnan(z):
                signals.iloc[i] = position
                continue

            # ---------- EXIT & FLIP LOGIC ----------
            if position != 0:
                # 1. POSITION FLIP (Stop & Reverse into Trend-Following Mode)
                # If mean reversion failed and price continues breaking down past zscore_flip,
                # flip position to follow the trend instead of holding a losing mean-reversion trade!
                if position > 0 and z < -self.zscore_flip:
                    position = -1.0  # FLIP LONG -> SHORT (Breakout trend following)
                    entry_price = price
                    lookback = df["return_1"].iloc[max(0, i - 50) : i].dropna().values
                    entry_vol = self._vol.garch_forecast(lookback)
                elif position < 0 and z > self.zscore_flip:
                    position = 1.0   # FLIP SHORT -> LONG (Breakout trend following)
                    entry_price = price
                    lookback = df["return_1"].iloc[max(0, i - 50) : i].dropna().values
                    entry_vol = self._vol.garch_forecast(lookback)

                # 2. Take profit: Z reverts past target
                elif position > 0 and z >= self.zscore_exit:
                    position = 0.0
                elif position < 0 and z <= -self.zscore_exit:
                    position = 0.0

                # 3. Volatility-adaptive stop-loss
                elif entry_price > 0 and entry_vol > 0:
                    stop_dist = self.vol_stop_mult * entry_vol * entry_price
                    pnl = (price - entry_price) * np.sign(position)
                    if pnl < -stop_dist:
                        position = 0.0

                # 4. Capital guard
                elif entry_price > 0:
                    loss_pct = abs(price - entry_price) / entry_price
                    if (price - entry_price) * np.sign(position) < 0:
                        if loss_pct > self.max_risk_pct:
                            position = 0.0

            # ---------- ENTRY LOGIC ----------
            if position == 0 and abs(z) > self.zscore_entry:
                row = df[feat_cols].iloc[[i]]
                prob = self._predict_proba(row)

                if prob >= self.ml_confidence:
                    # Kelly-inspired sizing: map probability to [0.5, 1.0]
                    size = min(1.0, max(0.5, 2.0 * (prob - 0.5)))

                    if z < -self.zscore_entry:
                        position = size       # LONG
                    else:
                        position = -size      # SHORT

                    entry_price = price
                    lookback = df["return_1"].iloc[max(0, i - 50) : i].dropna().values
                    entry_vol = self._vol.garch_forecast(lookback)

            signals.iloc[i] = position

        # Summary
        changes = signals.diff().abs() > 0
        elapsed = perf_counter() - t0
        print(f"[MeanReversionML] Done in {elapsed:.1f}s — "
              f"{changes.sum()} position changes, "
              f"long bars {(signals > 0).sum()}, "
              f"short bars {(signals < 0).sum()}, "
              f"flat bars {(signals == 0).sum()}")
        return signals

    # ----- fallback -----

    def _pure_zscore(self, df: pd.DataFrame) -> pd.Series:
        """Simple threshold-based signals when ML training is not possible."""
        z = df["ou_zscore"]
        signals = pd.Series(0.0, index=df.index)
        signals[z < -self.zscore_entry] = 1.0
        signals[z > self.zscore_entry] = -1.0
        return signals


# ---------------------------------------------------------------------------
# Dual-Leg Hedging & Winner Selection Strategy
# ---------------------------------------------------------------------------


class DualHedgeSelectionStrategy(Strategy):
    """
    Bi-Directional Dual-Leg Hedged Strategy.

    Logic:
    1. Initial Entry: When Z-score signal triggers (e.g. Z < -1.5 -> LONG).
    2. Hedge Trigger: If initial position starts losing (loss > hedge_trigger_loss_pct),
       open a counter SHORT leg simultaneously so both legs run side-by-side.
    3. Winner Selection: Monitor PnL of both legs. When one leg demonstrates net profit
       and the other leg breaches loss threshold:
       - CUT the losing leg immediately to cap its drawdown.
       - KEEP the winning leg active with a trailing stop to capture the breakout/trend movement!
    """

    name: str = "dual_hedge"

    def __init__(
        self,
        ou_lookback: int = 100,
        zscore_entry: float = 1.5,
        hedge_trigger_loss_pct: float = 0.005,  # 0.5% loss triggers counter hedge
        cut_losing_leg_pct: float = 0.010,       # 1.0% loss cuts the losing leg
        winning_leg_profit_pct: float = 0.005,   # 0.5% profit confirms winning leg
        trailing_stop_pct: float = 0.008,        # 0.8% trailing stop for survivor
        take_profit_pct: float = 0.015,          # 1.5% take profit for initial trade
    ):
        self.ou_lookback = ou_lookback
        self.zscore_entry = zscore_entry
        self.hedge_trigger_loss_pct = hedge_trigger_loss_pct
        self.cut_losing_leg_pct = cut_losing_leg_pct
        self.winning_leg_profit_pct = winning_leg_profit_pct
        self.trailing_stop_pct = trailing_stop_pct
        self.take_profit_pct = take_profit_pct
        self._calibrator = OUCalibrator()

    def generate_signals(self, data: pd.DataFrame) -> pd.Series:
        df = data.copy()
        close = df["close"]
        n = len(close)

        # Compute Z-score if not present
        if "ou_zscore" not in df.columns:
            log_close = np.log(close.where(close > 0, np.nan))
            zscores = np.full(n, np.nan)
            for i in range(self.ou_lookback, n):
                win = log_close.iloc[i - self.ou_lookback : i + 1].dropna().values
                if len(win) < 20:
                    continue
                theta, mu, sigma = self._calibrator.calibrate(win)
                zscores[i] = self._calibrator.zscore(
                    log_close.iloc[i], mu, sigma, theta, sample_std=np.std(win)
                )
            df["ou_zscore"] = zscores

        signals = pd.Series(0.0, index=df.index)

        # State tracking
        long_active = False
        long_entry = 0.0
        short_active = False
        short_entry = 0.0
        state = "FLAT"  # "FLAT", "INITIAL_TRADE", "HEDGED_DUAL", "SOLO_SURVIVOR"
        survivor_peak = 0.0

        for i in range(self.ou_lookback, n):
            z = df["ou_zscore"].iloc[i]
            price = close.iloc[i]

            if np.isnan(z) or price <= 0:
                signals.iloc[i] = 0.0
                continue

            # ---------------- 1. FLAT STATE ----------------
            if state == "FLAT":
                if z < -self.zscore_entry:
                    long_active = True
                    long_entry = price
                    state = "INITIAL_TRADE"
                elif z > self.zscore_entry:
                    short_active = True
                    short_entry = price
                    state = "INITIAL_TRADE"

            # ---------------- 2. INITIAL TRADE STATE ----------------
            elif state == "INITIAL_TRADE":
                if long_active:
                    pnl = (price - long_entry) / long_entry
                    # Early Take Profit
                    if pnl >= self.take_profit_pct:
                        long_active = False
                        state = "FLAT"
                    # Initial position losing -> OPEN COUNTER HEDGE LEG!
                    elif pnl <= -self.hedge_trigger_loss_pct:
                        short_active = True
                        short_entry = price
                        state = "HEDGED_DUAL"
                elif short_active:
                    pnl = (short_entry - price) / short_entry
                    # Early Take Profit
                    if pnl >= self.take_profit_pct:
                        short_active = False
                        state = "FLAT"
                    # Initial position losing -> OPEN COUNTER HEDGE LEG!
                    elif pnl <= -self.hedge_trigger_loss_pct:
                        long_active = True
                        long_entry = price
                        state = "HEDGED_DUAL"

            # ---------------- 3. HEDGED DUAL STATE ----------------
            elif state == "HEDGED_DUAL":
                long_pnl = (price - long_entry) / long_entry
                short_pnl = (short_entry - price) / short_entry

                # Rule A: Long is winning, Short is losing -> CUT SHORT, KEEP LONG!
                if long_pnl >= self.winning_leg_profit_pct and short_pnl <= -self.cut_losing_leg_pct:
                    short_active = False  # CUT LOSING SHORT
                    state = "SOLO_SURVIVOR"
                    survivor_peak = price  # Track peak for trailing stop
                # Rule B: Short is winning, Long is losing -> CUT LONG, KEEP SHORT!
                elif short_pnl >= self.winning_leg_profit_pct and long_pnl <= -self.cut_losing_leg_pct:
                    long_active = False  # CUT LOSING LONG
                    state = "SOLO_SURVIVOR"
                    survivor_peak = price  # Track trough for trailing stop
                # Safety Guard: If total hedge loss exceeds threshold, close both
                elif (long_pnl + short_pnl) < -2 * self.cut_losing_leg_pct:
                    long_active = False
                    short_active = False
                    state = "FLAT"

            # ---------------- 4. SOLO SURVIVOR STATE ----------------
            elif state == "SOLO_SURVIVOR":
                if long_active:
                    survivor_peak = max(survivor_peak, price)
                    drawdown = (survivor_peak - price) / survivor_peak
                    # Trailing stop or mean-reversion target reached
                    if drawdown >= self.trailing_stop_pct or z >= 0.0:
                        long_active = False
                        state = "FLAT"
                elif short_active:
                    survivor_peak = min(survivor_peak, price)
                    drawup = (price - survivor_peak) / survivor_peak
                    # Trailing stop or mean-reversion target reached
                    if drawup >= self.trailing_stop_pct or z <= 0.0:
                        short_active = False
                        state = "FLAT"

            # Net position signal
            net_signal = (1.0 if long_active else 0.0) - (1.0 if short_active else 0.0)
            signals.iloc[i] = net_signal

        print(f"[DualHedgeSelection] Done — active long bars: {(signals > 0).sum()}, "
              f"active short bars: {(signals < 0).sum()}, hedged/flat bars: {(signals == 0).sum()}")
        return signals


# ---------------------------------------------------------------------------
# RL Meta-Controller Strategy (Adaptive Parameter Tuner)
# ---------------------------------------------------------------------------

try:
    import gymnasium as gym
    from gymnasium import spaces
    from stable_baselines3 import PPO
    HAS_RL = True
except ImportError:
    HAS_RL = False


if HAS_RL:
    class TradingParamEnv(gym.Env):
        """Custom Gymnasium Environment for RL Meta-Controller parameter tuning
        governing the DualHedgeSelectionStrategy state machine.
        """

        def __init__(self, df: pd.DataFrame, initial_balance: float = 10_000.0):
            super().__init__()
            self.df = df.reset_index(drop=True)
            self.initial_balance = initial_balance
            self.n_bars = len(df)

            # Action space: continuous 6-D Box in [-1, 1]
            # 0: zscore_entry (mapped to [1.0, 2.5])
            # 1: hedge_trigger_loss_pct (mapped to [0.002, 0.012])
            # 2: cut_losing_leg_pct (mapped to [0.005, 0.020])
            # 3: winning_leg_profit_pct (mapped to [0.003, 0.015])
            # 4: trailing_stop_pct (mapped to [0.004, 0.015])
            # 5: take_profit_pct (mapped to [0.008, 0.030])
            self.action_space = spaces.Box(low=-1.0, high=1.0, shape=(6,), dtype=np.float32)

            # Observation space: 6 features
            # [ou_zscore, ewma_vol, ou_theta, rsi_14, trend_slope, current_drawdown]
            self.observation_space = spaces.Box(low=-np.inf, high=np.inf, shape=(6,), dtype=np.float32)
            self.reset()

        @property
        def position(self) -> float:
            """Net signal position: 1.0 (Long), -1.0 (Short), or 0.0 (Flat/Hedged)."""
            return (1.0 if self.long_active else 0.0) - (1.0 if self.short_active else 0.0)

        def _get_obs(self):
            if self.df.empty:
                raise ValueError("TradingParamEnv requires at least one row of price data")
            if self.current_step >= len(self.df):
                row = self.df.iloc[-1]
            else:
                row = self.df.iloc[self.current_step]
            z = row.get("ou_zscore", 0.0)
            vol = row.get("ewma_vol", 0.01)
            theta = row.get("ou_theta", 0.01)
            rsi = (row.get("rsi_14", 50.0) - 50.0) / 50.0

            p_curr = row["close"]
            p_prev = self.df["close"].iloc[max(0, self.current_step - 20)]
            slope = (p_curr - p_prev) / p_prev if p_prev > 0 else 0.0

            dd = (self.balance - self.peak_balance) / self.peak_balance if self.peak_balance > 0 else 0.0
            obs = np.array([z, vol, theta, rsi, slope, dd], dtype=np.float32)
            return np.nan_to_num(obs, nan=0.0, posinf=0.0, neginf=0.0)

        def reset(self, seed=None, options=None):
            super().reset(seed=seed)
            safe_start = 100 if self.n_bars > 100 else 0
            self.current_step = safe_start
            self.balance = self.initial_balance
            self.peak_balance = self.initial_balance
            self.long_active = False
            self.long_entry = 0.0
            self.short_active = False
            self.short_entry = 0.0
            self.state = "FLAT"  # "FLAT", "INITIAL_TRADE", "HEDGED_DUAL", "SOLO_SURVIVOR"
            self.survivor_peak = 0.0

            obs = self._get_obs()
            return obs, {}

        def step(self, action):
            # Decode dynamic action parameters
            zscore_entry = float(np.interp(action[0], [-1, 1], [1.0, 2.5]))
            hedge_trigger_pct = float(np.interp(action[1], [-1, 1], [0.002, 0.012]))
            cut_losing_pct = float(np.interp(action[2], [-1, 1], [0.005, 0.020]))
            win_profit_pct = float(np.interp(action[3], [-1, 1], [0.003, 0.015]))
            trail_stop_pct = float(np.interp(action[4], [-1, 1], [0.004, 0.015]))
            tp_pct = float(np.interp(action[5], [-1, 1], [0.008, 0.030]))

            price = self.df["close"].iloc[self.current_step]
            prev_price = self.df["close"].iloc[max(0, self.current_step - 1)]
            z = self.df.get("ou_zscore", pd.Series(0.0)).iloc[self.current_step]

            # Current net signal before state update
            net_signal = self.position
            mkt_ret = (price - prev_price) / prev_price if prev_price > 0 else 0.0
            step_return = net_signal * mkt_ret
            self.balance *= (1.0 + step_return)
            self.peak_balance = max(self.peak_balance, self.balance)

            # --- DualHedge State Machine ---
            if not (np.isnan(z) or price <= 0):
                # 1. FLAT STATE
                if self.state == "FLAT":
                    if z < -zscore_entry:
                        self.long_active = True
                        self.long_entry = price
                        self.state = "INITIAL_TRADE"
                    elif z > zscore_entry:
                        self.short_active = True
                        self.short_entry = price
                        self.state = "INITIAL_TRADE"

                # 2. INITIAL TRADE STATE
                elif self.state == "INITIAL_TRADE":
                    if self.long_active:
                        pnl = (price - self.long_entry) / self.long_entry
                        if pnl >= tp_pct:
                            self.long_active = False
                            self.state = "FLAT"
                        elif pnl <= -hedge_trigger_pct:
                            self.short_active = True
                            self.short_entry = price
                            self.state = "HEDGED_DUAL"
                    elif self.short_active:
                        pnl = (self.short_entry - price) / self.short_entry
                        if pnl >= tp_pct:
                            self.short_active = False
                            self.state = "FLAT"
                        elif pnl <= -hedge_trigger_pct:
                            self.long_active = True
                            self.long_entry = price
                            self.state = "HEDGED_DUAL"

                # 3. HEDGED DUAL STATE
                elif self.state == "HEDGED_DUAL":
                    long_pnl = (price - self.long_entry) / self.long_entry
                    short_pnl = (self.short_entry - price) / self.short_entry

                    if long_pnl >= win_profit_pct and short_pnl <= -cut_losing_pct:
                        self.short_active = False  # CUT LOSING SHORT
                        self.state = "SOLO_SURVIVOR"
                        self.survivor_peak = price
                    elif short_pnl >= win_profit_pct and long_pnl <= -cut_losing_pct:
                        self.long_active = False   # CUT LOSING LONG
                        self.state = "SOLO_SURVIVOR"
                        self.survivor_peak = price
                    elif (long_pnl + short_pnl) < -2 * cut_losing_pct:
                        self.long_active = False
                        self.short_active = False
                        self.state = "FLAT"

                # 4. SOLO SURVIVOR STATE
                elif self.state == "SOLO_SURVIVOR":
                    if self.long_active:
                        self.survivor_peak = max(self.survivor_peak, price)
                        drawdown = (self.survivor_peak - price) / self.survivor_peak
                        if drawdown >= trail_stop_pct or z >= 0.0:
                            self.long_active = False
                            self.state = "FLAT"
                    elif self.short_active:
                        self.survivor_peak = min(self.survivor_peak, price)
                        drawup = (price - self.survivor_peak) / self.survivor_peak
                        if drawup >= trail_stop_pct or z <= 0.0:
                            self.short_active = False
                            self.state = "FLAT"

            self.current_step += 1
            terminated = self.current_step >= self.n_bars - 1
            truncated = self.balance <= 0.5 * self.initial_balance

            dd = (self.balance - self.peak_balance) / self.peak_balance if self.peak_balance > 0 else 0.0
            reward = step_return - 0.25 * abs(dd)

            obs = self._get_obs() if not terminated else np.zeros(6, dtype=np.float32)
            return obs, reward, terminated, truncated, {}


class RLAdaptiveStrategy(Strategy):
    """
    RL Meta-Controller Adaptive Dual-Hedge Strategy.

    Uses PPO (Stable-Baselines3) to observe market regime features
    (volatility, OU reversion speed, RSI, trend slope, drawdown)
    and dynamically tunes DualHedge parameters in real time for execution.
    """

    name: str = "rl_adaptive"

    def __init__(self, total_timesteps: int = 25_000, train_ratio: float = 0.50, model_path: str = None):
        self.total_timesteps = total_timesteps
        self.train_ratio = train_ratio
        self._model = None
        self._calibrator = OUCalibrator()
        self._vol = VolatilityModel()
        self.model_path = model_path or os.path.join(os.path.dirname(__file__), "..", "models", "rl_adaptive_ppo.zip")
        if os.path.isfile(self.model_path):
            try:
                from stable_baselines3 import PPO
                self._model = PPO.load(self.model_path)
                print(f"[RLAdaptiveStrategy] Loaded saved PPO model from {self.model_path}")
            except Exception as e:
                print(f"[RLAdaptiveStrategy] Failed to load saved model: {e}")
                self._model = None
        else:
            os.makedirs(os.path.dirname(self.model_path), exist_ok=True)

    def generate_signals(self, data: pd.DataFrame) -> pd.Series:
        if not HAS_RL:
            print("[RLAdaptiveStrategy] gymnasium or stable_baselines3 not installed! Falling back to DualHedge.")
            return DualHedgeSelectionStrategy().generate_signals(data)

        df = data.copy()
        n = len(df)

        if "ou_zscore" not in df.columns:
            close = df["close"]
            log_close = np.log(close.where(close > 0, np.nan))
            zscores = np.full(n, np.nan)
            thetas = np.full(n, np.nan)
            for i in range(100, n):
                win = log_close.iloc[i - 100 : i + 1].dropna().values
                if len(win) < 20:
                    continue
                theta, mu, sigma = self._calibrator.calibrate(win)
                zscores[i] = self._calibrator.zscore(
                    log_close.iloc[i], mu, sigma, theta, sample_std=np.std(win)
                )
                thetas[i] = theta
            df["ou_zscore"] = zscores
            df["ou_theta"] = thetas

        if "ewma_vol" not in df.columns:
            df["ewma_vol"] = self._vol.ewma(df["close"].pct_change(), span=20)

        train_end = int(n * self.train_ratio)
        train_df = df.iloc[:train_end]

        print(f"[RLAdaptiveStrategy] Training PPO Meta-Controller on bars 0–{train_end} ({self.total_timesteps} timesteps)...")

        env = TradingParamEnv(train_df)
        self._model = PPO("MlpPolicy", env, verbose=0, learning_rate=0.0003, n_steps=256)
        self._model.learn(total_timesteps=self.total_timesteps)

        try:
            self._model.save(self.model_path)
            print(f"[RLAdaptiveStrategy] Saved trained PPO model to {self.model_path}")
        except Exception as e:
            print(f"[RLAdaptiveStrategy] Failed to save model: {e}")
        print("[RLAdaptiveStrategy] PPO Training Complete!")

        eval_env = TradingParamEnv(df)
        obs, _ = eval_env.reset()
        signals = pd.Series(0.0, index=df.index)

        safe_start = 100 if n > 100 else 0
        for _ in range(safe_start, n):
            step_idx = eval_env.current_step
            if step_idx >= n - 1:
                break
            action, _ = self._model.predict(obs, deterministic=True)
            obs, reward, term, trunc, _ = eval_env.step(action)
            signals.iloc[step_idx] = eval_env.position
            if term or trunc:
                break

        print(f"[RLAdaptiveStrategy] Done — Long bars: {(signals > 0).sum()}, Short bars: {(signals < 0).sum()}")
        return signals


# ---------------------------------------------------------------------------
# Monte Carlo Validator
# ---------------------------------------------------------------------------


class MonteCarloValidator:
    """Stress-test a strategy against synthetic price paths.

    Three path types:
        * **GBM** (random walk)  – strategy should stay roughly flat.
        * **OU** (mean-reverting) – strategy should capture profit.
        * **Trending** (strong drift) – strategy should not blow up.

    Usage::

        validator = MonteCarloValidator(n_paths=60)
        summary   = validator.run(MeanReversionMLStrategy())
    """

    def __init__(
        self,
        n_paths: int = 60,
        path_length: int = 2000,
        initial_price: float = 100.0,
        dt: float = 1.0 / (252 * 24),   # hourly
    ):
        self.n_paths = n_paths
        self.path_length = path_length
        self.initial_price = initial_price
        self.dt = dt

    # ----- path generators -----

    @staticmethod
    def _gbm(s0, mu, sigma, n, dt, seed=None):
        rng = np.random.default_rng(seed)
        z = rng.standard_normal(n - 1)
        log_ret = (mu - 0.5 * sigma ** 2) * dt + sigma * np.sqrt(dt) * z
        prices = np.empty(n)
        prices[0] = s0
        np.exp(log_ret, out=log_ret)  # in-place
        np.cumprod(log_ret, out=log_ret)
        prices[1:] = s0 * log_ret
        return prices

    @staticmethod
    def _ou(s0, theta, mu_price, sigma, n, dt, seed=None):
        rng = np.random.default_rng(seed)
        log_p = np.empty(n)
        log_p[0] = np.log(s0)
        log_mu = np.log(mu_price) if mu_price > 0 else log_p[0]
        for i in range(1, n):
            dW = rng.standard_normal() * np.sqrt(dt)
            log_p[i] = log_p[i - 1] + theta * (log_mu - log_p[i - 1]) * dt + sigma * dW
        return np.exp(log_p)

    @staticmethod
    def _trending(s0, drift, sigma, n, dt, seed=None):
        rng = np.random.default_rng(seed)
        z = rng.standard_normal(n - 1)
        log_ret = drift * dt + sigma * np.sqrt(dt) * z
        prices = np.empty(n)
        prices[0] = s0
        np.exp(log_ret, out=log_ret)
        np.cumprod(log_ret, out=log_ret)
        prices[1:] = s0 * log_ret
        return prices

    # ----- helpers -----

    def _to_dataframe(self, prices: np.ndarray) -> pd.DataFrame:
        n = len(prices)
        rng = np.random.default_rng(42)
        noise = rng.uniform(0.001, 0.005, n)
        sign = rng.choice([-1.0, 1.0], n)
        return pd.DataFrame({
            "time": pd.date_range("2025-01-01", periods=n, freq="h"),
            "open": prices * (1 + noise * sign * 0.5),
            "high": prices * (1 + noise),
            "low":  prices * (1 - noise),
            "close": prices,
            "volume": rng.integers(100, 10_000, n).astype(float),
            "symbol": "SYNTHETIC",
        })

    def _evaluate(self, strategy: Strategy, prices: np.ndarray) -> dict:
        df = self._to_dataframe(prices)
        try:
            signals = strategy.generate_signals(df)
        except Exception as exc:
            print(f"  ⚠ Strategy error: {exc}")
            return {"total_return": 0.0, "max_drawdown": 0.0,
                    "sharpe": 0.0, "n_trades": 0}

        mkt_ret = df["close"].pct_change()
        strat_ret = signals.shift(1) * mkt_ret
        cum = (1 + strat_ret.fillna(0)).cumprod()

        total_return = float(cum.iloc[-1] - 1) if len(cum) else 0.0
        rolling_max = cum.cummax()
        dd = ((cum - rolling_max) / rolling_max.where(rolling_max != 0)).min()
        max_dd = float(dd) if np.isfinite(dd) else 0.0

        sr_mean = strat_ret.mean()
        sr_std = strat_ret.std()
        # Annualised Sharpe (hourly bars → 252*24 periods / year)
        sharpe = float(sr_mean / sr_std * np.sqrt(252 * 24)) if sr_std > 0 else 0.0

        n_trades = int((signals.diff().abs() > 0).sum())
        return {"total_return": total_return, "max_drawdown": max_dd,
                "sharpe": sharpe, "n_trades": n_trades}

    # ----- public API -----

    def run(self, strategy: Strategy, verbose: bool = True) -> dict:
        """Run strategy across all synthetic paths and return summary."""
        per_type = self.n_paths // 3
        remainder = self.n_paths - 3 * per_type
        results: dict[str, list[dict]] = {"gbm": [], "ou": [], "trending": []}

        if verbose:
            print(f"\n[MonteCarlo] {self.n_paths} paths × "
                  f"{self.path_length} bars each\n")

        # GBM
        if verbose:
            print(f"  GBM (random walk) — {per_type} paths")
        for i in range(per_type):
            prices = self._gbm(self.initial_price, mu=0.0, sigma=0.30,
                               n=self.path_length, dt=self.dt, seed=i)
            results["gbm"].append(self._evaluate(strategy, prices))

        # OU
        if verbose:
            print(f"  OU  (mean-reverting) — {per_type} paths")
        for i in range(per_type):
            prices = self._ou(self.initial_price, theta=5.0,
                              mu_price=self.initial_price, sigma=0.30,
                              n=self.path_length, dt=self.dt, seed=i + 1000)
            results["ou"].append(self._evaluate(strategy, prices))

        # Trending
        n_trend = per_type + remainder
        if verbose:
            print(f"  Trending — {n_trend} paths")
        for i in range(n_trend):
            drift = 0.5 if i % 2 == 0 else -0.5
            prices = self._trending(self.initial_price, drift=drift, sigma=0.20,
                                    n=self.path_length, dt=self.dt, seed=i + 2000)
            results["trending"].append(self._evaluate(strategy, prices))

        summary = self._aggregate(results)
        if verbose:
            self._print(summary)
        return summary

    # ----- reporting -----

    @staticmethod
    def _aggregate(results: dict) -> dict:
        summary = {}
        for ptype, stats_list in results.items():
            if not stats_list:
                continue
            rets = [s["total_return"] for s in stats_list]
            dds = [s["max_drawdown"] for s in stats_list]
            shs = [s["sharpe"] for s in stats_list]
            trs = [s["n_trades"] for s in stats_list]
            summary[ptype] = {
                "n_paths":          len(stats_list),
                "avg_return":       float(np.mean(rets)),
                "median_return":    float(np.median(rets)),
                "std_return":       float(np.std(rets)),
                "win_rate":         float(np.mean([r > 0 for r in rets])),
                "avg_max_drawdown": float(np.mean(dds)),
                "worst_drawdown":   float(np.min(dds)),
                "avg_sharpe":       float(np.mean(shs)),
                "avg_trades":       float(np.mean(trs)),
            }
        return summary

    @staticmethod
    def _print(summary: dict) -> None:
        labels = {"gbm": "GBM (Random Walk)", "ou": "OU (Mean-Reverting)",
                  "trending": "Trending (Directional)"}
        print("\n" + "=" * 70)
        print("  MONTE CARLO STRESS TEST RESULTS")
        print("=" * 70)
        for ptype, s in summary.items():
            print(f"\n{'─' * 70}")
            print(f"  {labels.get(ptype, ptype)}  ({s['n_paths']} paths)")
            print(f"{'─' * 70}")
            print(f"  Avg Return:       {s['avg_return']:>+10.2%}")
            print(f"  Median Return:    {s['median_return']:>+10.2%}")
            print(f"  Std Return:       {s['std_return']:>10.2%}")
            print(f"  Win Rate:         {s['win_rate']:>10.1%}")
            print(f"  Avg Max Drawdown: {s['avg_max_drawdown']:>10.2%}")
            print(f"  Worst Drawdown:   {s['worst_drawdown']:>10.2%}")
            print(f"  Avg Sharpe:       {s['avg_sharpe']:>10.2f}")
            print(f"  Avg Trades:       {s['avg_trades']:>10.0f}")

        print(f"\n{'=' * 70}")
        print("  INTERPRETATION")
        print("=" * 70)
        ou = summary.get("ou")
        gbm = summary.get("gbm")
        trend = summary.get("trending")
        if ou:
            sym = "✅" if ou["avg_return"] > 0 else "⚠️"
            print(f"  {sym} Mean-reverting paths: avg return {ou['avg_return']:+.2%}")
        if gbm:
            sym = "✅" if abs(gbm["avg_return"]) < 0.10 else "⚠️"
            print(f"  {sym} Random-walk paths:    avg return {gbm['avg_return']:+.2%}")
        if trend:
            sym = "✅" if trend["worst_drawdown"] > -0.50 else "⚠️"
            print(f"  {sym} Trending paths:       worst DD {trend['worst_drawdown']:.2%}")
        print("=" * 70)


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
