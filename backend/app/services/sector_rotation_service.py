# backend/app/services/sector_rotation_service.py
"""
MIMIR Institutional Sector Rotation & Capital Inflow Engine
===========================================================
Detects institutional capital rotation across the 11 major US market sectors
BEFORE retail discovers them.

Institutions manage billions of dollars and cannot sit in cash due to benchmark
mandates. When they rotate out of an overbought sector (e.g. Technology), they
reallocate into consolidating, undervalued, or emerging sectors.

Key Early Footprints Tracked:
1. Relative Strength (RS) Ratio vs SPY: Delta(Sector / SPY) over 5-day and 20-day windows.
2. Chaikin Money Flow (CMF): Tracks whether volume is accumulating on up-days inside a tight base.
3. DeepSeek Narrative Velocity: Sector sentiment shifts 48-72h before retail headlines.
4. Institutional Phase Classification:
   - STEALTH_ACCUMULATION: RS inflection upwards, low volatility base, positive CMF. (EARLY ALERT)
   - MARKUP: High momentum expansion, above 20-day SMA, retail chasing.
   - DISTRIBUTION: Smart money taking profits, RS turning negative, CMF outflow.
   - OUTFLOW: Underperforming SPY, negative drift.
"""

import logging
import math
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Any, Tuple
import pandas as pd
import numpy as np

from ..database import get_db_connection
from ..config import get_settings
from .macro_tracker import is_market_regime_bullish

logger = logging.getLogger(__name__)
settings = get_settings()
_SCHEMA = settings.mimir_schema

# Fast in-memory caches to prevent 10,000 redundant database queries in backtests & scans
_MATRIX_CACHE: Dict[str, Any] = {"data": None, "ts": 0.0}
_TICKER_SECTOR_MAP: Dict[str, Optional[str]] = {}

# 11 SPDR Sector ETFs mapped to database asset_sub_category names
SECTOR_ETFS = {
    "XLK":  {"sub_category": "TECHNOLOGY",              "name": "Technology"},
    "XLE":  {"sub_category": "ENERGY",                  "name": "Energy"},
    "XLF":  {"sub_category": "FINANCIAL_SERVICES",      "name": "Financials"},
    "XLV":  {"sub_category": "HEALTHCARE",              "name": "Healthcare"},
    "XLI":  {"sub_category": "INDUSTRIALS",             "name": "Industrials"},
    "XLU":  {"sub_category": "UTILITIES",               "name": "Utilities"},
    "XLP":  {"sub_category": "CONSUMER_DEFENSIVE",      "name": "Consumer Staples"},
    "XLY":  {"sub_category": "CONSUMER_CYCLICAL",       "name": "Consumer Discretionary"},
    "XLC":  {"sub_category": "COMMUNICATION_SERVICES",  "name": "Communication Services"},
    "XLB":  {"sub_category": "BASIC_MATERIALS",         "name": "Basic Materials"},
    "XLRE": {"sub_category": "REAL_ESTATE",             "name": "Real Estate"},
}

SUB_CATEGORY_TO_ETF = {v["sub_category"]: k for k, v in SECTOR_ETFS.items()}


def _calculate_cmf(df_bars: pd.DataFrame, period: int = 14) -> float:
    """
    Calculates Chaikin Money Flow (CMF) over N bars.
    CMF = Sum(((Close - Low) - (High - Close)) / (High - Low) * Volume) / Sum(Volume)
    Positive CMF (> 0) indicates institutional accumulation.
    """
    if len(df_bars) < period:
        return 0.0
    
    sub = df_bars.tail(period)
    high = sub['high']
    low = sub['low']
    close = sub['close']
    volume = sub['volume']
    
    hl_range = high - low
    # Avoid zero division
    hl_range = hl_range.replace(0, 0.0001)
    mf_multiplier = ((close - low) - (high - close)) / hl_range
    mf_volume = mf_multiplier * volume
    
    vol_sum = volume.sum()
    if vol_sum <= 0:
        return 0.0
    return float(mf_volume.sum() / vol_sum)


