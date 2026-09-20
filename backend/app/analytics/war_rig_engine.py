# backend/app/analytics/war_rig_engine.py
"""
War Rig Transmission Engine (Unified Alpha Shaft)
=================================================
Unifies all alpha signals into a single crankshaft where Macro & Sector Inflows,
Catalysts (Pre-Earnings Beats + Supply Chain Spillovers), and Microstructure Asymmetry
work in tandem to drive a single composite conviction score (>= 75%).

Design Philosophy:
- No independent standalone engines firing disparate signals.
- All cylinders drive the SAME crankshaft.
- Cylinder 1: Macro & Sector Transmission (Max 25 pts)
- Cylinder 2: Catalyst V8 Twin-Turbo (Max 50 pts)
    - Turbo A: Pre-Earnings Beat Anticipation (0-30 pts)
    - Turbo B: Supply Chain Spillover & High-Impact Catalyst (0-30 pts)
    - Synergy: +15 pts bonus when both turbos fire together (capped at 50 pts)
- Cylinder 3: Microstructure & Asymmetry Gate (Max 25 pts + Hard 2.5:1 R/R Gate)
- Minimum Conviction Threshold: 75% for order emission.
- Output: Standardized 'WAR_RIG_CONVERGENCE' trade signal.
"""

import math
import logging
from datetime import datetime, timedelta, timezone, date
from typing import Dict, List, Optional, Any, Tuple
import pandas as pd
import numpy as np

from ..database import get_db_connection
from ..config import get_settings
from .technical_analysis import analyze_technical_indicators
from .paper_trader import is_us_stock
from ..services.macro_tracker import is_buy_blocked_by_macro
from ..services.sector_rotation_service import get_ticker_sector_tailwinds
from ..services.discord_notifier import send_trade_alert as _discord_alert

# Bong Strats Quantitative Indicators
try:
    from bong_strats.indicators import (
        compute_volatility_regime,
        compute_bollinger_features,
        compute_ou_features,
        OUCalibrator
    )
except ImportError:
    import sys
    from pathlib import Path
    root_dir = Path(__file__).resolve().parents[3]
    if str(root_dir) not in sys.path:
        sys.path.insert(0, str(root_dir))
    from bong_strats.indicators import (
        compute_volatility_regime,
        compute_bollinger_features,
        compute_ou_features,
        OUCalibrator
    )

settings = get_settings()
logger = logging.getLogger(__name__)

WAR_RIG_CONVICTION_THRESHOLD = 75.0
MINIMUM_RR_RATIO = 2.50


def get_ticker_historical_prices(
    ticker: str,
    as_of_date: Optional[date] = None,
    days: int = 90,
    conn = None
) -> pd.DataFrame:
    """
    Fetches daily OHLCV bars strictly <= as_of_date (Zero Look-Ahead Bias).
    Aggregates from v_mimir_daily_ohlcv.
    """
    close_conn = False
    if conn is None:
        conn = get_db_connection()
        close_conn = True
    cur = conn.cursor()

    end_d = as_of_date if as_of_date is not None else datetime.now(timezone.utc).date()
    start_d = end_d - timedelta(days=days)

    sql = f"""
        SELECT date, open, high, low, close, volume
        FROM {settings.mimir_schema}.v_mimir_daily_ohlcv
        WHERE ticker = %s 
          AND date >= %s 
          AND date <= %s
        ORDER BY date ASC
    """
    try:
        cur.execute(sql, (ticker.strip().upper(), start_d, end_d))
        rows = cur.fetchall()
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows, columns=['date', 'open', 'high', 'low', 'close', 'volume'])
        df['date'] = pd.to_datetime(df['date'])
        df.set_index('date', inplace=True)
        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = pd.to_numeric(df[col], errors='coerce')
        return df
    except Exception as e:
        logger.warning(f"[WAR_RIG] Error fetching prices for {ticker}: {e}")
        return pd.DataFrame()
    finally:
        cur.close()
        if close_conn:
            conn.close()


