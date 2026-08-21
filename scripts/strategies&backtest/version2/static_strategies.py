"""
Static, rule-based strategies. Each one is the "skeleton" whose
parameters an RLParameterTuner can retune live -- the entry/exit LOGIC
never changes, only the thresholds, which keeps this resistant to
overfitting compared to letting RL invent signals from scratch.

Add a new strategy by subclassing StaticStrategy: declare param_space,
implement tick() (live, one bar at a time) and generate_signals()
(backtest replay -- typically just tick() called in a loop, so live and
backtest behavior are identical by construction).
"""

import numpy as np
import pandas as pd

from base import StaticStrategy
from indicators import OUCalibrator, VolatilityModel, compute_ou_features

# ---------------------------------------------------------------------------
# Dual-leg hedging / winner-selection strategy
# ---------------------------------------------------------------------------


class DualHedgeStrategy(StaticStrategy):
    """
    1. Initial entry when |z| crosses zscore_entry.
    2. If the initial leg starts losing beyond hedge_trigger_pct, open a
       counter leg (both legs run side by side).
    3. When one leg is winning and the other is losing beyond thresholds,
       cut the loser and trail-stop the survivor.

    zscore_entry only matters while FLAT (deciding whether to open a
    trade), so it's safe to keep it "live" -- RL can retune it anytime.
    All other params affect an OPEN position's exit target, so they get
    frozen (via _snapshot) the moment a trade opens and only unfreeze
    once the strategy is FLAT again. Otherwise a moving take-profit can
    never actually get hit.
    """

    name = "dual_hedge"
    param_space = {
        "zscore_entry": (1.0, 2.5),
        "hedge_trigger_pct": (0.002, 0.012),
        "cut_losing_pct": (0.003, 0.008),
        "win_profit_pct": (0.005, 0.015),
        "trail_stop_pct": (0.004, 0.015),
        "take_profit_pct": (0.010, 0.025),
    }

    def __init__(
        self,
        zscore_entry=1.5,
        hedge_trigger_pct=0.005,
        cut_losing_pct=0.005,
        win_profit_pct=0.008,
        trail_stop_pct=0.008,
        take_profit_pct=0.012,
        ou_lookback=100,
    ):
        self.zscore_entry = zscore_entry
        self.hedge_trigger_pct = hedge_trigger_pct
        self.cut_losing_pct = cut_losing_pct
        self.win_profit_pct = win_profit_pct
        self.trail_stop_pct = trail_stop_pct
        self.take_profit_pct = take_profit_pct
        self.ou_lookback = ou_lookback  # only used by generate_signals() backtest path

        self._state = "FLAT"
        self.long_active = False
        self.long_entry = 0.0
        self.short_active = False
        self.short_entry = 0.0
        self.survivor_peak = 0.0
        self._active = None  # frozen param snapshot while a position is open

    @property
    def state(self) -> str:
        return self._state

    @property
    def net_position(self) -> float:
        return (1.0 if self.long_active else 0.0) - (1.0 if self.short_active else 0.0)

    def _snapshot(self):
        self._active = {
            k: getattr(self, k) for k in self.param_space if k != "zscore_entry"
        }

    def _p(self, key):
        return self._active[key] if self._active else getattr(self, key)

    def status_line(self, price: float = 0.0) -> str:
        parts = [f"State={self._state}"]
        if self.long_active and self.long_entry > 0 and price > 0:
            pnl = (price - self.long_entry) / self.long_entry
            parts.append(f"LONG@{self.long_entry:.2f}({pnl:+.2%})")
        if self.short_active and self.short_entry > 0 and price > 0:
            pnl = (self.short_entry - price) / self.short_entry
            parts.append(f"SHORT@{self.short_entry:.2f}({pnl:+.2%})")
        if self._state == "SOLO" and self.survivor_peak > 0:
            parts.append(f"Peak={self.survivor_peak:.2f}")
        params = self._active or self.get_params()
        parts.append(
            "Params[" + " ".join(f"{k}={v:.3g}" for k, v in params.items()) + "]"
        )
        return " | ".join(parts)

    def sync_from_broker(
        self,
        has_long,
        long_entry,
        has_short,
        short_entry,
        state_hint=None,
        survivor_peak=0.0,
    ):
        self.long_active = has_long
        self.long_entry = long_entry if has_long else 0.0
        self.short_active = has_short
        self.short_entry = short_entry if has_short else 0.0
        if has_long and has_short:
            self._state = "HEDGED"
            if not self._active:
                self._snapshot()
        elif has_long or has_short:
            if state_hint in ["SOLO", "INITIAL"]:
                self._state = state_hint
                if survivor_peak > 0:
                    self.survivor_peak = survivor_peak
            else:
                self._state = "INITIAL"
            if not self._active:
                self._snapshot()
        else:
            self._state = "FLAT"
            self._active = None
            self.survivor_peak = 0.0

    def save_state(self, filepath: str) -> None:
        import json

        state_data = {
            "state": self._state,
            "long_active": self.long_active,
            "long_entry": self.long_entry,
            "short_active": self.short_active,
            "short_entry": self.short_entry,
            "survivor_peak": self.survivor_peak,
            "active": self._active,
            "params": self.get_params(),
        }
        try:
            with open(filepath, "w") as f:
                json.dump(state_data, f, indent=2)
        except Exception as e:
            print(f"[WARN] Failed to save strategy state to {filepath}: {e}")

    def load_state(self, filepath: str) -> bool:
        import json
        import os

        if not os.path.exists(filepath):
            return False
        try:
            with open(filepath, "r") as f:
                data = json.load(f)
            self._state = data.get("state", "FLAT")
            self.long_active = data.get("long_active", False)
            self.long_entry = data.get("long_entry", 0.0)
            self.short_active = data.get("short_active", False)
            self.short_entry = data.get("short_entry", 0.0)
            self.survivor_peak = data.get("survivor_peak", 0.0)
            self._active = data.get("active", None)
            if "params" in data and isinstance(data["params"], dict):
                self.set_params(data["params"])
            return True
        except Exception as e:
            print(f"[WARN] Failed to load strategy state from {filepath}: {e}")
            return False

    def tick(self, price: float, features: dict) -> list:
        actions = []
        z = features.get("ou_zscore", float("nan"))
        if z != z or price <= 0:  # NaN check
            return actions

        if self._state == "FLAT":
            if z < -self.zscore_entry:
                self.long_active, self.long_entry = True, price
                self._state = "INITIAL"
                self._snapshot()
                actions.append("OPEN_LONG")
            elif z > self.zscore_entry:
                self.short_active, self.short_entry = True, price
                self._state = "INITIAL"
                self._snapshot()
                actions.append("OPEN_SHORT")

        elif self._state == "INITIAL":
            if self.long_active:
                pnl = (price - self.long_entry) / self.long_entry
                if pnl >= self._p("take_profit_pct"):
                    self.long_active = False
                    self._state, self._active = "FLAT", None
                    actions.append("CLOSE_LONG")
                elif pnl <= -self._p("hedge_trigger_pct"):
                    self.short_active, self.short_entry = True, price
                    self._state = "HEDGED"
                    actions.append("OPEN_SHORT")
            elif self.short_active:
                pnl = (self.short_entry - price) / self.short_entry
                if pnl >= self._p("take_profit_pct"):
                    self.short_active = False
                    self._state, self._active = "FLAT", None
                    actions.append("CLOSE_SHORT")
                elif pnl <= -self._p("hedge_trigger_pct"):
                    self.long_active, self.long_entry = True, price
                    self._state = "HEDGED"
                    actions.append("OPEN_LONG")

        elif self._state == "HEDGED":
            long_pnl = (price - self.long_entry) / self.long_entry
            short_pnl = (self.short_entry - price) / self.short_entry
            cut = self._p("cut_losing_pct")
            win = self._p("win_profit_pct")
            if long_pnl >= win and short_pnl <= -cut:
                self.short_active = False
                self._state, self.survivor_peak = "SOLO", price
                actions.append("CLOSE_SHORT")
            elif short_pnl >= win and long_pnl <= -cut:
                self.long_active = False
                self._state, self.survivor_peak = "SOLO", price
                actions.append("CLOSE_LONG")
            elif (long_pnl + short_pnl) < -2 * cut:
                self.long_active = self.short_active = False
                self._state, self._active = "FLAT", None
                actions.extend(["CLOSE_LONG", "CLOSE_SHORT"])

        elif self._state == "SOLO":
            trail = self._p("trail_stop_pct")
            tp = self._p("take_profit_pct")
            cut = self._p("cut_losing_pct")
            if self.long_active:
                pnl = (price - self.long_entry) / self.long_entry
                self.survivor_peak = max(self.survivor_peak, price)
                dd = (self.survivor_peak - price) / self.survivor_peak
                if pnl >= tp or pnl <= -cut or dd >= trail or z >= 0.0:
                    self.long_active = False
                    self._state, self._active = "FLAT", None
                    actions.append("CLOSE_LONG")
            elif self.short_active:
                pnl = (self.short_entry - price) / self.short_entry
                self.survivor_peak = min(self.survivor_peak, price)
                du = (
                    (price - self.survivor_peak) / self.survivor_peak
                    if self.survivor_peak > 0
                    else 0.0
                )
                if pnl >= tp or pnl <= -cut or du >= trail or z <= 0.0:
                    self.short_active = False
                    self._state, self._active = "FLAT", None
                    actions.append("CLOSE_SHORT")

        return actions

    def generate_signals(self, data: pd.DataFrame) -> pd.Series:
        """Backtest by replaying tick() bar-by-bar, so live and backtest
        behavior are identical by construction."""
        df = compute_ou_features(data.copy(), lookback=self.ou_lookback)
        signals = pd.Series(0.0, index=df.index)
        for i in range(len(df)):
            feats = {"ou_zscore": df["ou_zscore"].iloc[i]}
            self.tick(df["close"].iloc[i], feats)
            signals.iloc[i] = self.net_position
        print(
            f"[DualHedgeStrategy] Done — long bars: {(signals > 0).sum()}, "
            f"short bars: {(signals < 0).sum()}, flat/hedged bars: {(signals == 0).sum()}"
        )
        return signals