def get_sector_rotation_matrix(conn=None) -> Dict[str, Any]:
    """
    Fetches daily price data for SPY and all 11 Sector ETFs,
    computes Relative Strength (RS), Money Flow (CMF), and DeepSeek sentiment,
    and returns a structured Sector Rotation Matrix.
    """
    global _MATRIX_CACHE
    now_ts = time.time()
    if _MATRIX_CACHE["data"] is not None and (now_ts - _MATRIX_CACHE["ts"] < 300.0):
        return _MATRIX_CACHE["data"]

    close_conn = False
    if conn is None:
        conn = get_db_connection()
        close_conn = True

    cur = conn.cursor()
    all_tickers = list(SECTOR_ETFS.keys()) + ["SPY"]

    try:
        # 1. Fetch daily OHLCV for all sector ETFs and SPY for last 60 days
        sql_prices = f"""
            SELECT ticker, date, open, high, low, close, volume
            FROM {_SCHEMA}.v_mimir_daily_ohlcv
            WHERE ticker = ANY(%s)
              AND date >= CURRENT_DATE - INTERVAL '60 days'
            ORDER BY ticker, date ASC
        """
        cur.execute(sql_prices, (all_tickers,))
        rows = cur.fetchall()
        
        if not rows:
            logger.warning("[SECTOR_ROTATION] No daily OHLCV data found for sector ETFs.")
            return {"sectors": [], "spy": {}, "as_of": datetime.now(timezone.utc).isoformat()}

        df_all = pd.DataFrame(rows, columns=['ticker', 'date', 'open', 'high', 'low', 'close', 'volume'])
        df_all['date'] = pd.to_datetime(df_all['date'])
        for col in ['open', 'high', 'low', 'close', 'volume']:
            df_all[col] = pd.to_numeric(df_all[col], errors='coerce')

        piv_close = df_all.pivot(index='date', columns='ticker', values='close').ffill()
        if 'SPY' not in piv_close.columns or len(piv_close['SPY'].dropna()) < 5:
            logger.warning("[SECTOR_ROTATION] Insufficient SPY price history.")
            return {"sectors": [], "spy": {}, "as_of": datetime.now(timezone.utc).isoformat()}

        spy_s = piv_close['SPY'].dropna()
        spy_last = float(spy_s.iloc[-1])
        spy_5d_ret = float(((spy_s.iloc[-1] / spy_s.iloc[-5]) - 1) * 100) if len(spy_s) >= 5 else 0.0
        spy_20d_ret = float(((spy_s.iloc[-1] / spy_s.iloc[0]) - 1) * 100) if len(spy_s) >= 20 else 0.0

        # 2. Fetch DeepSeek 7-day sentiment by asset_sub_category
        sql_sent = f"""
            SELECT si.asset_sub_category,
                   AVG(si.sentiment_score) as avg_sent,
                   COUNT(DISTINCT a.id) as art_count
            FROM {_SCHEMA}.mimir_raw_articles a
            JOIN {_SCHEMA}.mimir_sentiment_impacts si ON si.article_id = a.id
            WHERE a.published_ts >= NOW() - INTERVAL '7 days'
              AND si.asset_sub_category IS NOT NULL
            GROUP BY si.asset_sub_category
        """
        sent_rows = cur.fetchall()
        sent_map = {}
        for r in sent_rows:
            sub = r.get("asset_sub_category") if isinstance(r, dict) else r[0]
            avg_s = r.get("avg_sent") if isinstance(r, dict) else r[1]
            cnt = r.get("art_count") if isinstance(r, dict) else r[2]
            if sub:
                sent_map[sub.strip().upper()] = {"avg_sent": float(avg_s), "count": int(cnt)}

        sectors_out = []
        for etf, meta in SECTOR_ETFS.items():
            if etf not in piv_close.columns:
                continue

            s_close = piv_close[etf].dropna()
            if len(s_close) < 5:
                continue

            last_close = float(s_close.iloc[-1])
            ret_5d = float(((s_close.iloc[-1] / s_close.iloc[-5]) - 1) * 100)
            ret_20d = float(((s_close.iloc[-1] / s_close.iloc[0]) - 1) * 100) if len(s_close) >= 20 else ret_5d

            # Relative Strength (RS) vs SPY: Ratio = Sector / SPY
            ratio_s = s_close / spy_s.reindex(s_close.index)
            rs_5d = float(((ratio_s.iloc[-1] / ratio_s.iloc[-5]) - 1) * 100) if len(ratio_s) >= 5 else 0.0
            rs_20d = float(((ratio_s.iloc[-1] / ratio_s.iloc[0]) - 1) * 100) if len(ratio_s) >= 20 else rs_5d

            # Technical trend
            sma_20 = float(s_close.rolling(20, min_periods=5).mean().iloc[-1])
            above_sma20 = bool(last_close >= sma_20)

            # Chaikin Money Flow (CMF) on ETF bars
            etf_bars = df_all[df_all['ticker'] == etf].sort_values('date')
            cmf_14 = _calculate_cmf(etf_bars, period=14)

            # DeepSeek sentiment
            sub_cat = meta["sub_category"]
            sent_info = sent_map.get(sub_cat, {"avg_sent": 0.0, "count": 0})
            avg_sentiment = sent_info["avg_sent"]
            sentiment_volume = sent_info["count"]

            # ── Institutional Lifecycle Phase Classification ──────────────
            if rs_5d >= 1.0 and ret_5d <= 4.0 and (cmf_14 >= -0.02 or avg_sentiment >= 0.10):
                phase = "STEALTH_ACCUMULATION"
                summary = f"Institutions quietly absorbing shares (+{rs_5d:.1f}% RS vs SPY) while price base is tight. Early entry window."
            elif rs_5d > 1.2 and ret_5d > 3.0 and above_sma20:
                phase = "MARKUP"
                summary = f"Markup phase underway (+{ret_5d:.1f}% 5D). Strong institutional trend; retail momentum chasing."
            elif rs_5d >= 0.3:
                phase = "ACCUMULATION"
                summary = f"Positive institutional interest (+{rs_5d:.1f}% RS vs SPY). Outperforming broad market base."
            elif rs_5d >= -0.5:
                phase = "CONSOLIDATION"
                summary = f"Neutral base / tracking market ({rs_5d:+.1f}% RS vs SPY). Stable sector foundation."
            elif rs_5d >= -1.2:
                phase = "IMPROVING"
                summary = f"Mild sector lag ({rs_5d:+.1f}% RS vs SPY). Sector consolidating without severe liquidation."
            elif rs_5d < -0.8 and ret_20d > 2.0:
                phase = "DISTRIBUTION"
                summary = f"Smart money taking profits. RS fading ({rs_5d:.1f}%) despite previous gains. Long risk elevated."
            else:
                phase = "OUTFLOW"
                summary = f"Severe sector lag ({rs_5d:.1f}% RS vs SPY). Capital rotating out into stronger sectors."

            sectors_out.append({
                "ticker": etf,
                "sector": sub_cat,
                "name": meta["name"],
                "last_close": round(last_close, 2),
                "return_5d_pct": round(ret_5d, 2),
                "return_20d_pct": round(ret_20d, 2),
                "rs_vs_spy_5d_pct": round(rs_5d, 2),
                "rs_vs_spy_20d_pct": round(rs_20d, 2),
                "cmf_14": round(cmf_14, 3),
                "avg_sentiment_7d": round(avg_sentiment, 3),
                "sentiment_article_count": sentiment_volume,
                "phase": phase,
                "above_sma20": above_sma20,
                "summary": summary
            })

        # Sort by 5-day Relative Strength alpha descending
        sectors_out.sort(key=lambda x: x["rs_vs_spy_5d_pct"], reverse=True)

        result_matrix = {
            "as_of": datetime.now(timezone.utc).isoformat(),
            "spy": {
                "last_close": round(spy_last, 2),
                "return_5d_pct": round(spy_5d_ret, 2),
                "return_20d_pct": round(spy_20d_ret, 2)
            },
            "sectors": sectors_out
        }
        _MATRIX_CACHE["data"] = result_matrix
        _MATRIX_CACHE["ts"] = time.time()
        return result_matrix
    except Exception as e:
        logger.error(f"[SECTOR_ROTATION] Error computing sector rotation matrix: {e}")
        return {"sectors": [], "spy": {}, "as_of": datetime.now(timezone.utc).isoformat()}
    finally:
        cur.close()
        if close_conn:
            conn.close()


