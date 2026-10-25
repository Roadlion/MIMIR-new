"""
Base interfaces.

Strategy        - abstract, backtest-oriented: generate_signals(df) -> Series
StaticStrategy  - abstract, live-oriented: rule-based state machine with a
                  declared, tunable parameter space (param_space). Any RL
                  tuner can retune ANY StaticStrategy without knowing its
                  internals, because it only ever reads/writes param_space
                  keys. This is what makes strategies and the RL layer
                  independently swappable.
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

    `state` and `net_position` let the live runner / RL tuner know when
    it's safe to retune params (only while state == "FLAT", so an open
    trade's exit target never moves mid-position).
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

    @abstractmethod
    def tick(self, price: float, features: dict) -> list:
        """Advance the strategy's internal state machine by one bar/tick.

        Args:
            price: latest price.
            features: dict of feature name -> value, e.g. ou_zscore,
                      ewma_vol, ou_theta, rsi_14, sentiment_score. A
                      strategy only reads the keys it cares about, so
                      new feature sources (e.g. sentiment) can be added
                      to the dict without breaking existing strategies.

        Returns:
            List of action strings: "OPEN_LONG", "OPEN_SHORT",
            "CLOSE_LONG", "CLOSE_SHORT" (zero or more per tick).
        """
        raise NotImplementedError

    def sync_from_broker(self, has_long: bool, long_entry: float,
                          has_short: bool, short_entry: float) -> None:
        """Resync internal state from actual broker positions, e.g. after
        a bot restart or an externally-triggered stop-loss. Default is a
        no-op; strategies that track entry state override this."""
        pass
