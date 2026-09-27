# backend/app/analytics/user_strategies/static_strategies.py
"""
Rule-based static strategies with declared parameter spaces and state machines.
Includes:
  - DualHedgeStrategy
  - MeanReversionStrategy
  - BollingerBandStrategy
"""

import json
import os
import numpy as np
import pandas as pd

from .base_strategy import StaticStrategy
from .indicators import (
    OUCalibrator,
    VolatilityModel,
    compute_adf_stat,
    compute_atr,
    compute_bollinger_features,
    compute_ou_features,
    compute_volatility_regime,
    KalmanPairEstimator,
)


class DualHedgeStrategy(StaticStrategy):
    """Dual-leg statistical hedging strategy with state-machine tracking.

    States:
      - FLAT: Standing by for entry z-score.
      - INITIAL: Single position open (e.g. LONG @ 100).
      - HEDGED: Adverse move opened dynamic hedge (LONG @ 100 + SHORT @ 99).
      - SOLO: Survivor position after cutting losing leg at loss threshold.
    """

    name = "dual_hedge"
    param_space = {
        "zscore_entry": (1.2, 2.5),
        "hedge_trigger_pct": (0.003, 0.015),
        "cut_losing_pct": (0.005, 0.025),
        "win_profit_pct": (0.003, 0.015),
        "take_profit_pct": (0.008, 0.035),
    }

    def __init__(
        self,
        zscore_entry: float = 1.8,
        hedge_trigger_pct: float = 0.008,
        cut_losing_pct: float = 0.012,
        win_profit_pct: float = 0.005,
        take_profit_pct: float = 0.015,
        mode: str = "adaptive",
    ):
        self.zscore_entry = zscore_entry
        self.hedge_trigger_pct = hedge_trigger_pct
        self.cut_losing_pct = cut_losing_pct
        self.win_profit_pct = win_profit_pct
        self.take_profit_pct = take_profit_pct
        self.mode = mode

        self._state = "FLAT"
        self.long_active = False
        self.short_active = False
        self.long_entry = 0.0
        self.short_entry = 0.0

        self.initial_direction = None
        self.initial_entry = 0.0
        self.survivor_peak = 0.0

        self._active_params = None

    @property
    def state(self) -> str:
        return self._state

    @property
    def net_position(self) -> float:
        pos = 0.0
        if self.long_active:
            pos += 1.0
        if self.short_active:
            pos -= 1.0
        return pos

    def _snapshot_params(self):
        self._active_params = {
            "zscore_entry": self.zscore_entry,
            "hedge_trigger_pct": self.hedge_trigger_pct,
            "cut_losing_pct": self.cut_losing_pct,
            "win_profit_pct": self.win_profit_pct,
            "take_profit_pct": self.take_profit_pct,
        }

    def _p(self, key: str) -> float:
        if self._active_params and key in self._active_params:
            return self._active_params[key]
        return getattr(self, key)

    def save_state(self, filepath: str):
        data = {
            "state": self._state,
            "long_active": self.long_active,
            "short_active": self.short_active,
            "long_entry": self.long_entry,
            "short_entry": self.short_entry,
            "initial_direction": self.initial_direction,
            "initial_entry": self.initial_entry,
            "survivor_peak": self.survivor_peak,
            "active_params": self._active_params,
        }
        os.makedirs(os.path.dirname(os.path.abspath(filepath)), exist_ok=True)
        with open(filepath, "w") as f:
            json.dump(data, f, indent=2)

    def load_state(self, filepath: str) -> bool:
        if not os.path.exists(filepath):
            return False
        try:
            with open(filepath, "r") as f:
                data = json.load(f)
            self._state = data.get("state", "FLAT")
            self.long_active = data.get("long_active", False)
            self.short_active = data.get("short_active", False)
            self.long_entry = data.get("long_entry", 0.0)
            self.short_entry = data.get("short_entry", 0.0)
            self.initial_direction = data.get("initial_direction", None)
            self.initial_entry = data.get("initial_entry", 0.0)
            self.survivor_peak = data.get("survivor_peak", 0.0)
            self._active_params = data.get("active_params", None)
            return True
        except Exception as e:
            print(f"[DualHedgeStrategy] Error loading state from {filepath}: {e}")
            return False

    def sync_from_broker(
        self,
        has_long: bool,
        long_entry: float,
        has_short: bool,
        short_entry: float,
    ):
        self.long_active = has_long
        self.long_entry = long_entry if has_long else 0.0
        self.short_active = has_short
        self.short_entry = short_entry if has_short else 0.0

        if has_long and has_short:
            self._state = "HEDGED"
            self._snapshot_params()
        elif has_long:
            if self._state in ["HEDGED", "SOLO"] or self.initial_direction == "SHORT":
                self._state = "SOLO"
            else:
                self._state = "INITIAL"
                self.initial_direction = "LONG"
                self.initial_entry = long_entry
            self._snapshot_params()
        elif has_short:
            if self._state in ["HEDGED", "SOLO"] or self.initial_direction == "LONG":
                self._state = "SOLO"
            else:
                self._state = "INITIAL"
                self.initial_direction = "SHORT"
                self.initial_entry = short_entry
            self._snapshot_params()
        else:
            self._state = "FLAT"
            self.initial_direction = None
            self.initial_entry = 0.0
            self.survivor_peak = 0.0
            self._active_params = None

    def tick(self, price: float, features: dict) -> list:
        actions = []
        if price <= 0:
            return actions

        z = features.get("ou_zscore", 0.0)
        vol = features.get("ewma_vol", 0.01)

        z_entry = self._p("zscore_entry")
        trig = self._p("hedge_trigger_pct")
        cut = self._p("cut_losing_pct")
        win = self._p("win_profit_pct")
        tp = self._p("take_profit_pct")

        if self.mode == "adaptive":
            trig = max(trig, 0.5 * vol)
            cut = max(cut, 0.8 * vol)
            win = max(win, 0.4 * vol)
            tp = max(tp, 1.2 * vol)

        if self._state == "FLAT":
            if z <= -z_entry:
                self.long_active, self.long_entry = True, price
                self.initial_direction, self.initial_entry = "LONG", price
                self._state = "INITIAL"
                self._snapshot_params()
                actions.append("OPEN_LONG")
            elif z >= z_entry:
                self.short_active, self.short_entry = True, price
                self.initial_direction, self.initial_entry = "SHORT", price
                self._state = "INITIAL"
                self._snapshot_params()
                actions.append("OPEN_SHORT")

        elif self._state == "INITIAL":
            if self.long_active:
                pnl = (price - self.long_entry) / self.long_entry
                if pnl >= tp:
                    self.long_active = False
                    self._state, self._active_params = "FLAT", None
                    actions.append("CLOSE_LONG")
                elif pnl <= -trig:
                    self.short_active, self.short_entry = True, price
                    self._state = "HEDGED"
                    actions.append("OPEN_SHORT")
            elif self.short_active:
                pnl = (self.short_entry - price) / self.short_entry
                if pnl >= tp:
                    self.short_active = False
                    self._state, self._active_params = "FLAT", None
                    actions.append("CLOSE_SHORT")
                elif pnl <= -trig:
                    self.long_active, self.long_entry = True, price
                    self._state = "HEDGED"
                    actions.append("OPEN_LONG")

        elif self._state == "HEDGED":
            long_pnl = (price - self.long_entry) / self.long_entry
            short_pnl = (self.short_entry - price) / self.short_entry

            # Rule A: Long is winning, Short is losing -> CUT SHORT, KEEP LONG!
            if long_pnl >= win and short_pnl <= -cut:
                self.short_active = False
                self._state = "SOLO"
                self.survivor_peak = price
                actions.append("CLOSE_SHORT")
            # Rule B: Short is winning, Long is losing -> CUT LONG, KEEP SHORT!
            elif short_pnl >= win and long_pnl <= -cut:
                self.long_active = False
                self._state = "SOLO"
                self.survivor_peak = price
                actions.append("CLOSE_LONG")
            # Safety Guard: If total hedge loss exceeds threshold, close both
            elif (long_pnl + short_pnl) < -2 * cut:
                self.long_active = False
                self.short_active = False
                self._state, self._active_params = "FLAT", None
                actions.append("CLOSE_LONG")
                actions.append("CLOSE_SHORT")

        elif self._state == "SOLO":
            if self.long_active:
                self.survivor_peak = max(self.survivor_peak, price)
                pnl = (price - self.long_entry) / self.long_entry
                dd_from_peak = (
                    (self.survivor_peak - price) / self.survivor_peak
                    if self.survivor_peak > 0
                    else 0.0
                )

                if pnl >= tp or z >= 0.0 or dd_from_peak >= win or pnl <= -cut:
                    self.long_active = False
                    self._state, self._active_params = "FLAT", None
                    actions.append("CLOSE_LONG")
            elif self.short_active:
                if self.survivor_peak == 0.0:
                    self.survivor_peak = price
                else:
                    self.survivor_peak = min(self.survivor_peak, price)
                pnl = (self.short_entry - price) / self.short_entry
                dd_from_peak = (
                    (price - self.survivor_peak) / self.survivor_peak
                    if self.survivor_peak > 0
                    else 0.0
                )

                if pnl >= tp or z <= 0.0 or dd_from_peak >= win or pnl <= -cut:
                    self.short_active = False
                    self._state, self._active_params = "FLAT", None
                    actions.append("CLOSE_SHORT")

        return actions

    def generate_signals(self, data: pd.DataFrame) -> pd.Series:
        df = compute_ou_features(data.copy())

        if "ewma_vol" not in df.columns:
            df["ewma_vol"] = VolatilityModel.ewma(
                df["close"].pct_change(), span=20
            )

        signals = pd.Series(0.0, index=df.index)

        for i in range(len(df)):
            price = df["close"].iloc[i]
            features = {
                "ou_zscore": (
                    df["ou_zscore"].iloc[i]
                    if not pd.isna(df["ou_zscore"].iloc[i])
                    else 0.0
                ),
                "ewma_vol": (
                    df["ewma_vol"].iloc[i]
                    if not pd.isna(df["ewma_vol"].iloc[i])
                    else 0.01
                ),
            }
            self.tick(price, features)
            signals.iloc[i] = self.net_position

        return signals