# ---------------------------------------------------------------------------
# Static mean-reversion strategy (no ML -- pure rule-based skeleton)
# ---------------------------------------------------------------------------


class MeanReversionStrategy(StaticStrategy):
    """Entry on |z| beyond zscore_entry, exit on reversion toward the
    mean (zscore_exit) or a volatility-scaled stop-loss. This is the
    'default' base strategy an RL tuner can retune -- no ML classifier,
    just OU z-score + EWMA volatility, so its behavior stays interpretable
    and doesn't drift into overfitting the way a learned signal can.
    """

    name = "mean_reversion"
    param_space = {
        "zscore_entry": (1.0, 2.5),
        "zscore_exit": (-0.5, 0.5),
        "vol_stop_mult": (1.5, 4.0),
    }

    def __init__(
        self, zscore_entry=1.5, zscore_exit=0.0, vol_stop_mult=2.5, ou_lookback=100
    ):
        self.zscore_entry = zscore_entry
        self.zscore_exit = zscore_exit
        self.vol_stop_mult = vol_stop_mult
        self.ou_lookback = ou_lookback

        self._state = "FLAT"  # FLAT or IN_POSITION
        self.position = 0.0  # 1.0 long, -1.0 short, 0.0 flat
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
            "zscore_exit": self.zscore_exit,
            "vol_stop_mult": self.vol_stop_mult,
        }

    def _p(self, key):
        return self._active[key] if self._active else getattr(self, key)

    def status_line(self, price: float = 0.0) -> str:
        side = (
            "LONG" if self.position > 0 else ("SHORT" if self.position < 0 else "FLAT")
        )
        parts = [f"State={self._state}", f"Pos={side}"]
        if self.position != 0 and self.entry_price > 0 and price > 0:
            pnl = (price - self.entry_price) / self.entry_price * self.position
            parts.append(f"Entry@{self.entry_price:.2f}({pnl:+.2%})")
        params = self._active or self.get_params()
        parts.append(
            "Params[" + " ".join(f"{k}={v:.3g}" for k, v in params.items()) + "]"
        )
        return " | ".join(parts)

    def sync_from_broker(self, has_long, long_entry, has_short, short_entry):
        if has_long:
            self.position, self.entry_price, self._state = (
                1.0,
                long_entry,
                "IN_POSITION",
            )
            self._snapshot()
        elif has_short:
            self.position, self.entry_price, self._state = (
                -1.0,
                short_entry,
                "IN_POSITION",
            )
            self._snapshot()
        else:
            self.position, self.entry_price, self._state, self._active = (
                0.0,
                0.0,
                "FLAT",
                None,
            )

    def tick(self, price: float, features: dict) -> list:
        actions = []
        z = features.get("ou_zscore", float("nan"))
        vol = features.get("ewma_vol", 0.01)
        if z != z or price <= 0:
            return actions

        if self._state == "FLAT":
            if z < -self.zscore_entry:
                self.position, self.entry_price = 1.0, price
                self._state = "IN_POSITION"
                self._snapshot()
                actions.append("OPEN_LONG")
            elif z > self.zscore_entry:
                self.position, self.entry_price = -1.0, price
                self._state = "IN_POSITION"
                self._snapshot()
                actions.append("OPEN_SHORT")

        elif self._state == "IN_POSITION":
            exit_z = self._p("zscore_exit")
            stop_mult = self._p("vol_stop_mult")
            pnl = (price - self.entry_price) / self.entry_price * self.position
            stop_dist = stop_mult * vol

            hit_exit = (self.position > 0 and z >= exit_z) or (
                self.position < 0 and z <= -exit_z
            )
            hit_stop = pnl < -stop_dist

            if hit_exit or hit_stop:
                closing = "CLOSE_LONG" if self.position > 0 else "CLOSE_SHORT"
                self.position, self._state, self._active = 0.0, "FLAT", None
                actions.append(closing)

        return actions

    def generate_signals(self, data: pd.DataFrame) -> pd.Series:
        df = data.copy()
        if "ewma_vol" not in df.columns:
            df["ewma_vol"] = self._vol.ewma(df["close"].pct_change(), span=20)
        df = compute_ou_features(df, lookback=self.ou_lookback)

        signals = pd.Series(0.0, index=df.index)
        for i in range(len(df)):
            feats = {
                "ou_zscore": df["ou_zscore"].iloc[i],
                "ewma_vol": df["ewma_vol"].iloc[i],
            }
            self.tick(df["close"].iloc[i], feats)
            signals.iloc[i] = self.net_position
        print(
            f"[MeanReversionStrategy] Done — long bars: {(signals > 0).sum()}, "
            f"short bars: {(signals < 0).sum()}, flat bars: {(signals == 0).sum()}"
        )
        return signals


