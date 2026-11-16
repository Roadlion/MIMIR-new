# backend/app/analytics/chain_validator.py
"""
MIMIR Empirical Relationship Validator & Recalibration Engine (v2)
==================================================================
Empirically evaluates the forward predictive accuracy of asset relationships
in mimir_asset_relationships:

1. Queries past spillover impacts generated between source and target over the last 90 days.
2. Benchmarks the 5-day post-activation price return of the target against its predicted direction.
3. If empirical hit rate >= 55% (over >= 3 events): Promotes confidence score and validates edge.
4. If empirical hit rate < 40% (over >= 3 events): Deactivates edge (auto-pruning).
5. For high-conviction curated seeds with insufficient events (< 3): Preserves seed confidence.
"""

import sys
import logging
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Any, Optional
import pandas as pd
import numpy as np

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))
    from backend.app.database import get_db_connection
    from backend.app.config import get_settings
else:
    from ..database import get_db_connection
    from ..config import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)


def calculate_edge_empirical_hit_rate(
    src_key: str,
    tgt_key: str,
    cur,
    lookback_days: int = 90
) -> Optional[float]:
    """
    Calculates empirical hit rate for a relationship edge over the lookback window.
    Compares spillover sentiment direction with target's 5-day forward return.
    Returns float hit_rate in [0.0, 1.0] or None if insufficient sample size (< 3).
    """
    schema = settings.mimir_schema

    # 1. Fetch mature spillover impacts between this pair
    cur.execute(f"""
        SELECT si.impact_id, si.sentiment_score, si.direction, si.activation_date
        FROM {schema}.mimir_sentiment_impacts si
        WHERE si.ticker = %s
          AND (si.spillover_source_asset = %s OR si.spillover_source_asset ILIKE %s)
          AND si.is_spillover = TRUE
          AND si.activation_date >= NOW() - INTERVAL '{int(lookback_days)} days'
          AND si.activation_date <= NOW() - INTERVAL '5 days'
        ORDER BY si.activation_date ASC;
    """, (tgt_key.upper(), src_key.upper(), f"%{src_key}%"))

    impact_rows = cur.fetchall()
    if len(impact_rows) < 3:
        return None

    # 2. Fetch daily OHLCV prices for target
    cur.execute(f"""
        SELECT date, close
        FROM {schema}.v_mimir_daily_ohlcv
        WHERE ticker = %s
          AND date >= CURRENT_DATE - INTERVAL '{int(lookback_days + 15)} days'
        ORDER BY date ASC;
    """, (tgt_key.upper(),))

    price_rows = cur.fetchall()
    if len(price_rows) < 10:
        return None

    df_prices = pd.DataFrame(price_rows, columns=['date', 'close'])
    df_prices['date'] = pd.to_datetime(df_prices['date']).dt.date
    df_prices.set_index('date', inplace=True)
    df_prices['close'] = pd.to_numeric(df_prices['close'], errors='coerce')

    hits = 0
    total = 0

    for imp_id, score, direction, act_dt in impact_rows:
        if not act_dt:
            continue
        eval_date = act_dt.date() if hasattr(act_dt, 'date') else act_dt

        # Find entry date (first trading date >= eval_date)
        future_dates = [d for d in df_prices.index if d >= eval_date]
        if len(future_dates) < 5:
            continue

        p_entry = float(df_prices.loc[future_dates[0]]['close'])
        p_exit = float(df_prices.loc[future_dates[min(4, len(future_dates)-1)]]['close'])

        if p_entry <= 0:
            continue

        pct_change = (p_exit - p_entry) / p_entry * 100.0
        score_val = float(score)

        # Hit criteria: price moved in the predicted direction
        if score_val > 0.05 and pct_change >= 0.50:
            hits += 1
        elif score_val < -0.05 and pct_change <= -0.50:
            hits += 1
        total += 1

    if total < 3:
        return None

    return float(hits / total)


def validate_and_recalibrate_chains(conn=None) -> Dict[str, Any]:
    """
    Weekly feedback loop:
    1. Evaluates empirical hit-rate for all active and quarantined relationships.
    2. Promotes hit_rate >= 0.55 to is_validated = TRUE, increases confidence.
    3. Deprecates hit_rate < 0.40 to is_active = FALSE (prunes dead edges).
    """
    close_conn = False
    if conn is None:
        conn = get_db_connection()
        close_conn = True
    cur = conn.cursor()

    promoted = 0
    deprecated = 0
    preserved = 0
    evaluated = 0

    schema = settings.mimir_schema

    try:
        cur.execute(f"""
            SELECT id, source_key, target_key, chain_tier, confidence_score,
                   validation_source, discovery_source, is_active
            FROM {schema}.mimir_asset_relationships
            WHERE is_active = TRUE OR discovery_source = 'llm_gate3'
        """)
        relationships = cur.fetchall()

        for rel_id, src_key, tgt_key, tier, conf, val_src, disc_src, is_act in relationships:
            evaluated += 1

            # High-conviction institutional seeds (SEC 10-K curated) are verified ground truth;
            # preserve them and avoid pruning from legacy pre-v2 impacts.
            if val_src in ('SEC_10K_CURATED', 'SECTOR_MAP') or disc_src == 'institutional_seed':
                preserved += 1
                continue

            # Empirical hit rate calculation for dynamic / discovered edges
            empirical_hr = calculate_edge_empirical_hit_rate(src_key, tgt_key, cur)

            if empirical_hr is not None:
                if empirical_hr >= 0.55:
                    new_conf = min(0.95, float(conf or 0.50) + 0.05)
                    cur.execute(f"""
                        UPDATE {schema}.mimir_asset_relationships
                        SET is_validated = TRUE,
                            is_active = TRUE,
                            confidence_score = %s,
                            last_validated = NOW(),
                            economic_rationale = COALESCE(economic_rationale, '') || ' [Empirically Validated: ' || %s || '%% hit rate]'
                        WHERE id = %s
                    """, (new_conf, f"{empirical_hr*100:.0f}", rel_id))
                    promoted += 1
                elif empirical_hr < 0.40:
                    cur.execute(f"""
                        UPDATE {schema}.mimir_asset_relationships
                        SET is_active = FALSE,
                            is_validated = FALSE,
                            last_validated = NOW(),
                            economic_rationale = COALESCE(economic_rationale, '') || ' [Empirically Pruned: ' || %s || '%% hit rate]'
                        WHERE id = %s
                    """, (f"{empirical_hr*100:.0f}", rel_id))
                    deprecated += 1
                else:
                    preserved += 1
            else:
                # Curated seed edge with insufficient sample: preserve seed confidence
                if val_src in ('SEC_10K_CURATED', 'SECTOR_MAP') or disc_src == 'institutional_seed':
                    preserved += 1
                else:
                    # Unverified candidate remains in quarantine
                    preserved += 1

        conn.commit()
        logger.info(
            f"[CHAIN_VALIDATOR] Empirical validation complete: {evaluated} evaluated, "
            f"{promoted} promoted, {deprecated} pruned, {preserved} preserved."
        )
        return {
            "evaluated": evaluated,
            "promoted": promoted,
            "deprecated": deprecated,
            "preserved": preserved,
            "status": "SUCCESS"
        }
    except Exception as e:
        conn.rollback()
        logger.error(f"[CHAIN_VALIDATOR] Error during chain validation: {e}")
        return {"error": str(e), "status": "FAILED"}
    finally:
        cur.close()
        if close_conn:
            conn.close()


if __name__ == "__main__":
    res = validate_and_recalibrate_chains()
    print("Chain validation results:", res)
