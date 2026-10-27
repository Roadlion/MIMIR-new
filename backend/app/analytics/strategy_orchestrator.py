# backend/app/analytics/strategy_orchestrator.py
"""
MIMIR Strategy Orchestrator & Live Strategy Manager
==================================================
Manages registered quantitative strategies, active strategy state,
and runtime execution authorization for MT5 and Paper Trading.
"""

import json
import logging
from typing import Dict, List, Any, Optional

from backend.app.database import get_db_connection_dict
from backend.app.config import get_settings
from backend.app.analytics.user_strategies.registry import list_strategies, reload_registries

logger = logging.getLogger(__name__)
settings = get_settings()

DEFAULT_ACTIVE_STRATEGIES = ["war_rig"]

# Standard descriptions and icons for built-in quants
STRATEGY_METADATA = {
    "war_rig": {
        "name": "War Rig Transmission Engine",
        "category": "🌟 Flagship Quant Engines",
        "icon": "fas fa-car-side",
        "description": "Unified 3-Cylinder Crankshaft: Macro Inflows + Catalyst Twin-Turbo + Microstructure (>=75% conviction).",
        "is_flagship": True,
    },
    "stat_arb": {
        "name": "Stat-Arb APU (Pairs Trading)",
        "category": "🌟 Flagship Quant Engines",
        "icon": "fas fa-balance-scale",
        "description": "Kalman-filtered cointegrated pairs trading with Ornstein-Uhlenbeck mean-reversion.",
        "is_flagship": True,
    },
    "dual_hedge": {
        "name": "Dual Hedge Statistical Hedging",
        "category": "📈 Built-in Strategies",
        "icon": "fas fa-arrows-split-up-and-left",
        "description": "Dual-leg statistical hedging state-machine (FLAT -> INITIAL -> HEDGED -> SOLO).",
        "is_flagship": False,
    },
    "mean_reversion": {
        "name": "Mean Reversion (OU Z-Score)",
        "category": "📈 Built-in Strategies",
        "icon": "fas fa-wave-square",
        "description": "Pure Ornstein-Uhlenbeck z-score reversion (buys oversold, sells overbought).",
        "is_flagship": False,
    },
    "bollinger_bands": {
        "name": "Bollinger Bands & RSI Regime",
        "category": "📈 Built-in Strategies",
        "icon": "fas fa-chart-line",
        "description": "Bollinger Bands + RSI confirmation with volatility regime adaptation.",
        "is_flagship": False,
    },
    "volume_exhaustion_reversal": {
        "name": "Volume Exhaustion Reversal",
        "category": "📈 Built-in Strategies",
        "icon": "fas fa-bolt",
        "description": "Swing reversal detected after exceptional-volume directional candle exhaustion.",
        "is_flagship": False,
    },
}


def get_active_strategies() -> List[str]:
    """Retrieves the list of currently active strategy IDs from DB."""
    conn = get_db_connection_dict()
    cur = conn.cursor()
    try:
        cur.execute(f"""
            SELECT active_strategies 
            FROM {settings.mimir_schema}.mimir_paper_trading_config 
            ORDER BY id ASC LIMIT 1
        """)
        row = cur.fetchone()
        if row and row.get("active_strategies") is not None:
            val = row["active_strategies"]
            if isinstance(val, str):
                try:
                    return json.loads(val)
                except Exception:
                    return [val]
            elif isinstance(val, list):
                return val
        return list(DEFAULT_ACTIVE_STRATEGIES)
    except Exception as e:
        logger.warning(f"[STRATEGY_ORCHESTRATOR] Error reading active_strategies: {e}")
        return list(DEFAULT_ACTIVE_STRATEGIES)
    finally:
        cur.close()
        conn.close()


