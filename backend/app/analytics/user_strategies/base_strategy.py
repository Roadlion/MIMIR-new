# backend/app/analytics/user_strategies/base_strategy.py
"""
Base interfaces for user-defined strategies in MIMIR.

Strategy        - abstract, backtest-oriented: generate_signals(df) -> Series
StaticStrategy  - abstract, live-oriented: rule-based state machine with a
                  declared, tunable parameter space (param_space).
"""

from abc import ABC, abstractmethod
import pandas as pd


class Strategy(ABC):
    """Common backtest interface every strategy must implement."""

    name: str = "base_strategy"

    @abstractmethod
    def generate_signals(self, data: pd.DataFrame) -> pd.Series:
        """Returns a pd.Series of {-1, 0, 1} (or continuous) positions
        aligned to data.index."""
        raise NotImplementedError


class StaticStrategy(Strategy):
    """Rule-based strategy with a declared, tunable parameter space and a
    live tick() state machine.

    Subclasses declare:
        param_space: dict[str, tuple[min, max]]  -- the knobs an RL tuner
                                                      is allowed to move.
    and implement:
        tick(price, features) -> list[str]        -- one live state update
        generate_signals(df) -> pd.Series          -- backtest replay
    """

    param_space: dict = {}

    def get_params(self) -> dict:
        return {k: getattr(self, k) for k in self.param_space}

    def set_params(self, params: dict) -> None:
        for k, v in params.items():
            if k in self.param_space:
                setattr(self, k, v)

    @property
    def state(self) -> str:
        raise NotImplementedError

    @property
    def net_position(self) -> float:
        raise NotImplementedError

    def status_line(self, price: float = 0.0) -> str:
        side = "LONG" if self.net_position > 0 else ("SHORT" if self.net_position < 0 else "FLAT")
        parts = [f"State={self.state}", f"Pos={side}"]
        try:
            params = self.get_params()
            parts.append("Params[" + " ".join(f"{k}={v:.3g}" for k, v in params.items()) + "]")
        except Exception:
            pass
        return " | ".join(parts)

    @abstractmethod
    def tick(self, price: float, features: dict) -> list:
        """Advance the strategy's internal state machine by one bar/tick.

        Args:
            price: latest price.
            features: dict of feature name -> value, e.g. ou_zscore,
                      ewma_vol, ou_theta, rsi_14, sentiment_score.

        Returns:
            List of action strings: "OPEN_LONG", "OPEN_SHORT",
            "CLOSE_LONG", "CLOSE_SHORT" (zero or more per tick).
        """
        raise NotImplementedError

    def sync_from_broker(
        self,
        has_long: bool,
        long_entry: float,
        has_short: bool,
        short_entry: float,
    ) -> None:
        """Resync internal state from actual broker positions, e.g. after
        a bot restart or an externally-triggered stop-loss."""
        pass