class MeanReversionStrategy(StaticStrategy):
    """Pure Ornstein-Uhlenbeck z-score mean reversion strategy."""

    name = "mean_reversion"
    param_space = {
        "zscore_entry": (1.5, 2.5),
        "zscore_exit": (0.0, 0.5),
        "stop_loss_mult": (1.5, 3.5),
    }

    def __init__(
        self,
        zscore_entry: float = 2.0,
        zscore_exit: float = 0.0,
        stop_loss_mult: float = 2.5,
        mode: str = "adaptive",
    ):
        self.zscore_entry = zscore_entry
        self.zscore_exit = zscore_exit
        self.stop_loss_mult = stop_loss_mult
        self.mode = mode

        self._state = "FLAT"
        self.position = 0.0
        self.entry_price = 0.0
        self._active = None

    @property
    def state(self) -> str:
        return self._state

    @property
    def net_position(self) -> float:
        return self.position

    def _snapshot(self):
        self._active = {
            "zscore_entry": self.zscore_entry,
            "zscore_exit": self.zscore_exit,
            "stop_loss_mult": self.stop_loss_mult,
        }

    def _p(self, key: str) -> float:
        return self._active[key] if self._active else getattr(self, key)

    def tick(self, price: float, features: dict) -> list:
        actions = []
        if price <= 0:
            return actions

        z = features.get("ou_zscore", 0.0)
        vol = features.get("ewma_vol", 0.01)

        z_entry = self._p("zscore_entry")
        z_exit = self._p("zscore_exit")
        stop_mult = self._p("stop_loss_mult")

        if self._state == "FLAT":
            if z <= -z_entry:
                self.position, self.entry_price = 1.0, price
                self._state = "IN_POSITION"
                self._snapshot()
                actions.append("OPEN_LONG")
            elif z >= z_entry:
                self.position, self.entry_price = -1.0, price
                self._state = "IN_POSITION"
                self._snapshot()
                actions.append("OPEN_SHORT")

        elif self._state == "IN_POSITION":
            pnl = (price - self.entry_price) / self.entry_price * self.position
            stop_dist = stop_mult * vol

            if self.position > 0:
                if z >= z_exit or pnl < -stop_dist:
                    self.position, self._state, self._active = 0.0, "FLAT", None
                    actions.append("CLOSE_LONG")
            elif self.position < 0:
                if z <= -z_exit or pnl < -stop_dist:
                    self.position, self._state, self._active = 0.0, "FLAT", None
                    actions.append("CLOSE_SHORT")

        return actions

    def generate_signals(self, data: pd.DataFrame) -> pd.Series:
        df = compute_ou_features(data.copy())
        if "ewma_vol" not in df.columns:
            df["ewma_vol"] = VolatilityModel.ewma(
                df["close"].pct_change(), span=20
            )

        signals = pd.Series(0.0, index=df.index)
        for i in range(len(df)):
            feats = {
                "ou_zscore": df["ou_zscore"].iloc[i],
                "ewma_vol": df["ewma_vol"].iloc[i],
            }
            self.tick(df["close"].iloc[i], feats)
            signals.iloc[i] = self.net_position

        return signals


