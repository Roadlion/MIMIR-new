# backend/app/analytics/user_strategies/registry.py
"""
Dynamic Strategy Auto-Discovery and Registry System for MIMIR.

Discovers any strategy subclassing `Strategy`, `StaticStrategy`, or `QuickStrategy`
placed anywhere in:
  - `bong_strats/`
  - `bong_strats/strategies/`
  - `backend/app/analytics/user_strategies/`

Supports automatic hot-reloading when strategy files are modified on disk.
"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple, Type

from .base_strategy import Strategy, StaticStrategy
from .quick_strategy import QuickStrategy

# Base built-in static strategies
from .static_strategies import (
    DualHedgeStrategy,
    MeanReversionStrategy,
    BollingerBandStrategy,
    VolumeExhaustionReversalStrategy,
    PairStatArbitrageStrategy,
)

# Core initial registries
_BUILTIN_USER_STRATEGIES: Dict[str, Type[Strategy]] = {
    "dual_hedge": DualHedgeStrategy,
    "mean_reversion": MeanReversionStrategy,
    "bollinger_bands": BollingerBandStrategy,
}

_BUILTIN_BACKTEST_STRATEGIES: Dict[str, Type[Strategy]] = {
    "volume_exhaustion_reversal": VolumeExhaustionReversalStrategy,
    "pair_stat_arbitrage": PairStatArbitrageStrategy,
}

_EXCLUDED_CLASS_NAMES = {
    "Strategy",
    "StaticStrategy",
    "QuickStrategy",
    "DualHedgeStrategy",
    "MeanReversionStrategy",
    "BollingerBandStrategy",
    "VolumeExhaustionReversalStrategy",
    "PairStatArbitrageStrategy",
}

_EXCLUDED_FILE_NAMES = {
    "__init__.py",
    "__main__.py",
    "base_strategy.py",
    "quick_strategy.py",
    "registry.py",
    "indicators.py",
    "rl_tuner.py",
    "static_strategies.py",
    "strategy_runner.py",
    "pair_screener.py",
}

_MODULE_MTIMES: Dict[str, float] = {}


def _load_module_from_path(file_path: Path):
    """
    Dynamically imports or reloads a Python module from file path.
    Detects if the file timestamp changed on disk and reloads it automatically.
    """
    mod_name = f"bong_dynamic_{file_path.stem}"
    try:
        mtime = file_path.stat().st_mtime
    except Exception:
        mtime = 0.0

    if mod_name in sys.modules:
        if _MODULE_MTIMES.get(mod_name) == mtime:
            return sys.modules[mod_name]
        try:
            # File on disk changed! Reload in-memory so edits take effect immediately
            mod = importlib.reload(sys.modules[mod_name])
            _MODULE_MTIMES[mod_name] = mtime
            return mod
        except Exception:
            sys.modules.pop(mod_name, None)

    try:
        spec = importlib.util.spec_from_file_location(mod_name, str(file_path))
        if spec and spec.loader:
            module = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = module
            spec.loader.exec_module(module)
            _MODULE_MTIMES[mod_name] = mtime
            return module
    except Exception as exc:
        print(f"[STRATEGY REGISTRY WARNING] Failed to load strategy file {file_path.name}: {exc}")
    return None


def discover_strategies() -> Tuple[Dict[str, Type[Strategy]], Dict[str, Type[Strategy]]]:
    """
    Scans folders for strategy implementations and returns:
      (user_strategies, backtest_strategies)
    """
    user_strats: Dict[str, Type[Strategy]] = dict(_BUILTIN_USER_STRATEGIES)
    backtest_strats: Dict[str, Type[Strategy]] = dict(_BUILTIN_BACKTEST_STRATEGIES)

    project_root = Path(__file__).resolve().parent.parent.parent.parent
    scan_dirs = [
        project_root / "bong_strats",
        project_root / "bong_strats" / "strategies",
        project_root / "backend" / "app" / "analytics" / "user_strategies",
    ]

    for s_dir in scan_dirs:
        if not s_dir.exists() or not s_dir.is_dir():
            continue

        for py_file in s_dir.glob("*.py"):
            if py_file.name in _EXCLUDED_FILE_NAMES:
                continue

            mod = _load_module_from_path(py_file)
            if not mod:
                continue

            for attr_name, attr_val in inspect.getmembers(mod, inspect.isclass):
                if attr_name in _EXCLUDED_CLASS_NAMES:
                    continue

                if issubclass(attr_val, Strategy):
                    # Resolve unique strategy identifier key
                    strat_name = getattr(attr_val, "name", None)
                    if not strat_name or strat_name in ("base_strategy", "quick_strategy"):
                        strat_name = py_file.stem

                    strat_key = strat_name.lower().strip()
                    is_live = getattr(attr_val, "is_live_ready", True)
                    is_bt_only = getattr(attr_val, "backtest_only", False)

                    if is_live and not is_bt_only:
                        user_strats[strat_key] = attr_val
                    else:
                        backtest_strats[strat_key] = attr_val

    return user_strats, backtest_strats


# Dynamic live registries
USER_STRATEGY_REGISTRY, BACKTEST_STRATEGY_REGISTRY = discover_strategies()
FULL_STRATEGY_REGISTRY = {**USER_STRATEGY_REGISTRY, **BACKTEST_STRATEGY_REGISTRY}


def reload_registries():
    """Forces rescanning of strategy directories and reloads modified modules."""
    global USER_STRATEGY_REGISTRY, BACKTEST_STRATEGY_REGISTRY, FULL_STRATEGY_REGISTRY
    USER_STRATEGY_REGISTRY, BACKTEST_STRATEGY_REGISTRY = discover_strategies()
    FULL_STRATEGY_REGISTRY = {**USER_STRATEGY_REGISTRY, **BACKTEST_STRATEGY_REGISTRY}
    return FULL_STRATEGY_REGISTRY


def list_strategies() -> List[Dict[str, Any]]:
    """Returns a structured catalog of all discovered strategies."""
    reload_registries()
    items = []
    for name, cls in FULL_STRATEGY_REGISTRY.items():
        doc = getattr(cls, "description", None) or inspect.getdoc(cls) or "No description."
        first_line = doc.strip().split("\n")[0]
        declared_params = {}
        if hasattr(cls, "get_declared_params"):
            declared_params = cls.get_declared_params()
        elif hasattr(cls, "param_space"):
            declared_params = getattr(cls, "param_space", {})

        items.append({
            "id": name,
            "name": getattr(cls, "name", name),
            "class_name": cls.__name__,
            "description": first_line,
            "is_live_ready": name in USER_STRATEGY_REGISTRY,
            "params": declared_params,
        })
    return items