def calculate_war_rig_execution_bounds(
    df_prices: pd.DataFrame
) -> Tuple[float, float, float, float]:
    """
    Computes institutional asymmetric execution bounds:
    - Trigger: Last close
    - Stop Loss: 1.5x ATR downside (tight invalidation, 4.0% - 6.5%)
    - Target: 3.0x ATR upside (home run projection, 12.0% - 25.0%)
    - Guaranteed minimum R/R >= 2.5:1
    Returns: (trigger_price, target_price, stop_loss, risk_reward_ratio)
    """
    if df_prices.empty or len(df_prices) < 14:
        last_close = float(df_prices.iloc[-1]['close']) if not df_prices.empty else 100.0
        sl = round(last_close * 0.95, 2)
        tp = round(last_close * 1.15, 2)
        rr = (tp - last_close) / max(0.01, (last_close - sl))
        return last_close, tp, sl, rr

    last_close = float(df_prices.iloc[-1]['close'])

    high = df_prices['high']
    low = df_prices['low']
    close = df_prices['close']
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = float(tr.rolling(14).mean().iloc[-1]) if len(tr) >= 14 else last_close * 0.03
    if math.isnan(atr) or atr <= 0:
        atr = last_close * 0.03

    # Institutional Asymmetry: 1.5x ATR SL, 3.0x ATR TP
    sl_dist = max(0.038 * last_close, min(0.065 * last_close, 1.5 * atr))
    tp_dist = max(0.120 * last_close, min(0.250 * last_close, 3.0 * atr))
    tp_dist = max(tp_dist, sl_dist * MINIMUM_RR_RATIO)

    stop_loss = round(last_close - sl_dist, 2)
    target_price = round(last_close + tp_dist, 2)
    rr = round((target_price - last_close) / max(0.01, (last_close - stop_loss)), 2)

    return round(last_close, 2), target_price, stop_loss, rr