class BollingerBandStrategy(StaticStrategy):
    """Bollinger Bands + RSI strategy with volatility regime adaptation."""

    name = "bollinger_bands"
    param_space = {
        "bb_std_mult": (1.5, 2.5),
        "rsi_oversold": (25.0, 40.0),
        "rsi_overbought": (60.0, 75.0),
        "vol_stop_mult": (1.5, 3.5),
    }

    def __init__(
        self,
        bb_std_mult: float = 2.0,
        rsi_oversold: float = 35.0,
        rsi_overbought: float = 65.0,
        vol_stop_mult: float = 2.5,
        mode: str = "adaptive",
    ):
        self.bb_std_mult = bb_std_mult
        self.rsi_oversold = rsi_oversold
        self.rsi_overbought = rsi_overbought
        self.vol_stop_mult = vol_stop_mult
        self.mode = mode

        self._state = "FLAT"
        self.position = 0.0
        self.entry_price = 0.0
        self._active = None
        self._vol = VolatilityModel()

    @property
    def state(self) -> str:
        return self._state

    @property
    def net_position(self) -> float:
        return self.position

    def _snapshot(self):
        self._active = {
            "bb_std_mult": self.bb_std_mult,
            "rsi_oversold": self.rsi_oversold,
            "rsi_overbought": self.rsi_overbought,
            "vol_stop_mult": self.vol_stop_mult,
        }

    def _p(self, key: str) -> float:
        return self._active[key] if self._active else getattr(self, key)

    def tick(self, price: float, features: dict) -> list:
        actions = []
        if price <= 0:
            return actions

        bb_upper = features.get(
            "bb_upper", features.get("bb_upper_20_2", float("nan"))
        )
        bb_lower = features.get(
            "bb_lower", features.get("bb_lower_20_2", float("nan"))
        )
        bb_middle = features.get("bb_middle", price)
        rsi = features.get("rsi_14", 50.0)
        vol_regime = features.get("volatility_regime", "STABLE")
        vol = features.get("ewma_vol", 0.01)

        rsi_oversold = self._p("rsi_oversold")
        rsi_overbought = self._p("rsi_overbought")
        stop_mult = self._p("vol_stop_mult")

        if self._state == "FLAT":
            if vol_regime == "SQUEEZE" and self.mode != "breakout":
                return actions

            is_breakout = (self.mode == "breakout") or (
                self.mode == "adaptive" and vol_regime == "EXPANDING"
            )
            is_mean_reversion = (self.mode == "mean_reversion") or (
                self.mode == "adaptive"
                and vol_regime in ["STABLE", "EXHAUSTION"]
            )

            if is_breakout:
                if bb_upper == bb_upper and price > bb_upper:
                    self.position, self.entry_price = 1.0, price
                    self._state = "IN_POSITION"
                    self._snapshot()
                    actions.append("OPEN_LONG")
                elif bb_lower == bb_lower and price < bb_lower:
                    self.position, self.entry_price = -1.0, price
                    self._state = "IN_POSITION"
                    self._snapshot()
                    actions.append("OPEN_SHORT")

            elif is_mean_reversion:
                if (
                    bb_lower == bb_lower
                    and price <= bb_lower
                    and rsi <= rsi_oversold
                ):
                    self.position, self.entry_price = 1.0, price
                    self._state = "IN_POSITION"
                    self._snapshot()
                    actions.append("OPEN_LONG")
                elif (
                    bb_upper == bb_upper
                    and price >= bb_upper
                    and rsi >= rsi_overbought
                ):
                    self.position, self.entry_price = -1.0, price
                    self._state = "IN_POSITION"
                    self._snapshot()
                    actions.append("OPEN_SHORT")

        elif self._state == "IN_POSITION":
            pnl = (price - self.entry_price) / self.entry_price * self.position
            stop_dist = stop_mult * vol

            hit_tp = (self.position > 0 and price >= bb_middle) or (
                self.position < 0 and price <= bb_middle
            )
            hit_sl = pnl < -stop_dist

            if hit_tp or hit_sl:
                closing = "CLOSE_LONG" if self.position > 0 else "CLOSE_SHORT"
                self.position, self._state, self._active = 0.0, "FLAT", None
                actions.append(closing)

        return actions

    def generate_signals(self, data: pd.DataFrame) -> pd.Series:
        df = data.copy()
        if "bb_upper" not in df.columns:
            df = compute_bollinger_features(df, std_mult=self.bb_std_mult)
        if "atr_14" not in df.columns:
            df["atr_14"] = compute_atr(df)
        if "ewma_vol" not in df.columns:
            df["ewma_vol"] = self._vol.ewma(df["close"].pct_change(), span=20)
        if "volatility_regime" not in df.columns:
            df["volatility_regime"] = compute_volatility_regime(df)

        signals = pd.Series(0.0, index=df.index)
        for i in range(len(df)):
            feats = {
                "bb_upper": df["bb_upper"].iloc[i],
                "bb_lower": df["bb_lower"].iloc[i],
                "bb_middle": df["bb_middle"].iloc[i],
                "rsi_14": (
                    df["rsi_14"].iloc[i] if "rsi_14" in df.columns else 50.0
                ),
                "volatility_regime": df["volatility_regime"].iloc[i],
                "ewma_vol": df["ewma_vol"].iloc[i],
            }
            self.tick(df["close"].iloc[i], feats)
            signals.iloc[i] = self.net_position

        return signals


