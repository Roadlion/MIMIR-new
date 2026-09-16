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
import pandas as pd

from .base_strategy import StaticStrategy
from .indicators import (
    OUCalibrator,
    VolatilityModel,
    compute_atr,
    compute_bollinger_features,
    compute_ou_features,
    compute_volatility_regime,
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


USER_STRATEGY_REGISTRY = {
    "dual_hedge": DualHedgeStrategy,
    "mean_reversion": MeanReversionStrategy,
    "bollinger_bands": BollingerBandStrategy,
}

# Backtest-only strategies must be explicitly added here, so creating one
# cannot accidentally activate direct MT5 order execution.
BACKTEST_STRATEGY_REGISTRY = {
    "volume_exhaustion_reversal": VolumeExhaustionReversalStrategy,
}
