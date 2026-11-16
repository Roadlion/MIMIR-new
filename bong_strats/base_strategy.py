# bong_strats/base_strategy.py
"""
Re-exports the core Strategy and StaticStrategy base classes from MIMIR's
user strategies system so user custom strategies inherit the exact same types.
"""
from backend.app.analytics.user_strategies.base_strategy import Strategy, StaticStrategy

__all__ = ["Strategy", "StaticStrategy"]