class VolumeExhaustionReversalStrategy(StaticStrategy):
    """Swing reversal after an exceptional-volume directional bar.

    The setup is deliberately confirmation based.  A high-volume sell bar is
    *not* bought immediately: the following bar must fail to make a lower low
    and close bullish before a long signal is produced.  The inverse applies
    after a high-volume buy bar.  Signals become positions on the next bar,
    which keeps the backtest free of same-bar look-ahead.

    This strategy currently implements ``generate_signals`` for backtesting.
    It is intentionally not in ``USER_STRATEGY_REGISTRY``: that registry is
    consumed by the live MT5 signal runner.
    """

    name = "volume_exhaustion_reversal"
    param_space = {
        "volume_lookback": (10, 60),
        "volume_multiple": (1.25, 4.0),
        "stop_atr_mult": (0.75, 3.0),
        "target_atr_mult": (1.0, 6.0),
        "max_holding_bars": (2, 30),
    }

    def __init__(
        self,
        volume_lookback: int = 20,
        volume_multiple: float = 2.0,
        stop_atr_mult: float = 1.5,
        target_atr_mult: float = 3.0,
        max_holding_bars: int = 10,
        mode: str = "confirmation",
    ):
        self.volume_lookback = int(volume_lookback)
        self.volume_multiple = float(volume_multiple)
        self.stop_atr_mult = float(stop_atr_mult)
        self.target_atr_mult = float(target_atr_mult)
        self.max_holding_bars = int(max_holding_bars)
        self.mode = mode

    @property
    def state(self) -> str:
        # This class is backtest-only for now; its replay state lives locally
        # inside generate_signals rather than surviving live ticks.
        return "BACKTEST_ONLY"

    @property
    def net_position(self) -> float:
        return 0.0

    def tick(self, price: float, features: dict) -> list:
        raise NotImplementedError(
            "VolumeExhaustionReversalStrategy is backtest-only; use generate_signals()."
        )

    @staticmethod
    def _normalise_ohlcv(data: pd.DataFrame) -> pd.DataFrame:
        df = data.copy()
        df.columns = [str(column).lower() for column in df.columns]
        if "close" not in df.columns or "volume" not in df.columns:
            raise ValueError("Volume exhaustion backtests require 'close' and 'volume' columns.")
        for column in ("open", "high", "low"):
            if column not in df.columns:
                df[column] = df["close"]
        return df

    def generate_signals(self, data: pd.DataFrame) -> pd.Series:
        df = self._normalise_ohlcv(data)
        signals = pd.Series(0.0, index=df.index)
        if len(df) <= self.volume_lookback + 1:
            return signals

        atr = compute_atr(df)
        # Shift ensures the surge bar is compared only with earlier volume.
        prior_average_volume = df["volume"].shift(1).rolling(
            self.volume_lookback, min_periods=self.volume_lookback
        ).mean()

        position = 0.0
        entry_price = stop_price = target_price = 0.0
        bars_held = 0

        for i in range(1, len(df)):
            current = df.iloc[i]
            previous = df.iloc[i - 1]
            price = float(current["close"])

            if position:
                bars_held += 1
                if position > 0:
                    exit_trade = (
                        float(current["low"]) <= stop_price
                        or float(current["high"]) >= target_price
                        or bars_held >= self.max_holding_bars
                    )
                else:
                    exit_trade = (
                        float(current["high"]) >= stop_price
                        or float(current["low"]) <= target_price
                        or bars_held >= self.max_holding_bars
                    )
                if exit_trade:
                    position = 0.0
                    entry_price = stop_price = target_price = 0.0
                    bars_held = 0

            # The previous bar is the volume event.  The current bar confirms
            # exhaustion only when it cannot extend the event bar's extreme.
            average_volume = prior_average_volume.iloc[i - 1]
            is_volume_surge = (
                pd.notna(average_volume)
                and average_volume > 0
                and float(previous["volume"]) >= average_volume * self.volume_multiple
            )
            bearish_surge = is_volume_surge and previous["close"] < previous["open"]
            bullish_surge = is_volume_surge and previous["close"] > previous["open"]
            seller_exhausted = (
                bearish_surge
                and current["low"] >= previous["low"]
                and current["close"] > current["open"]
            )
            buyer_exhausted = (
                bullish_surge
                and current["high"] <= previous["high"]
                and current["close"] < current["open"]
            )

            if position == 0.0 and pd.notna(atr.iloc[i]) and atr.iloc[i] > 0:
                if seller_exhausted:
                    position = 1.0
                    entry_price = price
                    stop_price = entry_price - atr.iloc[i] * self.stop_atr_mult
                    target_price = entry_price + atr.iloc[i] * self.target_atr_mult
                    bars_held = 0
                elif buyer_exhausted:
                    position = -1.0
                    entry_price = price
                    stop_price = entry_price + atr.iloc[i] * self.stop_atr_mult
                    target_price = entry_price - atr.iloc[i] * self.target_atr_mult
                    bars_held = 0

            signals.iloc[i] = position

        return signals