class WarRigEngine:
    """
    Unified Single-Shaft War Rig Alpha Transmission.
    Evaluates candidate tickers by converging:
    - Cylinder 1: Macro & Sector Inflows (25%)
    - Cylinder 2: Catalyst V8 Twin-Turbo (50%)
    - Cylinder 3: Microstructure Invalidation & Asymmetry Gate (25%)
    """

    def __init__(self, conn=None):
        self.conn = conn

    def evaluate_cylinder_1_macro_sector(
        self,
        ticker: str,
        as_of_date: Optional[date] = None,
        conn = None
    ) -> Tuple[float, Dict[str, Any]]:
        """
        Cylinder 1: Macro & Sector Inflow (Max 25 pts).
        - Macro check: Blocks trade or cuts points if Fed/CPI liquidity shock active.
        - Sector check: Rewards Stealth Accumulation (+25 pts) and Markup (+20 pts).
          Penalizes Distribution (0 pts) and Outflows (-5 pts).
        """
        db = conn or self.conn
        score = 0.0
        details = {}

        # 1. Sector Rotation Matrix Check
        try:
            sector_info = get_ticker_sector_tailwinds(ticker, conn=db)
            phase = sector_info.get("phase", "UNKNOWN")
            rs_5d = float(sector_info.get("rs_5d", 0.0))
            sector_name = sector_info.get("sector_name", "General Market")
            sector_etf = sector_info.get("sector_etf", "SPY")

            details["sector_name"] = sector_name
            details["sector_etf"] = sector_etf
            details["sector_phase"] = phase
            details["rs_vs_spy_5d"] = rs_5d

            if phase == "STEALTH_ACCUMULATION":
                score = 25.0
                details["sector_thesis"] = f"Leading Accumulation (+{rs_5d:.1f}% RS vs SPY). Institutions quietly building positions."
            elif phase == "MARKUP":
                score = 20.0
                details["sector_thesis"] = f"Strong Sector Momentum (+{rs_5d:.1f}% RS vs SPY). Full institutional trend established."
            elif phase in ("ACCUMULATION", "IMPROVING"):
                score = 15.0
                details["sector_thesis"] = f"Early Rotation / Improving (+{rs_5d:.1f}% RS vs SPY)."
            elif phase == "CONSOLIDATION" or rs_5d >= 0.0:
                score = 10.0
                details["sector_thesis"] = f"Neutral Sector Base (RS: {rs_5d:+.1f}% vs SPY)."
            elif phase == "DISTRIBUTION":
                score = 0.0
                details["sector_thesis"] = f"Institutional Distribution ({rs_5d:+.1f}% RS). Heavy supply overhead."
            else:
                score = 0.0
                details["sector_thesis"] = f"Sector Outflow / Markdown ({rs_5d:+.1f}% RS). Capital flowing into leaders."

            # Bonus for exceptional relative strength (> +2.0% vs SPY)
            if rs_5d >= 2.0 and score > 0:
                score = min(25.0, score + 3.0)

        except Exception as e:
            logger.warning(f"[WAR_RIG] Cylinder 1 error for {ticker}: {e}")
            score = 10.0
            details["sector_thesis"] = "Sector data unavailable (defaulted to neutral 10pts)."
            sector_name = "General Market"

        # 2. Macro Regime Check (e.g. rate-sensitive sectors under Fed hawkishness)
        try:
            macro_blocked, macro_reason = is_buy_blocked_by_macro(ticker_sector=sector_name, conn=db)
            if macro_blocked:
                details["macro_status"] = "BLOCKED"
                details["macro_reason"] = macro_reason
                logger.info(f"[WAR_RIG] {ticker} Cylinder 1: Macro blocked ({macro_reason})")
                return 0.0, details
            else:
                details["macro_status"] = "CLEAR"
                details["macro_reason"] = macro_reason
        except Exception as e:
            details["macro_status"] = "ERROR"
            details["macro_reason"] = str(e)

        return score, details

    def evaluate_cylinder_2_catalysts(
        self,
        ticker: str,
        as_of_date: Optional[date] = None,
        conn = None
    ) -> Tuple[float, Dict[str, Any]]:
        """
        Cylinder 2: Catalyst V8 Twin-Turbo (Max 50 pts).
        - Turbo A: Pre-Earnings Beat Anticipation (0 - 30 pts)
        - Turbo B: Supply Chain Spillover / High-Magnitude Catalyst (0 - 30 pts)
        - Synergy Bonus: +15 pts when both turbos converge (capped at 50 pts total).
        """
        db = conn or self.conn
        close_conn = False
        if db is None:
            db = get_db_connection()
            close_conn = True

        cur = db.cursor()
        turbo_a_score = 0.0
        turbo_b_score = 0.0
        details = {
            "turbo_a_active": False,
            "turbo_b_active": False,
            "pre_earnings": {},
            "spillover": {},
            "headlines": []
        }

        eval_date = as_of_date if as_of_date is not None else datetime.now(timezone.utc).date()

        try:
            # ── TURBO A: Pre-Earnings Beat Engine ────────────────────────────
            cur.execute(f"""
                SELECT earnings_date, earnings_time, company_name
                FROM {settings.mimir_schema}.mimir_earnings_calendar
                WHERE ticker = %s 
                  AND earnings_date >= %s + INTERVAL '2 days'
                  AND earnings_date <= %s + INTERVAL '14 days'
                ORDER BY earnings_date ASC
                LIMIT 1
            """, (ticker.strip().upper(), eval_date, eval_date))
            earn_row = cur.fetchone()

            if earn_row:
                earn_date, earn_time, comp_name = earn_row
                days_until = (earn_date - eval_date).days

                # Fetch 7-day pre-earnings sentiment leading into print
                cur.execute(f"""
                    SELECT AVG(si.sentiment_score), AVG(si.confidence), COUNT(*)
                    FROM {settings.mimir_schema}.mimir_sentiment_impacts si
                    JOIN {settings.mimir_schema}.mimir_raw_articles a ON si.article_id = a.id
                    WHERE si.ticker = %s
                      AND (a.published_ts AT TIME ZONE 'UTC')::date >= %s - INTERVAL '7 days'
                      AND (a.published_ts AT TIME ZONE 'UTC')::date <= %s
                """, (ticker.strip().upper(), eval_date, eval_date))
                sent_row = cur.fetchone()
                avg_sent = float(sent_row[0]) if sent_row and sent_row[0] is not None else 0.0
                avg_conf = float(sent_row[1]) if sent_row and sent_row[1] is not None else 0.50
                art_count = int(sent_row[2]) if sent_row and sent_row[2] is not None else 0

                # Fundamentals check (EPS growth)
                cur.execute(f"""
                    SELECT eps_growth, operating_margin, valuation_status
                    FROM {settings.mimir_schema}.mimir_asset_fundamentals
                    WHERE ticker = %s
                """, (ticker.strip().upper(),))
                fund_row = cur.fetchone()
                eps_growth = float(fund_row[0]) if fund_row and fund_row[0] is not None else None
                valuation = str(fund_row[2]) if fund_row and fund_row[2] is not None else "FAIRLY_VALUED"

                # Turbo A Score Calculation:
                # Base for entering the 3-10 day sweet spot
                if 3 <= days_until <= 10:
                    turbo_a_score = 15.0
                elif 2 <= days_until <= 14:
                    turbo_a_score = 10.0

                # Positive sentiment impulse into print
                if avg_sent >= 0.35:
                    turbo_a_score += min(10.0, avg_sent * 15.0)
                elif avg_sent > 0.0:
                    turbo_a_score += 4.0

                # Fundamental growth confirmation
                if eps_growth is not None and eps_growth > 0.10:
                    turbo_a_score += 5.0

                details["turbo_a_active"] = True
                details["pre_earnings"] = {
                    "earnings_date": earn_date.strftime('%Y-%m-%d'),
                    "earnings_time": earn_time or "TNS",
                    "days_until": days_until,
                    "avg_sentiment": round(avg_sent, 2),
                    "confidence": round(avg_conf, 2),
                    "eps_growth": eps_growth,
                    "valuation": valuation
                }

            # ── TURBO B: Supply Chain Spillover & High-Magnitude News ────────
            # 1. Check for supply chain spillover sentiment
            cur.execute(f"""
                SELECT si.spillover_source_asset, AVG(si.sentiment_score), AVG(si.confidence),
                       STRING_AGG(DISTINCT a.title, ' | ')
                FROM {settings.mimir_schema}.mimir_sentiment_impacts si
                JOIN {settings.mimir_schema}.mimir_raw_articles a ON si.article_id = a.id
                WHERE si.ticker = %s
                  AND si.is_spillover = TRUE
                  AND (a.published_ts AT TIME ZONE 'UTC')::date >= %s - INTERVAL '7 days'
                  AND (a.published_ts AT TIME ZONE 'UTC')::date <= %s
                GROUP BY si.spillover_source_asset
                ORDER BY AVG(si.sentiment_score) DESC
                LIMIT 1
            """, (ticker.strip().upper(), eval_date, eval_date))
            spill_row = cur.fetchone()

            if spill_row and spill_row[1] is not None and float(spill_row[1]) >= 0.12:
                source_asset, spill_score, spill_conf, spill_titles = spill_row
                turbo_b_score = min(28.0, 16.0 + float(spill_score) * 20.0)
                details["turbo_b_active"] = True
                details["spillover"] = {
                    "source_asset": source_asset,
                    "spillover_score": round(float(spill_score), 2),
                    "spillover_confidence": round(float(spill_conf), 2),
                    "headlines": spill_titles[:150] if spill_titles else ""
                }

            # 2. Check for Direct High-Magnitude Catalyst Articles (Tier 1 earnings/contract/guidance)
            cur.execute(f"""
                SELECT a.title, si.sentiment_score, si.confidence, si.magnitude, si.reasoning
                FROM {settings.mimir_schema}.mimir_sentiment_impacts si
                JOIN {settings.mimir_schema}.mimir_raw_articles a ON si.article_id = a.id
                WHERE si.ticker = %s
                  AND si.magnitude IN ('HIGH', 'MEDIUM')
                  AND si.sentiment_score >= 0.35
                  AND (a.published_ts AT TIME ZONE 'UTC')::date >= %s - INTERVAL '5 days'
                  AND (a.published_ts AT TIME ZONE 'UTC')::date <= %s
                ORDER BY si.confidence DESC, si.sentiment_score DESC
                LIMIT 3
            """, (ticker.strip().upper(), eval_date, eval_date))
            mag_rows = cur.fetchall()

            if mag_rows:
                high_mag_boost = 0.0
                for row in mag_rows:
                    title, score, conf, mag, reasoning = row
                    details["headlines"].append(title[:120])
                    mag_weight = 14.0 if mag == 'HIGH' else 8.0
                    art_score = float(score) * 16.0 + mag_weight * float(conf)
                    high_mag_boost = max(high_mag_boost, art_score)

                # Combine or upgrade turbo_b_score
                turbo_b_score = max(turbo_b_score, min(30.0, high_mag_boost))
                details["turbo_b_active"] = True

            # ── TWIN-TURBO SUPERCHARGING SYNERGY ─────────────────────────────
            # When Pre-Earnings Beat runs with Upstream Supply Chain / High-Mag News:
            synergy_bonus = 0.0
            if details["turbo_a_active"] and details["turbo_b_active"]:
                synergy_bonus = 15.0
                details["twin_turbo_synergy"] = True
            else:
                details["twin_turbo_synergy"] = False

            total_cylinder_2 = min(50.0, turbo_a_score + turbo_b_score + synergy_bonus)
            details["turbo_a_score"] = round(turbo_a_score, 1)
            details["turbo_b_score"] = round(turbo_b_score, 1)
            details["synergy_bonus"] = round(synergy_bonus, 1)

            return total_cylinder_2, details

        except Exception as e:
            logger.error(f"[WAR_RIG] Cylinder 2 error for {ticker}: {e}")
            return 0.0, details
        finally:
            cur.close()
            if close_conn:
                db.close()

    def evaluate_cylinder_3_microstructure(
        self,
        ticker: str,
        df_prices: pd.DataFrame
    ) -> Tuple[float, bool, Dict[str, Any]]:
        """
        Cylinder 3: Microstructure & Hard Asymmetry Gate (Max 28 pts + Gatekeeper).
        - Price Trend: Above 20-day SMA (+10 pts), Above 50-day SMA (+5 pts)
        - RSI Sweet Spot: 42 <= RSI <= 65 (+8 pts), 35 <= RSI < 42 (+5 pts)
        - Volume Expansion: Volume ratio >= 1.2x (+7 pts), >= 1.0x (+4 pts)
        - Bong Strats Quant Features:
            - Volatility Regime: SQUEEZE (+4 pts coiling), EXHAUSTION (-8 pts blow-off penalty)
            - Wyckoff Institutional Absorption: +5 pts (Heavy sell volume absorbed by buyers)
            - Ornstein-Uhlenbeck SDE Mean-Reversion:
                - Prime Discount Pocket (-1.8 <= Z <= -0.5): +4 pts
                - Overextended Risk (Z > 2.2): -8 pts penalty
        - Asymmetry Gate: Target/Stop R/R >= 2.5:1 and upside >= 10.0%. If not met, REJECT immediately!
        """
        score = 0.0
        details = {}

        if df_prices.empty or len(df_prices) < 20:
            return 0.0, False, {"error": "Insufficient price bars (<20 days)"}

        current_price = float(df_prices.iloc[-1]['close'])
        sma20 = float(df_prices['close'].rolling(20).mean().iloc[-1])
        sma50 = float(df_prices['close'].rolling(50).mean().iloc[-1]) if len(df_prices) >= 50 else sma20

        # Technical Indicators
        techs = analyze_technical_indicators(df_prices)
        rsi = float(techs.get("rsi", 50.0))
        vol_ratio = float(techs.get("volume_ratio", 1.0))

        # 1. Base Trend Quality (Up to 10 pts)
        if current_price >= sma20:
            score += 10.0
            trend_note = "Bullish structure (Above 20-day SMA)"
        elif current_price >= sma50:
            score += 5.0
            trend_note = "Consolidating on 50-day SMA base"
        else:
            trend_note = "Below key moving averages (downward drift)"

        # 2. RSI Health (Up to 8 pts)
        if 42.0 <= rsi <= 65.0:
            score += 8.0
            rsi_note = f"Optimal momentum launchpad (RSI: {rsi:.1f})"
        elif 35.0 <= rsi < 42.0:
            score += 5.0
            rsi_note = f"Oversold stabilization (RSI: {rsi:.1f})"
        elif 65.0 < rsi <= 72.0:
            score += 4.0
            rsi_note = f"Active momentum but elevated (RSI: {rsi:.1f})"
        else:
            rsi_note = f"Suboptimal RSI ({rsi:.1f}) — overextended or weak"

        # 3. Volume Expansion (Up to 7 pts)
        if vol_ratio >= 1.20:
            score += 7.0
            vol_note = f"Institutional volume expansion ({vol_ratio:.2f}x 20D avg)"
        elif vol_ratio >= 1.00:
            score += 4.0
            vol_note = f"Normal volume turnover ({vol_ratio:.2f}x 20D avg)"
        else:
            vol_note = f"Light volume ({vol_ratio:.2f}x 20D avg)"

        # 4. Bong Strats: Volatility Regime (+4 Squeeze / -8 Exhaustion)
        try:
            vol_regimes = compute_volatility_regime(df_prices)
            current_regime = vol_regimes.iloc[-1] if not vol_regimes.empty else "STABLE"
        except Exception:
            current_regime = "STABLE"

        if current_regime == "SQUEEZE":
            score += 4.0
            regime_note = "Bollinger Squeeze (Volatility Coiling)"
        elif current_regime == "EXHAUSTION":
            score -= 8.0
            regime_note = "Volatility Exhaustion (Blow-off Risk)"
        elif current_regime == "EXPANDING":
            score += 2.0
            regime_note = "Volatility Expanding"
        else:
            regime_note = "Stable Volatility"

        # 5. Bong Strats: Wyckoff Seller Absorption (+5 pts)
        try:
            if len(df_prices) >= 22:
                prior_vol = df_prices["volume"].shift(1).rolling(20, min_periods=10).mean()
                prev_bar = df_prices.iloc[-2]
                curr_bar = df_prices.iloc[-1]
                prev_avg = prior_vol.iloc[-2]

                is_vol_surge = (pd.notna(prev_avg) and prev_avg > 0 and float(prev_bar["volume"]) >= prev_avg * 1.5)
                bearish_surge = is_vol_surge and (float(prev_bar["close"]) < float(prev_bar["open"]))
                seller_exhausted = bearish_surge and (float(curr_bar["low"]) >= float(prev_bar["low"])) and (float(curr_bar["close"]) > float(curr_bar["open"]))
            else:
                seller_exhausted = False
        except Exception:
            seller_exhausted = False

        if seller_exhausted:
            score += 5.0
            wyckoff_note = "Wyckoff Seller Absorption Confirmed"
        else:
            wyckoff_note = "Normal Volume Distribution"

        # 6. Bong Strats: Ornstein-Uhlenbeck Mean-Reversion Feature (+4 Discount / -8 Overextended)
        try:
            ou_df = compute_ou_features(df_prices.copy(), lookback=min(40, len(df_prices) - 1))
            ou_z = float(ou_df["ou_zscore"].iloc[-1]) if pd.notna(ou_df["ou_zscore"].iloc[-1]) else 0.0
        except Exception:
            ou_z = 0.0

        if -1.8 <= ou_z <= -0.5:
            score += 4.0
            ou_note = f"OU Mean-Reversion Pocket (Z={ou_z:.2f})"
        elif ou_z > 2.2:
            score -= 8.0
            ou_note = f"OU Overextended (Z={ou_z:.2f})"
        else:
            ou_note = f"OU Neutral (Z={ou_z:.2f})"

        # Cap score between 0.0 and 28.0 (headroom for exceptional quantitative confluence)
        final_score = max(0.0, min(28.0, score))

        # Asymmetry Bounds Calculation
        trigger_price, target_price, stop_loss, rr_ratio = calculate_war_rig_execution_bounds(df_prices)
        upside_pct = round(((target_price / trigger_price) - 1.0) * 100.0, 1)
        downside_pct = round((1.0 - (stop_loss / trigger_price)) * 100.0, 1)

        # HARD ASYMMETRY GATEKEEPER
        is_asymmetric = (rr_ratio >= MINIMUM_RR_RATIO) and (upside_pct >= 10.0)

        details = {
            "current_price": trigger_price,
            "target_price": target_price,
            "stop_loss": stop_loss,
            "rr_ratio": rr_ratio,
            "upside_pct": upside_pct,
            "downside_pct": downside_pct,
            "rsi": round(rsi, 1),
            "vol_ratio": round(vol_ratio, 2),
            "sma20": round(sma20, 2),
            "sma50": round(sma50, 2),
            "trend_note": trend_note,
            "rsi_note": rsi_note,
            "vol_note": vol_note,
            "vol_regime": current_regime,
            "regime_note": regime_note,
            "seller_exhausted": seller_exhausted,
            "wyckoff_note": wyckoff_note,
            "ou_zscore": round(ou_z, 2),
            "ou_note": ou_note,
            "passes_asymmetry_gate": is_asymmetric
        }

        return final_score, is_asymmetric, details

    def evaluate_war_rig_candidate(
        self,
        ticker: str,
        as_of_date: Optional[date] = None,
        conn = None
    ) -> Optional[Dict[str, Any]]:
        """
        The Central Crankshaft Transmission:
        Evaluates ticker through all 3 cylinders in lockstep.
        Requires:
        1. Asymmetry Gate == True (R/R >= 2.5:1)
        2. Composite Conviction >= 75.0%
        Returns validated signal dictionary or None.
        """
        db = conn or self.conn
        if not is_us_stock(ticker):
            return None

        # Fetch historical daily prices strictly <= as_of_date
        df_prices = get_ticker_historical_prices(ticker, as_of_date=as_of_date, days=90, conn=db)
        if df_prices.empty or len(df_prices) < 20:
            return None

        # Cylinder 1: Macro & Sector Inflow (Max 25 pts)
        c1_score, c1_details = self.evaluate_cylinder_1_macro_sector(ticker, as_of_date=as_of_date, conn=db)
        if c1_details.get("macro_status") == "BLOCKED":
            return None

        # Cylinder 2: Catalyst V8 Twin-Turbo (Max 50 pts)
        c2_score, c2_details = self.evaluate_cylinder_2_catalysts(ticker, as_of_date=as_of_date, conn=db)
        # Must have at least one catalyst turbo firing
        if not c2_details.get("turbo_a_active") and not c2_details.get("turbo_b_active"):
            return None

        # Cylinder 3: Microstructure & Asymmetry Gate (Max 28 pts)
        c3_score, passes_asymmetry, c3_details = self.evaluate_cylinder_3_microstructure(ticker, df_prices)
        if not passes_asymmetry:
            return None

        # Crankshaft Convergence
        composite_conviction = c1_score + c2_score + c3_score

        if composite_conviction < WAR_RIG_CONVICTION_THRESHOLD:
            return None

        # Build Institutional Investment Thesis
        c_date = as_of_date.strftime('%Y-%m-%d') if as_of_date else datetime.now(timezone.utc).strftime('%Y-%m-%d')
        trigger_p = c3_details["current_price"]
        target_p = c3_details["target_price"]
        stop_p = c3_details["stop_loss"]
        rr = c3_details["rr_ratio"]

        catalyst_desc = []
        if c2_details.get("turbo_a_active"):
            pe = c2_details["pre_earnings"]
            catalyst_desc.append(f"Pre-Earnings Beat Momentum (Reporting {pe['earnings_date']} — {pe['days_until']}d out, Sentiment: +{pe['avg_sentiment']:.2f})")
        if c2_details.get("turbo_b_active"):
            if c2_details.get("spillover"):
                sp = c2_details["spillover"]
                catalyst_desc.append(f"Supply Chain Spillover (+{sp['spillover_score']:.2f} from {sp['source_asset']})")
            elif c2_details.get("headlines"):
                catalyst_desc.append(f"High-Impact Catalyst: {c2_details['headlines'][0][:80]}")

        micro_notes = [c3_details['trend_note'], c3_details['rsi_note'], c3_details['vol_note']]
        if c3_details.get('vol_regime') in ('SQUEEZE', 'EXHAUSTION'):
            micro_notes.append(c3_details['regime_note'])
        if c3_details.get('seller_exhausted'):
            micro_notes.append(c3_details['wyckoff_note'])
        if abs(c3_details.get('ou_zscore', 0.0)) >= 0.5:
            micro_notes.append(c3_details['ou_note'])

        thesis = (
            f"[WAR RIG ALPHA TRANSMISSION] {ticker} — Composite Conviction: {composite_conviction:.1f}%\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⚡ CYLINDER 1 (Macro & Sector): [{c1_details.get('sector_phase', 'NEUTRAL')}] {c1_details.get('sector_thesis', '')}\n"
            f"🔥 CYLINDER 2 (Catalyst V8): {' & '.join(catalyst_desc)}\n"
            f"🎯 CYLINDER 3 (Microstructure): {' | '.join(micro_notes)}\n"
            f"⚖️ ASYMMETRY GATE: Entry ${trigger_p:.2f} | Target ${target_p:.2f} (+{c3_details['upside_pct']}%) | Stop ${stop_p:.2f} (-{c3_details['downside_pct']}%) | R/R: {rr:.2f}:1\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        )

        holding_period = "Pre-Earnings / Catalyst Run (5-14 Days Swing)"
        if c2_details.get("turbo_a_active"):
            holding_period = f"Pre-Earnings Window (Hold into {c2_details['pre_earnings']['earnings_date']} report)"

        headline = f"War Rig Convergence: {ticker} (Conviction: {composite_conviction:.0f}%, R/R: {rr:.1f}:1)"
        reason = f"WAR_RIG_CONVERGENCE: Sector [{c1_details.get('sector_phase')}] + Catalyst V8 + {rr:.1f}:1 Asymmetry"

        # Nitrous Bridge Payload Hook
        nitrous_input = {
            "ticker": ticker,
            "trigger_price": trigger_p,
            "target_price": target_p,
            "stop_loss": stop_p,
            "conviction_score": round(composite_conviction / 100.0, 2),
            "risk_reward_ratio": rr,
            "holding_period": holding_period,
            "catalyst_type": "WAR_RIG_CONVERGENCE",
            "earnings_date": c2_details.get("pre_earnings", {}).get("earnings_date"),
            "days_until_earnings": c2_details.get("pre_earnings", {}).get("days_until"),
            "investment_thesis": thesis,
            "evaluation_date": c_date
        }

        return {
            "ticker": ticker,
            "signal_type": "BUY",
            "catalyst_type": "WAR_RIG_CONVERGENCE",
            "trigger_price": trigger_p,
            "target_price": target_p,
            "stop_loss": stop_p,
            "conviction_score": round(composite_conviction / 100.0, 2),
            "raw_conviction_pct": composite_conviction,
            "risk_reward_ratio": rr,
            "holding_period": holding_period,
            "headline": headline,
            "reason": reason,
            "investment_thesis": thesis,
            "cylinder_1_score": c1_score,
            "cylinder_2_score": c2_score,
            "cylinder_3_score": c3_score,
            "evaluation_date": c_date,
            "nitrous_payload": nitrous_input
        }


def get_war_rig_nitrous_options(signal: Dict[str, Any]) -> Dict[str, Any]:
    """
    Passes a War Rig signal to the Nitrous Express Bridge
    to calculate Mode A and Mode B options configurations.
    """
    from .war_rig_nitrous import get_nitrous_bridge
    bridge = get_nitrous_bridge()
    return bridge.generate_nitrous_deployment(signal)


def run_war_rig_scan(conn=None, top_n: int = 10) -> List[Dict[str, Any]]:
    """
    Scans the MIMIR universe through the War Rig single-shaft transmission.
    Inserts newly generated WAR_RIG_CONVERGENCE signals into mimir_trade_signals.
    """
    close_conn = False
    if conn is None:
        conn = get_db_connection()
        close_conn = True

    cur = conn.cursor()
    war_rig = WarRigEngine(conn=conn)
    generated_signals = []

    try:
        from ..routers.prices import DEFAULT_TICKERS

        # Universe assembly: active tickers with upcoming earnings, recent catalysts, or top liquidity
        candidate_tickers = set([t.strip().upper() for t in DEFAULT_TICKERS if t])

        # Add upcoming earnings tickers in the next 14 days
        cur.execute(f"""
            SELECT DISTINCT ticker
            FROM {settings.mimir_schema}.mimir_earnings_calendar
            WHERE earnings_date >= CURRENT_DATE + INTERVAL '2 days'
              AND earnings_date <= CURRENT_DATE + INTERVAL '14 days'
              AND ticker IS NOT NULL
        """)
        for r in cur.fetchall():
            if r[0]:
                candidate_tickers.add(r[0].strip().upper())

        # Add recent positive catalyst tickers (last 7 days)
        cur.execute(f"""
            SELECT DISTINCT si.ticker
            FROM {settings.mimir_schema}.mimir_sentiment_impacts si
            JOIN {settings.mimir_schema}.mimir_raw_articles a ON si.article_id = a.id
            WHERE (a.published_ts AT TIME ZONE 'UTC')::date >= CURRENT_DATE - INTERVAL '7 days'
              AND si.sentiment_score >= 0.35
              AND si.ticker IS NOT NULL
        """)
        for r in cur.fetchall():
            if r[0]:
                candidate_tickers.add(r[0].strip().upper())

        logger.info(f"[WAR_RIG] Scanning universe of {len(candidate_tickers)} candidates through the unified crankshaft...")

        for ticker in candidate_tickers:
            # Check duplicate signals in last 24 hours
            cur.execute(f"""
                SELECT 1 FROM {settings.mimir_schema}.mimir_trade_signals
                WHERE ticker = %s 
                  AND catalyst_type = 'WAR_RIG_CONVERGENCE'
                  AND (status = 'PENDING' OR created_at >= NOW() - INTERVAL '24 hours')
                LIMIT 1
            """, (ticker,))
            if cur.fetchone():
                continue

            sig = war_rig.evaluate_war_rig_candidate(ticker, conn=conn)
            if sig:
                # Insert into database
                cur.execute(f"""
                    INSERT INTO {settings.mimir_schema}.mimir_trade_signals
                    (ticker, signal_type, catalyst_type, trigger_price, target_price, stop_loss,
                     holding_period, headline, investment_thesis, conviction_score, sentiment_score, reason, status, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'PENDING', NOW())
                """, (
                    sig["ticker"], sig["signal_type"], sig["catalyst_type"],
                    sig["trigger_price"], sig["target_price"], sig["stop_loss"],
                    sig["holding_period"], sig["headline"], sig["investment_thesis"],
                    sig["conviction_score"], sig["cylinder_2_score"] / 50.0, sig["reason"]
                ))
                conn.commit()

                # Dispatch Discord Notification
                try:
                    _discord_alert(
                        ticker=sig["ticker"],
                        signal_type=sig["signal_type"],
                        trigger_price=sig["trigger_price"],
                        target_price=sig["target_price"],
                        stop_loss=sig["stop_loss"],
                        catalyst_type=sig["catalyst_type"],
                        headline=sig["headline"],
                        reason=sig["reason"],
                        holding_period=sig["holding_period"],
                        conviction_score=sig["conviction_score"],
                    )
                except Exception as d_err:
                    logger.warning(f"[WAR_RIG] Discord notify warning: {d_err}")

                generated_signals.append(sig)
                logger.info(f"[WAR_RIG] Generated War Rig Signal: {sig['ticker']} ({sig['raw_conviction_pct']:.0f}% Conviction | {sig['risk_reward_ratio']:.1f}:1 R/R)")
                if len(generated_signals) >= top_n:
                    break

        return generated_signals

    except Exception as e:
        conn.rollback()
        logger.error(f"[WAR_RIG] Error during war rig scan: {e}")
        return generated_signals
    finally:
        cur.close()
        if close_conn:
            conn.close()