def set_active_strategies(active_list: List[str]) -> List[str]:
    """Updates the active strategies list in the database."""
    # Deduplicate while preserving order
    cleaned = []
    for s in active_list:
        s_clean = str(s).strip()
        if s_clean and s_clean not in cleaned:
            cleaned.append(s_clean)
    if not cleaned:
        cleaned = ["war_rig"]

    conn = get_db_connection_dict()
    cur = conn.cursor()
    try:
        cur.execute(f"""
            UPDATE {settings.mimir_schema}.mimir_paper_trading_config
            SET active_strategies = %s::jsonb,
                updated_at = NOW()
            WHERE id = (SELECT id FROM {settings.mimir_schema}.mimir_paper_trading_config ORDER BY id ASC LIMIT 1)
        """, (json.dumps(cleaned),))
        conn.commit()
        return cleaned
    except Exception as e:
        conn.rollback()
        logger.error(f"[STRATEGY_ORCHESTRATOR] Error saving active_strategies: {e}")
        raise e
    finally:
        cur.close()
        conn.close()


def get_strategy_catalog() -> Dict[str, Any]:
    """
    Returns the complete catalog of all available strategies (Flagships, Built-ins,
    and dynamically discovered user/bong_strats files) along with active statuses.
    """
    active_strategies = get_active_strategies()
    catalog = []

    # 1. Add Flagships
    for f_id in ["war_rig", "stat_arb"]:
        meta = STRATEGY_METADATA.get(f_id, {})
        catalog.append({
            "id": f_id,
            "name": meta.get("name", f_id),
            "category": meta.get("category", "🌟 Flagship Quant Engines"),
            "icon": meta.get("icon", "fas fa-robot"),
            "description": meta.get("description", ""),
            "is_active": f_id in active_strategies,
            "is_flagship": True,
            "is_custom": False,
        })

    # 2. Add Discovered Strategies
    try:
        discovered = list_strategies()
        for s in discovered:
            s_id = s["id"]
            if s_id in ("pair_stat_arbitrage", "stat_arb", "war_rig"):
                continue  # Handled above

            meta = STRATEGY_METADATA.get(s_id)
            if meta:
                name = meta["name"]
                cat = meta["category"]
                icon = meta["icon"]
                desc = meta["description"]
                is_custom = False
            else:
                # Custom strategy from bong_strats
                name = s["name"].replace("_", " ").title()
                cat = "🐍 Custom Strategies (bong_strats/)"
                icon = "fab fa-python"
                desc = s.get("description") or f"Custom strategy implementation ({s.get('class_name')})."
                is_custom = True

            catalog.append({
                "id": s_id,
                "name": name,
                "category": cat,
                "icon": icon,
                "description": desc,
                "is_active": s_id in active_strategies,
                "is_flagship": False,
                "is_custom": is_custom,
            })
    except Exception as e:
        logger.warning(f"[STRATEGY_ORCHESTRATOR] Error loading dynamic strategies: {e}")

    return {
        "catalog": catalog,
        "active_strategies": active_strategies,
        "total_available": len(catalog),
        "total_active": len(active_strategies),
    }


def add_active_strategy(strategy_id: str) -> Dict[str, Any]:
    """Adds a strategy to the active running list."""
    current = get_active_strategies()
    strategy_id = strategy_id.strip()
    if strategy_id and strategy_id not in current:
        current.append(strategy_id)
        set_active_strategies(current)
    return get_strategy_catalog()


def remove_active_strategy(strategy_id: str) -> Dict[str, Any]:
    """Removes a strategy from the active running list."""
    current = get_active_strategies()
    strategy_id = strategy_id.strip()
    if strategy_id in current:
        current.remove(strategy_id)
        set_active_strategies(current)
    return get_strategy_catalog()


def toggle_active_strategy(strategy_id: str, is_enabled: bool) -> Dict[str, Any]:
    """Toggles strategy active/inactive."""
    if is_enabled:
        return add_active_strategy(strategy_id)
    else:
        return remove_active_strategy(strategy_id)