class PairStatArbitrageStrategy(StaticStrategy):
    """Market-neutral, rolling hedge-ratio pairs strategy for backtesting.

    ``data`` must contain ``close`` for the traded (primary) leg and
    ``hedge_close`` for the hedge leg.  A rolling OLS fit on log prices builds
    a residual spread.  The strategy enters when that spread is unusually far
    from its own recent mean and exits on convergence, a wider stop, or a
    maximum holding period.  It returns dollar-neutral-ish *leg positions*,
    not a single-instrument signal, and is therefore deliberately excluded
    from the live strategy registry.
    """

    name = "pair_stat_arbitrage"
    param_space = {
        "lookback": (40, 252),
        "entry_zscore": (1.5, 3.5),
        "exit_zscore": (0.0, 1.0),
        "stop_zscore": (2.5, 5.0),
        "max_holding_bars": (5, 150),
        "min_correlation": (0.5, 0.95),
        "orderflow_lookback": (5, 100),
        "orderflow_threshold": (0.0, 0.75),
        "rebalance_on_drift": (0.0, 0.2),
        "max_adf_stat": (-4.0, -1.8),
    }

    def __init__(
        self,
        lookback: int = 60,
        entry_zscore: float = 2.0,
        exit_zscore: float = 0.5,
        stop_zscore: float = 3.5,
        max_holding_bars: int = 30,
        min_correlation: float = 0.7,
        orderflow_lookback: int = 20,
        orderflow_threshold: float = 0.05,
        rebalance_on_drift: float = 0.0,
        max_adf_stat: float = -2.2,
        use_dynamic_half_life: bool = True,
        scale_in: bool = True,
        estimator: str = "ols",
        mode: str = "backtest",
    ):
        self.lookback = int(lookback)
        self.entry_zscore = float(entry_zscore)
        self.exit_zscore = float(exit_zscore)
        self.stop_zscore = float(stop_zscore)
        self.max_holding_bars = int(max_holding_bars)
        self.min_correlation = float(min_correlation)
        self.orderflow_lookback = int(orderflow_lookback)
        self.orderflow_threshold = float(orderflow_threshold)
        self.rebalance_on_drift = float(rebalance_on_drift)
        self.max_adf_stat = float(max_adf_stat)
        self.use_dynamic_half_life = bool(use_dynamic_half_life)
        self.scale_in = bool(scale_in)
        self.estimator = str(estimator).lower()
        self.mode = mode

    @property
    def state(self) -> str:
        return "BACKTEST_ONLY"

    @property
    def net_position(self) -> float:
        return 0.0

    def tick(self, price: float, features: dict) -> list:
        raise NotImplementedError(
            "PairStatArbitrageStrategy needs two aligned price series; use generate_pair_positions()."
        )

    def generate_signals(self, data: pd.DataFrame) -> pd.Series:
        """Compatibility view: primary-leg positions only.

        Backtests must call :meth:`generate_pair_positions` to account for
        both legs and their transaction costs.
        """
        return self.generate_pair_positions(data)["primary_position"]

    def generate_pair_positions(self, data: pd.DataFrame) -> pd.DataFrame:
        required = {"close", "hedge_close", "volume", "hedge_volume"}
        missing = required.difference(data.columns)
        if missing:
            raise ValueError(
                "Pair stat-arbitrage requires aligned 'close' and 'hedge_close' columns "
                f"(missing: {', '.join(sorted(missing))})."
            )
        if self.lookback < 20:
            raise ValueError("lookback must be at least 20 bars.")
        if not 0 <= self.exit_zscore < self.entry_zscore < self.stop_zscore:
            raise ValueError("Require 0 <= exit_zscore < entry_zscore < stop_zscore.")
        if self.orderflow_lookback < 2:
            raise ValueError("orderflow_lookback must be at least 2 bars.")

        prices = data[["close", "hedge_close"]].astype(float).replace([np.inf, -np.inf], np.nan)
        valid = (prices["close"] > 0) & (prices["hedge_close"] > 0)
        primary_log = np.log(prices["close"].where(valid))
        hedge_log = np.log(prices["hedge_close"].where(valid))

        if "macro_beta" in data.columns and "macro_alpha" in data.columns:
            beta = data["macro_beta"].astype(float)
            alpha = data["macro_alpha"].astype(float)
            spread_mean = data.get("macro_spread_mean", pd.Series(0.0, index=data.index)).astype(float)
            spread_std = data.get("macro_spread_std", pd.Series(1.0, index=data.index)).astype(float).replace(0, np.nan)
            correlation = data.get("macro_correlation", pd.Series(1.0, index=data.index)).astype(float)
            spread = primary_log - alpha - beta * hedge_log
            zscore = (spread - spread_mean).div(spread_std)
        elif self.estimator == "kalman":
            k_alpha, k_beta, k_spread, k_std, k_z = KalmanPairEstimator().fit_series(
                primary_log.values, hedge_log.values
            )
            beta = pd.Series(k_beta, index=data.index)
            alpha = pd.Series(k_alpha, index=data.index)
            spread = pd.Series(k_spread, index=data.index)
            spread_std = pd.Series(k_std, index=data.index)
            zscore = pd.Series(k_z, index=data.index)
            correlation = primary_log.rolling(self.lookback, min_periods=self.lookback).corr(hedge_log)
        else:
            # beta is the rolling OLS slope of primary log-price on hedge log-price.
            hedge_var = hedge_log.rolling(self.lookback, min_periods=self.lookback).var()
            beta = primary_log.rolling(self.lookback, min_periods=self.lookback).cov(hedge_log).div(hedge_var)
            alpha = primary_log.rolling(self.lookback, min_periods=self.lookback).mean() - beta * hedge_log.rolling(self.lookback, min_periods=self.lookback).mean()
            spread = primary_log - alpha - beta * hedge_log
            spread_mean = spread.rolling(self.lookback, min_periods=self.lookback).mean()
            spread_std = spread.rolling(self.lookback, min_periods=self.lookback).std().replace(0, np.nan)
            zscore = (spread - spread_mean).div(spread_std)
            correlation = primary_log.rolling(self.lookback, min_periods=self.lookback).corr(hedge_log)

        # Microstructure signed volume imbalance proxy
        def signed_volume_imbalance(close_col: str, volume_col: str, prefix: str) -> pd.Series:
            close = data[close_col].astype(float)
            volume = data[volume_col].astype(float).clip(lower=0).fillna(0.0)
            high = data.get(f"{prefix}high", close).astype(float)
            low = data.get(f"{prefix}low", close).astype(float)
            bar_range = (high - low).replace(0, np.nan)
            close_location = ((2.0 * close - high - low) / bar_range).clip(-1.0, 1.0)
            fallback_direction = np.sign(close.diff()).fillna(0.0)
            signed_volume = volume * close_location.fillna(fallback_direction)
            return signed_volume.rolling(
                self.orderflow_lookback, min_periods=self.orderflow_lookback
            ).sum().div(
                volume.rolling(self.orderflow_lookback, min_periods=self.orderflow_lookback)
                .sum().replace(0, np.nan)
            )

        primary_orderflow = signed_volume_imbalance("close", "volume", "")
        hedge_orderflow = signed_volume_imbalance("hedge_close", "hedge_volume", "hedge_")
        relative_orderflow = primary_orderflow - hedge_orderflow

        macro_adf = data.get("macro_adf_stat", pd.Series(np.nan, index=data.index))
        macro_hl = data.get("macro_half_life", pd.Series(np.nan, index=data.index))

        primary_position = pd.Series(0.0, index=data.index)
        hedge_position = pd.Series(0.0, index=data.index)
        active_hedge_ratios = pd.Series(0.0, index=data.index)
        signal_reasons = pd.Series("", index=data.index)
        position = 0.0
        active_beta = 0.0
        bars_held = 0
        trade_max_bars = self.max_holding_bars

        scale_step = 0.6  # additional zscore required to scale into full size

        for i in range(len(data)):
            z = zscore.iloc[i]
            hedge_ratio = beta.iloc[i]
            corr = correlation.iloc[i]
            flow = relative_orderflow.iloc[i]
            ready = pd.notna(z) and pd.notna(hedge_ratio) and pd.notna(corr) and hedge_ratio > 0

            # ADF stationarity check: reject entry if spread is non-stationary
            adf_val = macro_adf.iloc[i] if i < len(macro_adf) else np.nan
            is_stationary = (adf_val <= self.max_adf_stat) if pd.notna(adf_val) else True

            # Dynamic half-life max holding duration
            hl_val = macro_hl.iloc[i] if i < len(macro_hl) else np.nan

            if abs(position) > 1e-6:
                bars_held += 1
                exit_trigger = None

                if not ready:
                    exit_trigger = "REGIME_INVALID"
                elif abs(z) >= self.stop_zscore:
                    exit_trigger = "STOP_LOSS"
                elif bars_held >= trade_max_bars:
                    exit_trigger = "HALF_LIFE_EXPIRED" if trade_max_bars != self.max_holding_bars else "MAX_HOLDING"
                # Full target reversion: cross 0.0 line
                elif (position > 0 and z >= 0.0) or (position < 0 and z <= 0.0):
                    exit_trigger = "TARGET_REVERSION"

                if exit_trigger is not None:
                    position = 0.0
                    bars_held = 0
                    signal_reasons.iloc[i] = exit_trigger
                    active_beta = 0.0
                else:
                    # Partial take-profit when reaching exit_zscore (e.g. 0.5)
                    if self.scale_in and abs(position) > 0.55 and abs(z) <= self.exit_zscore:
                        position = np.sign(position) * 0.5
                        signal_reasons.iloc[i] = "PARTIAL_TP_50%"

                    # Scale-in: add remaining 50% if divergence extends further
                    elif self.scale_in and abs(position) <= 0.55:
                        if position < 0 and z >= (self.entry_zscore + scale_step) and flow <= -self.orderflow_threshold:
                            position = -1.0
                            signal_reasons.iloc[i] = "SCALE_IN_SHORT_L2"
                        elif position > 0 and z <= -(self.entry_zscore + scale_step) and flow >= self.orderflow_threshold:
                            position = 1.0
                            signal_reasons.iloc[i] = "SCALE_IN_LONG_L2"

                    # Hedge ratio drift rebalancing
                    if self.rebalance_on_drift > 0 and ready and active_beta > 0:
                        drift = abs(hedge_ratio - active_beta) / active_beta
                        if drift >= self.rebalance_on_drift:
                            active_beta = hedge_ratio
                            signal_reasons.iloc[i] = f"DRIFT_REBALANCE({drift:.1%})"

            elif ready and corr >= self.min_correlation and pd.notna(flow) and is_stationary:
                # Calculate trade max bars from half-life if available
                if self.use_dynamic_half_life and pd.notna(hl_val) and hl_val > 1:
                    trade_max_bars = int(np.clip(2.5 * hl_val, 12, self.max_holding_bars))
                else:
                    trade_max_bars = self.max_holding_bars

                # Level 1 Entry
                init_size = 0.5 if self.scale_in else 1.0
                if z >= self.entry_zscore and flow <= -self.orderflow_threshold:
                    position = -init_size  # short rich primary, long hedge
                    active_beta = hedge_ratio
                    bars_held = 0
                    signal_reasons.iloc[i] = "ENTRY_SHORT_L1" if self.scale_in else "ENTRY_SHORT_SPREAD"
                elif z <= -self.entry_zscore and flow >= self.orderflow_threshold:
                    position = init_size   # long cheap primary, short hedge
                    active_beta = hedge_ratio
                    bars_held = 0
                    signal_reasons.iloc[i] = "ENTRY_LONG_L1" if self.scale_in else "ENTRY_LONG_SPREAD"

            primary_position.iloc[i] = position
            hedge_position.iloc[i] = -position * active_beta if abs(position) > 1e-6 and ready else 0.0
            active_hedge_ratios.iloc[i] = active_beta

        return pd.DataFrame(
            {
                "primary_position": primary_position,
                "hedge_position": hedge_position,
                "hedge_ratio": beta,
                "active_hedge_ratio": active_hedge_ratios,
                "spread_zscore": zscore,
                "rolling_correlation": correlation,
                "primary_orderflow_proxy": primary_orderflow,
                "hedge_orderflow_proxy": hedge_orderflow,
                "relative_orderflow_proxy": relative_orderflow,
                "signal_reason": signal_reasons,
            },
            index=data.index,
        )


USER_STRATEGY_REGISTRY = {
    "dual_hedge": DualHedgeStrategy,
    "mean_reversion": MeanReversionStrategy,
    "bollinger_bands": BollingerBandStrategy,
}

# Backtest-only strategies must be explicitly added here, so creating one
# cannot accidentally activate direct MT5 order execution.
BACKTEST_STRATEGY_REGISTRY = {
    "volume_exhaustion_reversal": VolumeExhaustionReversalStrategy,
    "pair_stat_arbitrage": PairStatArbitrageStrategy,
}
