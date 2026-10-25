"""
Strategy-agnostic live execution loop. Pairs one StaticStrategy with an
optional RLParameterTuner. Replaces the old LiveDualHedgeManager, which
hardcoded DualHedge's state machine directly into the trader script.

Swapping strategies (dual_hedge <-> mean_reversion <-> any future one)
or turning RL tuning on/off never touches this class.
"""

from base import StaticStrategy


class LiveRunner:
    def __init__(self, strategy: StaticStrategy, tuner=None):
        self.strategy = strategy
        self.tuner = tuner

    def sync_from_broker(self, has_long, long_entry, has_short, short_entry, *args, **kwargs):
        self.strategy.sync_from_broker(has_long, long_entry, has_short, short_entry, *args, **kwargs)

    def on_tick(self, price: float, features: dict, balance_dd: float = 0.0) -> list:
        """One live step: retune params (only while FLAT), then advance
        the strategy's state machine and return MT5 actions to execute."""
        if self.tuner is not None and self.strategy.state == "FLAT":
            params = self.tuner.predict_params(features, balance_dd)
            if params:
                self.strategy.set_params(params)
        return self.strategy.tick(price, features)

    def status_line(self, price: float = 0.0) -> str:
        if hasattr(self.strategy, "status_line"):
            return self.strategy.status_line(price)
        return f"State={self.strategy.state} | Pos={self.strategy.net_position}"