def get_ticker_sector_tailwinds(ticker: str, conn=None) -> Dict[str, Any]:
    """
    Checks whether a specific equity ticker has institutional sector tailwinds or headwinds.
    """
    ticker = ticker.strip().upper()
    close_conn = False
    if conn is None:
        conn = get_db_connection()
        close_conn = True

    cur = conn.cursor()
    try:
        global _TICKER_SECTOR_MAP
        if ticker in _TICKER_SECTOR_MAP:
            sub_cat = _TICKER_SECTOR_MAP[ticker]
        else:
            cur.execute(f"""
                SELECT asset_sub_category
                FROM {_SCHEMA}.mimir_sentiment_impacts
                WHERE ticker = %s AND asset_sub_category IS NOT NULL
                ORDER BY created_at DESC
                LIMIT 1
            """, (ticker,))
            row = cur.fetchone()
            if row:
                raw_val = row.get("asset_sub_category") if isinstance(row, dict) else row[0]
                sub_cat = raw_val.strip().upper() if raw_val else None
            else:
                sub_cat = None
            _TICKER_SECTOR_MAP[ticker] = sub_cat

        if not sub_cat or sub_cat not in SUB_CATEGORY_TO_ETF:
            return {
                "has_tailwind": False,
                "is_outflow": False,
                "sector_name": "Uncategorized",
                "sector_etf": None,
                "phase": "NEUTRAL",
                "rs_5d": 0.0,
                "reason": f"{ticker}: Sector mapping not found; neutral baseline."
            }

        etf = SUB_CATEGORY_TO_ETF[sub_cat]
        matrix = get_sector_rotation_matrix(conn=conn)
        sector_entry = next((s for s in matrix.get("sectors", []) if s["ticker"] == etf), None)

        if not sector_entry:
            return {
                "has_tailwind": False,
                "is_outflow": False,
                "sector_name": sub_cat,
                "sector_etf": etf,
                "phase": "NEUTRAL",
                "rs_5d": 0.0,
                "reason": f"{ticker}: No rotation metrics available for {etf}."
            }

        phase = sector_entry["phase"]
        rs_5d = sector_entry["rs_vs_spy_5d_pct"]
        name = sector_entry["name"]

        has_tailwind = phase in ("STEALTH_ACCUMULATION", "MARKUP", "ACCUMULATION") and rs_5d >= 0.3
        is_outflow = (phase in ("OUTFLOW", "DISTRIBUTION") and rs_5d < -0.8) or rs_5d < -1.5

        if phase == "STEALTH_ACCUMULATION":
            reason = f"Institutional Capital Inflow: Parent sector {name} ({etf}) in STEALTH ACCUMULATION (+{rs_5d:.1f}% RS vs SPY)."
        elif phase == "MARKUP":
            reason = f"Strong Sector Momentum: Parent sector {name} ({etf}) in MARKUP (+{rs_5d:.1f}% RS vs SPY)."
        elif phase == "ACCUMULATION":
            reason = f"Steady Sector Inflow: Parent sector {name} ({etf}) in ACCUMULATION (+{rs_5d:.1f}% RS vs SPY)."
        elif phase == "CONSOLIDATION":
            reason = f"Neutral Sector Base: Parent sector {name} ({etf}) tracking broad market ({rs_5d:+.1f}% RS vs SPY)."
        elif phase == "IMPROVING":
            reason = f"Mild Sector Consolidation: Parent sector {name} ({etf}) mildly lagging ({rs_5d:+.1f}% RS vs SPY)."
        elif phase == "DISTRIBUTION":
            reason = f"Institutional Headwind: Parent sector {name} ({etf}) in DISTRIBUTION (-{abs(rs_5d):.1f}% RS vs SPY)."
        else:
            reason = f"Sector Outflow: Capital currently leaving {name} ({etf}) for leading sectors."

        return {
            "has_tailwind": has_tailwind,
            "is_outflow": is_outflow,
            "sector_name": name,
            "sector_etf": etf,
            "phase": phase,
            "rs_5d": rs_5d,
            "reason": reason
        }
    except Exception as e:
        logger.warning(f"[SECTOR_ROTATION] Error resolving tailwind for {ticker}: {e}")
        return {
            "has_tailwind": False,
            "is_outflow": False,
            "sector_name": "Error",
            "sector_etf": None,
            "phase": "UNKNOWN",
            "rs_5d": 0.0,
            "reason": str(e)
        }
    finally:
        cur.close()
        if close_conn:
            conn.close()


