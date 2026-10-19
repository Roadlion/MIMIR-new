# backend/app/analytics/user_strategies/strategy_runner.py
"""
Bridge between live price daemon tick stream (price_cache) and isolated user custom strategies.
Emits strategy signals directly into mimir_trade_signals table for paper trader auto-execution.
"""

from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List
import pandas as pd

try:
    from backend.app.database import get_db_connection
    from backend.app.config import get_settings
    settings = get_settings()
    HAS_DB = True
except Exception:
    get_db_connection = None
    get_settings = None
    settings = None
    HAS_DB = False

from .static_strategies import USER_STRATEGY_REGISTRY
from .indicators import (
    compute_ou_features,
    compute_bollinger_features,
    compute_rsi,
    compute_volatility_regime,
    VolatilityModel,
)

# In-memory dictionary holding active strategy instances per ticker
# Structure: { ticker: { strategy_name: StrategyInstance } }
_ACTIVE_USER_STRATEGIES: Dict[str, Dict[str, Any]] = {}


def get_or_create_strategy_instances(ticker: str) -> Dict[str, Any]:
    ticker_clean = ticker.upper().strip()
    if ticker_clean not in _ACTIVE_USER_STRATEGIES:
        _ACTIVE_USER_STRATEGIES[ticker_clean] = {}
        for name, cls in USER_STRATEGY_REGISTRY.items():
            _ACTIVE_USER_STRATEGIES[ticker_clean][name] = cls(mode="adaptive")
    return _ACTIVE_USER_STRATEGIES[ticker_clean]


def emit_user_strategy_signal(
    ticker: str,
    action: str,
    price: float,
    strategy_name: str,
    reason_detail: str,
):
    """
    [DECOMMISSIONED FOR DIRECT EXECUTION & DB EMISSION]
    Standalone user strategy signals are deactivated to prevent uncontrolled market execution and queue spam.
    Only the unified War Rig Transmission Engine is authorized for trading.
    """
    print(f"[USER_STRATEGY DECOMMISSIONED] Blocked {action} for {ticker} ({strategy_name}). Only War Rig is authorized.")
    return


def run_user_strategies(price_cache: Dict[str, Any]):
    """
    Called by live_price_daemon.py whenever new MT5 tick data arrives.
    Evaluates all registered user custom strategies per updated ticker.
    """
    for ticker, deque_data in price_cache.items():
        if not deque_data or len(deque_data) < 10:
            continue

        ticker_clean = ticker.upper().strip()
        df = pd.DataFrame(list(deque_data))

        # Compute technical indicators for strategy features
        df = compute_ou_features(df, lookback=min(50, len(df) - 1))
        df = compute_bollinger_features(df)
        df["rsi_14"] = compute_rsi(df["close"])
        df["ewma_vol"] = VolatilityModel.ewma(df["close"].pct_change(), span=20)
        df["volatility_regime"] = compute_volatility_regime(df)

        latest_bar = df.iloc[-1]
        price = float(latest_bar["close"])

        features = {
            "ou_zscore": (
                float(latest_bar["ou_zscore"])
                if not pd.isna(latest_bar["ou_zscore"])
                else 0.0
            ),
            "ewma_vol": (
                float(latest_bar["ewma_vol"])
                if not pd.isna(latest_bar["ewma_vol"])
                else 0.01
            ),
            "bb_upper": float(latest_bar.get("bb_upper", price)),
            "bb_lower": float(latest_bar.get("bb_lower", price)),
            "bb_middle": float(latest_bar.get("bb_middle", price)),
            "rsi_14": float(latest_bar.get("rsi_14", 50.0)),
            "volatility_regime": str(
                latest_bar.get("volatility_regime", "STABLE")
            ),
        }

        strats = get_or_create_strategy_instances(ticker_clean)

        for strat_name, strategy in strats.items():
            try:
                actions = strategy.tick(price, features)
                for act in actions:
                    status_desc = strategy.status_line(price)
                    emit_user_strategy_signal(
                        ticker_clean, act, price, strat_name, status_desc
                    )
            except Exception as e:
                print(
                    f"[USER_STRATEGY ERROR] Strategy {strat_name} failed on {ticker_clean}: {e}"
                )