# ---------------------------------------------------------------------------
# Volatility-Confirmed Bollinger Bands Strategy
# ---------------------------------------------------------------------------


class BollingerBandStrategy(StaticStrategy):
    """
    Bollinger Bands Strategy with Volatility Regime Confirmation and RSI momentum filter.

    Modes:
    - "mean_reversion": Buy lower band touch (RSI < oversold), sell upper band touch (RSI > overbought).
    - "breakout": Buy upper band expansion, short lower band expansion.
    - "adaptive" (Default): Automatically switches between Mean Reversion (in STABLE/EXHAUSTION regimes)
      and Breakout (in EXPANDING regime), while standing by during low volatility SQUEEZE regimes.
    """

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

        self._state = "FLAT"  # FLAT or IN_POSITION
        self.position = 0.0  # 1.0 long, -1.0 short, 0.0 flat
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

    def _p(self, key):
        return self._active[key] if self._active else getattr(self, key)

    def status_line(self, price: float = 0.0) -> str:
        side = (
            "LONG" if self.position > 0 else ("SHORT" if self.position < 0 else "FLAT")
        )
        parts = [f"State={self._state}", f"Pos={side}", f"Mode={self.mode}"]
        if self.position != 0 and self.entry_price > 0 and price > 0:
            pnl = (price - self.entry_price) / self.entry_price * self.position
            parts.append(f"Entry@{self.entry_price:.2f}({pnl:+.2%})")
        params = self._active or self.get_params()
        parts.append(
            "Params[" + " ".join(f"{k}={v:.3g}" for k, v in params.items()) + "]"
        )
        return " | ".join(parts)

    def sync_from_broker(self, has_long, long_entry, has_short, short_entry):
        if has_long:
            self.position, self.entry_price, self._state = (
                1.0,
                long_entry,
                "IN_POSITION",
            )
            self._snapshot()
        elif has_short:
            self.position, self.entry_price, self._state = (
                -1.0,
                short_entry,
                "IN_POSITION",
            )
            self._snapshot()
        else:
            self.position, self.entry_price, self._state, self._active = (
                0.0,
                0.0,
                "FLAT",
                None,
            )

    def tick(self, price: float, features: dict) -> list:
        actions = []
        if price <= 0:
            return actions

        bb_upper = features.get("bb_upper", features.get("bb_upper_20_2", float("nan")))
        bb_lower = features.get("bb_lower", features.get("bb_lower_20_2", float("nan")))
        bb_middle = features.get("bb_middle", price)
        rsi = features.get("rsi_14", 50.0)
        vol_regime = features.get("volatility_regime", "STABLE")
        vol = features.get("ewma_vol", 0.01)

        rsi_oversold = self._p("rsi_oversold")
        rsi_overbought = self._p("rsi_overbought")
        stop_mult = self._p("vol_stop_mult")

        if self._state == "FLAT":
            # 1. Squeeze Standby: Avoid entering during tight squeeze unless in pure breakout mode
            if vol_regime == "SQUEEZE" and self.mode != "breakout":
                return actions

            # 2. Determine trade direction based on mode & regime
            is_breakout = (self.mode == "breakout") or (
                self.mode == "adaptive" and vol_regime == "EXPANDING"
            )
            is_mean_reversion = (self.mode == "mean_reversion") or (
                self.mode == "adaptive" and vol_regime in ["STABLE", "EXHAUSTION"]
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
                if bb_lower == bb_lower and price <= bb_lower and rsi <= rsi_oversold:
                    self.position, self.entry_price = 1.0, price
                    self._state = "IN_POSITION"
                    self._snapshot()
                    actions.append("OPEN_LONG")
                elif (
                    bb_upper == bb_upper and price >= bb_upper and rsi >= rsi_overbought
                ):
                    self.position, self.entry_price = -1.0, price
                    self._state = "IN_POSITION"
                    self._snapshot()
                    actions.append("OPEN_SHORT")

        elif self._state == "IN_POSITION":
            pnl = (price - self.entry_price) / self.entry_price * self.position
            stop_dist = stop_mult * vol

            # Exits: Reversion back to Middle Band (20 SMA) OR Volatility-scaled Stop Loss
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
        from indicators import (
            compute_atr,
            compute_bollinger_features,
            compute_volatility_regime,
        )

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
                "bb_upper": (
                    df["bb_upper"].iloc[i]
                    if "bb_upper" in df.columns
                    else df.get("bb_upper_20_2", pd.Series()).iloc[i]
                ),
                "bb_lower": (
                    df["bb_lower"].iloc[i]
                    if "bb_lower" in df.columns
                    else df.get("bb_lower_20_2", pd.Series()).iloc[i]
                ),
                "bb_middle": (
                    df["bb_middle"].iloc[i]
                    if "bb_middle" in df.columns
                    else df["close"].iloc[i]
                ),
                "rsi_14": df["rsi_14"].iloc[i] if "rsi_14" in df.columns else 50.0,
                "volatility_regime": df["volatility_regime"].iloc[i],
                "ewma_vol": df["ewma_vol"].iloc[i],
            }
            self.tick(df["close"].iloc[i], feats)
            signals.iloc[i] = self.net_position
        print(
            f"[BollingerBandStrategy] Done ({self.mode}) — long bars: {(signals > 0).sum()}, "
            f"short bars: {(signals < 0).sum()}, flat bars: {(signals == 0).sum()}"
        )
        return signals


# Registry so the trader / backtests can pick a strategy by name.
STRATEGY_REGISTRY = {
    "dual_hedge": DualHedgeStrategy,
    "mean_reversion": MeanReversionStrategy,
    "bollinger_bands": BollingerBandStrategy,
}