def scan_sector_rotation_runners(conn=None) -> List[Dict[str, Any]]:
    """
    Scans for institutional "home run" runners inside sectors undergoing
    STEALTH ACCUMULATION or early MARKUP.
    Pairs sector rotation tailwinds with individual stock consolidation/breakout setups.
    """
    close_conn = False
    if conn is None:
        conn = get_db_connection()
        close_conn = True

    cur = conn.cursor()
    generated_signals = []

    try:
        from ..analytics.catalyst_engine import (
            CatalystType, calculate_ta_execution_bounds,
            check_existing_catalyst_signal, insert_catalyst_trade_signal
        )
        from ..analytics.signal_fusion import get_recent_prices, build_thesis_from_reasonings
        from ..analytics.paper_trader import is_us_stock

        # 1. Get live Sector Rotation Matrix
        matrix = get_sector_rotation_matrix(conn=conn)
        accum_sectors = [
            s for s in matrix.get("sectors", [])
            if s["phase"] in ("STEALTH_ACCUMULATION", "MARKUP") and s["rs_vs_spy_5d_pct"] >= 1.0
        ]

        if not accum_sectors:
            logger.info("[SECTOR_ROTATION] No sectors currently in high-alpha accumulation phase.")
            return []

        top_sector_names = [s["sector"] for s in accum_sectors]
        logger.info(f"[SECTOR_ROTATION] Active Accumulating Sectors: {top_sector_names}")

        # 2. Query top bullish stocks in these accumulating sectors from last 7 days
        sql_stocks = f"""
            SELECT si.ticker, si.asset_sub_category,
                   AVG(si.sentiment_score) as avg_sent,
                   AVG(si.confidence) as avg_conf,
                   STRING_AGG(DISTINCT a.title, ' | ') as headlines
            FROM {_SCHEMA}.mimir_raw_articles a
            JOIN {_SCHEMA}.mimir_sentiment_impacts si ON si.article_id = a.id
            WHERE a.published_ts >= NOW() - INTERVAL '7 days'
              AND si.asset_sub_category = ANY(%s)
              AND si.sentiment_score >= 0.40
              AND si.ticker IS NOT NULL
            GROUP BY si.ticker, si.asset_sub_category
            HAVING COUNT(DISTINCT a.id) >= 1
            ORDER BY avg_sent DESC
            LIMIT 25
        """
        cur.execute(sql_stocks, (top_sector_names,))
        candidates = cur.fetchall()

        for ticker, sub_cat, avg_sent, avg_conf, headlines in candidates:
            ticker = ticker.strip().upper()
            if not is_us_stock(ticker):
                continue

            # Deduplication: don't alert if existing signal in last 24h
            if check_existing_catalyst_signal(ticker, CatalystType.THEMATIC_TREND, conn=conn):
                continue

            sector_meta = next((s for s in accum_sectors if s["sector"] == sub_cat), None)
            if not sector_meta:
                continue

            # Price action check: ensure price has sufficient history
            df_prices = get_recent_prices(ticker, days=60, conn=conn)
            if df_prices.empty or len(df_prices) < 14:
                continue

            last_close = float(df_prices.iloc[-1]['close'])
            sma_20 = float(df_prices['close'].rolling(20, min_periods=5).mean().iloc[-1])

            # Must not be deeply broken below 20-day SMA
            if last_close < sma_20 * 0.95:
                continue

            # Calculate Home Run execution bounds (3x ATR Target, min +12% to +25%, 2.5:1 R/R)
            trigger_price, target_price, stop_loss = calculate_ta_execution_bounds(df_prices, is_home_run=True)

            upside_pct = ((target_price / trigger_price) - 1) * 100
            downside_pct = ((1 - (stop_loss / trigger_price))) * 100
            conviction = float(avg_conf) * float(avg_sent) * 1.15  # Boost for sector rotation tailwind
            conviction = min(1.0, max(0.50, conviction))

            etf_ticker = sector_meta["ticker"]
            sector_label = sector_meta["name"]
            rs_alpha = sector_meta["rs_vs_spy_5d_pct"]

            thesis = (
                f"[INSTITUTIONAL ROTATION RUNNER] {ticker}\n"
                f"Capital Inflow Tailwind: Sector {sector_label} ({etf_ticker}) is in {sector_meta['phase']} "
                f"with +{rs_alpha:.1f}% Relative Strength alpha vs SPY.\n"
                f"Recent News Impulses: {headlines[:150]}\n"
                f"Sentiment Momentum: +{float(avg_sent):.2f} (Confidence: {float(avg_conf):.0%})\n"
                f"Execution Strategy: Swing Long | Target ${target_price:.2f} (+{upside_pct:.1f}%) | "
                f"Stop ${stop_loss:.2f} (-{downside_pct:.1f}%) | Reward/Risk: {upside_pct/downside_pct:.2f}:1"
            )

            # DeepSeek reasoning enrichment
            try:
                db_thesis = build_thesis_from_reasonings(ticker, days=7, conn=conn)
                if db_thesis:
                    thesis += f"\n\nDeepSeek Catalyst Intelligence:\n{db_thesis}"
            except Exception:
                pass

            reason_str = (
                f"SECTOR_ROTATION_RUNNER: Inflowing sector {sector_label} ({etf_ticker} +{rs_alpha:.1f}% RS) | "
                f"Sentiment +{float(avg_sent):.2f}"
            )

            # [PERMANENTLY DEPRECATED FOR STANDALONE EMISSION]
            # Standalone sector runner alerts are decommissioned. Sector rotation tailwinds
            # are centralized as Cylinder 1 of the War Rig Transmission Engine (war_rig_engine.py).
            logger.info(
                f"[SECTOR_ROTATION] Sector runner identified: {ticker} in {sector_label} (+{rs_alpha:.1f}% RS). "
                f"Standalone emission decommissioned; will be captured through War Rig Transmission."
            )

    except Exception as e:
        logger.error(f"[SECTOR_ROTATION] Error scanning rotation runners: {e}")
    finally:
        cur.close()
        if close_conn:
            conn.close()

    return generated_signals
