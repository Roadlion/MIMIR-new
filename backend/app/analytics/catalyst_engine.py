# backend/app/analytics/catalyst_engine.py
import math
import logging
import pandas as pd
import numpy as np
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Any, Tuple

from ..database import get_db_connection
from ..config import get_settings
from .technical_analysis import analyze_technical_indicators
from .paper_trader import is_us_stock
from ..services.discord_notifier import send_trade_alert as _discord_alert


settings = get_settings()
logger = logging.getLogger(__name__)

class CatalystType:
    PRE_EARNINGS_BEAT = "PRE_EARNINGS_BEAT"
    SUPPLY_CHAIN_SPILLOVER = "SUPPLY_CHAIN_SPILLOVER"
    MICRO_CATALYST = "MICRO_CATALYST"
    THEMATIC_TREND = "THEMATIC_TREND"
    MACRO_CATALYST = "MACRO_CATALYST"

def calculate_ta_execution_bounds(df_prices: pd.DataFrame, is_home_run: bool = False) -> Tuple[float, float, float]:
    """
    Calculates exact execution parameters from price history:
    - trigger_price: Current close price
    - target_price: 2.5x ATR projection (or 3.0x ATR / min 12-25% for home runs)
    - stop_loss: 1.5x ATR downside risk
    - Enforces min 2.5:1 R/R for home runs and min 1.67:1 R/R for standard catalysts.
    """
    if df_prices.empty or len(df_prices) < 14:
        last_close = float(df_prices.iloc[-1]['close']) if not df_prices.empty else 100.0
        if is_home_run:
            return last_close, round(last_close * 1.15, 2), round(last_close * 0.95, 2)
        return last_close, round(last_close * 1.08, 2), round(last_close * 0.95, 2)

    last_close = float(df_prices.iloc[-1]['close'])
    
    # Calculate ATR (Average True Range over 14 bars)
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

    # Institutional ATR Execution Bounds
    if is_home_run:
        # Home Run Asymmetric Setup: 1.5x ATR SL (4.0% - 7.5%), 3.0x ATR TP (12.0% - 25.0%, min 2.5:1 R/R)
        sl_dist = max(0.040 * last_close, min(0.075 * last_close, 1.5 * atr))
        tp_dist = max(0.120 * last_close, min(0.250 * last_close, 3.0 * atr))
        tp_dist = max(tp_dist, sl_dist * 2.5)
    else:
        sl_dist = max(0.035 * last_close, min(0.075 * last_close, 1.5 * atr))
        tp_dist = max(0.060 * last_close, min(0.150 * last_close, 2.5 * atr))
        tp_dist = max(tp_dist, sl_dist * 1.67)

    stop_loss = last_close - sl_dist
    target_price = last_close + tp_dist

    return round(last_close, 2), round(target_price, 2), round(stop_loss, 2)


def check_existing_catalyst_signal(ticker: str, catalyst_type: str, conn=None) -> bool:
    """Checks if a PENDING catalyst signal of this type exists for ticker in last 24 hours."""
    close_conn = False
    if conn is None:
        conn = get_db_connection()
        close_conn = True
    cur = conn.cursor()
    try:
        cur.execute(f"""
            SELECT 1 FROM {settings.mimir_schema}.mimir_trade_signals
            WHERE ticker = %s AND catalyst_type = %s
              AND (status = 'PENDING' OR created_at >= NOW() - INTERVAL '24 hours')
            LIMIT 1
        """, (ticker, catalyst_type))
        return cur.fetchone() is not None
    except Exception:
        return False
    finally:
        cur.close()
        if close_conn:
            conn.close()


def insert_catalyst_trade_signal(
    ticker: str,
    signal_type: str,
    catalyst_type: str,
    trigger_price: float,
    target_price: float,
    stop_loss: float,
    holding_period: str,
    headline: str,
    investment_thesis: str,
    conviction_score: float,
    sentiment_score: float,
    reason: str,
    conn=None
) -> bool:
    """Inserts a structured Catalyst Trade Signal into mimir_trade_signals."""
    close_conn = False
    if conn is None:
        conn = get_db_connection()
        close_conn = True
    cur = conn.cursor()
    try:
        sql = f"""
            INSERT INTO {settings.mimir_schema}.mimir_trade_signals
            (ticker, signal_type, catalyst_type, trigger_price, target_price, stop_loss,
             holding_period, headline, investment_thesis, conviction_score, sentiment_score, reason, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'PENDING')
        """
        cur.execute(sql, (
            ticker, signal_type, catalyst_type, trigger_price, target_price, stop_loss,
            holding_period, headline, investment_thesis, conviction_score, sentiment_score, reason
        ))
        conn.commit()
        logger.info(f"[CATALYST_ENGINE] Created {catalyst_type} alert for {ticker} ({signal_type}): {headline[:60]}")

        # ── Discord notification ─────────────────────────────────────────────
        try:
            _discord_alert(
                ticker=ticker,
                signal_type=signal_type,
                trigger_price=trigger_price,
                target_price=target_price,
                stop_loss=stop_loss,
                sentiment_score=sentiment_score,
                catalyst_type=catalyst_type,
                headline=headline,
                reason=reason,
                holding_period=holding_period,
                conviction_score=conviction_score,
            )
        except Exception as discord_err:
            logger.warning(f"[CATALYST_ENGINE] Discord notify failed (non-fatal): {discord_err}")
        # ────────────────────────────────────────────────────────────────────

        return True
    except Exception as e:
        conn.rollback()
        logger.error(f"[CATALYST_ENGINE] Error inserting catalyst signal for {ticker}: {e}")
        return False
    finally:
        cur.close()
        if close_conn:
            conn.close()


def eval_realtime_article_catalyst(
    ticker: str,
    sentiment_score: float,
    confidence: float,
    headline: str,
    reasoning: str,
    policy_signal: Optional[str] = None,
    is_spillover: bool = False,
    spillover_source_asset: Optional[str] = None,
    df_prices: Optional[pd.DataFrame] = None,
    ignore_macro: bool = False,
    conn=None
) -> Optional[Dict[str, Any]]:
    """
    Evaluates a real-time DeepSeek scored article to generate an instant catalyst alert.
    Requires sentiment score |score| >= 0.50 and confidence >= 0.60.
    """
    ticker = ticker.strip().upper()
    if not is_us_stock(ticker):
        return None

    # High quality bar: strictly require |sentiment| >= 0.60 and confidence >= 0.65
    if abs(sentiment_score) < 0.60 or confidence < 0.65:
        return None

    signal_type = "BUY" if sentiment_score > 0 else "SELL"

    # Macro Regime Gate: reject BUY signals if broad market is bearish (SPY < 50-SMA or VIX > 25)
    if signal_type == "BUY" and not ignore_macro:
        try:
            from ..services.macro_tracker import is_market_regime_bullish
            bullish, reason = is_market_regime_bullish(conn=conn)
            if not bullish:
                logger.info(f"[CATALYST_ENGINE] {ticker} BUY alert blocked by macro gate: {reason}")
                return None
        except Exception:
            pass

    # Determine Catalyst Type
    headline_lower = headline.lower()
    if is_spillover:
        cat_type = CatalystType.SUPPLY_CHAIN_SPILLOVER
        cat_title = f"Supply Chain Spillover ({spillover_source_asset or 'Supplier'})"
    elif any(kw in headline_lower for kw in ["earnings", "eps", "revenue", "quarterly", "guidance", "fda", "patent", "contract", "merger", "acquisition"]):
        cat_type = CatalystType.MICRO_CATALYST
        cat_title = "Micro Catalyst & Corporate Event"
    elif any(kw in headline_lower for kw in ["fed", "tariff", "cpi", "inflation", "interest rate", "geopolitics", "war", "sanction"]):
        cat_type = CatalystType.MACRO_CATALYST
        cat_title = "Macro / Regulatory Catalyst"
    else:
        cat_type = CatalystType.THEMATIC_TREND
        cat_title = "Thematic Market Impulse"

    if check_existing_catalyst_signal(ticker, cat_type, conn=conn):
        return None

    # Check Sector Rotation Tailwinds & Outflows
    sector_info = {}
    try:
        from ..services.sector_rotation_service import get_ticker_sector_tailwinds
        sector_info = get_ticker_sector_tailwinds(ticker, conn=conn)
    except Exception:
        pass

    # Suppress BUY signals if sector is in heavy OUTFLOW / DISTRIBUTION (unless fresh major catalyst or conviction >= 0.85)
    fresh_catalyst = (is_spillover or cat_type in (CatalystType.PRE_EARNINGS_BEAT, CatalystType.SUPPLY_CHAIN_SPILLOVER) or (confidence * abs(sentiment_score)) >= 0.80)
    if signal_type == "BUY" and sector_info.get("is_outflow") and not fresh_catalyst:
        if (confidence * abs(sentiment_score)) < 0.85:
            logger.info(f"[CATALYST_ENGINE] Suppressed {ticker} BUY alert: parent sector in {sector_info.get('phase')} outflow.")
            return None

    # Fetch recent prices for TA execution math
    if df_prices is None:
        from .signal_fusion import get_recent_prices
        df_prices = get_recent_prices(ticker, days=60, conn=conn)

    is_home_run = (cat_type in (CatalystType.PRE_EARNINGS_BEAT, CatalystType.THEMATIC_TREND) or sector_info.get("has_tailwind", False))
    trigger_price, target_price, stop_loss = calculate_ta_execution_bounds(df_prices, is_home_run=is_home_run)

    holding_days = 10 if is_home_run else (5 if cat_type in [CatalystType.MICRO_CATALYST, CatalystType.SUPPLY_CHAIN_SPILLOVER] else 3)
    holding_period = f"Hold {holding_days} trading days ({'Home Run Swing' if is_home_run else 'Swing Horizon'})"

    # Build full investment thesis from accumulated DeepSeek reasonings in DB + Multi-day Narrative Context
    try:
        from .signal_fusion import build_thesis_from_reasonings
        db_thesis = build_thesis_from_reasonings(ticker, days=7, conn=conn)
    except Exception:
        db_thesis = ""

    nar_context = {}
    try:
        from .narrative_tracker import get_multi_day_narrative_context
        nar_context = get_multi_day_narrative_context(ticker, conn=conn)
    except Exception:
        pass

    nar_phase = nar_context.get("phase", "UNKNOWN")
    nar_theme = nar_context.get("theme", "Multi-Day Market Trend")
    nar_summary = nar_context.get("summary", "")

    # Multi-day Narrative conviction adjustment & distribution check
    if signal_type == "BUY" and nar_phase in ("PEAK", "FADING") and not fresh_catalyst:
        logger.info(f"[CATALYST_ENGINE] Suppressed {ticker} BUY alert: narrative in {nar_phase} phase.")
        return None

    conviction_score = confidence * abs(sentiment_score)
    if nar_phase in ("EMERGING", "BUILDING"):
        conviction_score = min(1.0, conviction_score * 1.15)
    elif nar_phase == "FADING" and not fresh_catalyst:
        conviction_score *= 0.75

    if sector_info.get("has_tailwind"):
        conviction_score = min(1.0, conviction_score * 1.15)

    # Institutional Quality Floor: strictly require conviction >= 0.75 (unless verified PRE_EARNINGS_BEAT)
    if conviction_score < 0.75 and cat_type != CatalystType.PRE_EARNINGS_BEAT:
        return None

    thesis = (
        f"[{cat_title}] {headline}\n"
        f"🌊 Multi-Day Narrative Alignment: [{nar_phase}] {nar_theme}\n"
        f"Multi-Day Narrative Context: {nar_summary}\n"
        f"DeepSeek Catalyst Analysis: {reasoning}\n"
        f"Conviction Score: {conviction_score*100:.0f}% | Policy Signal: {policy_signal or 'N/A'}\n"
        f"TA Execution Bounds: Limit Buy ${trigger_price:.2f} | Target ${target_price:.2f} "
        f"(+{((target_price/trigger_price)-1)*100:.1f}%) | Stop ${stop_loss:.2f} "
        f"(-{((1-(stop_loss/trigger_price))*100):.1f}%)"
    )
    if sector_info.get("has_tailwind"):
        thesis += f"\n🌊 Capital Inflow Tailwind: [{sector_info.get('phase')}] {sector_info.get('reason')}"
    if db_thesis:
        thesis += f"\n\nSupporting Catalyst Intelligence (from DeepSeek coverage):\n{db_thesis}"

    reason_summary = (
        f"{cat_title} ({nar_phase} Narrative): Sentiment {sentiment_score:+.2f} | "
        f"{headline[:70]} | Multi-Day: {nar_summary[:80]}"
    )

    success = insert_catalyst_trade_signal(
        ticker=ticker,
        signal_type=signal_type,
        catalyst_type=cat_type,
        trigger_price=trigger_price,
        target_price=target_price,
        stop_loss=stop_loss,
        holding_period=holding_period,
        headline=headline,
        investment_thesis=thesis,
        conviction_score=round(conviction_score, 3),
        sentiment_score=sentiment_score,
        reason=reason_summary,
        conn=conn
    )

    if success:
        return {
            "ticker": ticker,
            "signal_type": signal_type,
            "catalyst_type": cat_type,
            "trigger_price": trigger_price,
            "target_price": target_price,
            "stop_loss": stop_loss,
            "holding_period": holding_period,
            "headline": headline,
            "investment_thesis": thesis
        }
    return None


def scan_pre_earnings_catalysts(conn=None) -> List[Dict[str, Any]]:
    """
    Scans tracked tickers for upcoming earnings in 3 to 10 days.
    If positive DeepSeek sentiment / supplier spillovers exist in the past 7 days,
    generates a PRE_EARNINGS_BEAT trade signal!
    """
    close_conn = False
    if conn is None:
        conn = get_db_connection()
        close_conn = True
    cur = conn.cursor()

    generated_signals = []
    try:
        from ..routers.prices import DEFAULT_TICKERS
        from .signal_fusion import get_recent_prices

        # Query recent positive sentiment impacts from last 7 days grouped by ticker
        sql_sentiment = f"""
            SELECT si.ticker, AVG(si.sentiment_score) as avg_score, AVG(si.confidence) as avg_conf,
                   STRING_AGG(DISTINCT a.title, ' | ') as headlines
            FROM {settings.mimir_schema}.mimir_sentiment_impacts si
            JOIN {settings.mimir_schema}.mimir_raw_articles a ON si.article_id = a.id
            WHERE (a.published_ts AT TIME ZONE 'UTC')::date >= NOW()::date - INTERVAL '7 days'
              AND si.sentiment_score >= 0.35
              AND si.ticker IS NOT NULL
            GROUP BY si.ticker
        """
        cur.execute(sql_sentiment)
        rows = cur.fetchall()
        sent_map = {r[0].strip().upper(): {"score": float(r[1]), "conf": float(r[2]), "headlines": r[3]} for r in rows if r[0]}

        # Scan tickers for pre-earnings setup across the full active database universe:
        tickers = set(sent_map.keys()) | set([t.strip().upper() for t in DEFAULT_TICKERS if t])
        try:
            cur.execute(f"""
                SELECT DISTINCT ticker
                FROM {settings.mimir_schema}.mimir_earnings_calendar
                WHERE earnings_date >= CURRENT_DATE + INTERVAL '2 days'
                  AND earnings_date <= CURRENT_DATE + INTERVAL '12 days'
                  AND ticker IS NOT NULL
            """)
            for r in cur.fetchall():
                if r[0]:
                    tickers.add(r[0].strip().upper())
        except Exception as cal_fetch_err:
            logger.debug(f"[CATALYST] Could not pre-fetch earnings calendar tickers: {cal_fetch_err}")

        for ticker in tickers:
            if not is_us_stock(ticker):
                continue

            if ticker not in sent_map:
                continue

            sent_info = sent_map[ticker]
            avg_score = float(sent_info["score"])
            avg_conf = float(sent_info["conf"])
            headlines = sent_info["headlines"][:150]

            # ── REAL EARNINGS CALENDAR GATE ──────────────────────────────────
            # Only fire PRE_EARNINGS_BEAT if there is a confirmed upcoming earnings date in 3-10 days.
            try:
                from ..services.earnings_calendar import is_pre_earnings_window
                if not is_pre_earnings_window(ticker, min_days=3, max_days=10, conn=conn):
                    continue
            except Exception as cal_err:
                logger.warning(f"[CATALYST] Earnings calendar check failed for {ticker}: {cal_err}")
                continue

            # ── NARRATIVE & CORPORATE QUIET PERIOD GATE ───────────────────────
            # During the 2 weeks before earnings, companies enter an SEC quiet period
            # where PR and headlines naturally drop. We only skip if sentiment is weak (< 0.40).
            try:
                from .narrative_tracker import get_narrative_phase
                narr = get_narrative_phase(ticker, conn=conn)
                if narr.phase == "FADING" and avg_score < 0.40:
                    logger.info(
                        f"[CATALYST] {ticker}: PRE_EARNINGS_BEAT skipped — "
                        f"narrative is {narr.phase} and sentiment {avg_score:.2f} < 0.40."
                    )
                    continue
            except Exception:
                pass

            # ── SECTOR ROTATION CHECK ─────────────────────────────────────────
            sector_info = {}
            try:
                from ..services.sector_rotation_service import get_ticker_sector_tailwinds
                sector_info = get_ticker_sector_tailwinds(ticker, conn=conn)
            except Exception:
                pass

            # Sector tailwind or headwind adjustment
            sector_headwind = sector_info.get("phase") == "DISTRIBUTION"
            if sector_headwind and avg_score < 0.45:
                logger.info(f"[CATALYST] {ticker}: PRE_EARNINGS_BEAT skipped — parent sector {sector_info.get('sector_name')} in DISTRIBUTION and sentiment {avg_score:.2f} < 0.45.")
                continue

            if check_existing_catalyst_signal(ticker, CatalystType.PRE_EARNINGS_BEAT, conn=conn):
                continue

            # Fetch price data
            df_prices = get_recent_prices(ticker, days=60, conn=conn)
            if df_prices.empty:
                continue

            # Home Run Execution Bounds: 3x ATR target, 1.5x ATR stop, min 2.5:1 R/R
            trigger_price, target_price, stop_loss = calculate_ta_execution_bounds(df_prices, is_home_run=True)

            # Fetch earnings date for the thesis
            try:
                from ..services.earnings_calendar import get_upcoming_earnings
                upcoming = get_upcoming_earnings(days_ahead=12, conn=conn)
                ticker_event = next((e for e in upcoming if e["ticker"] == ticker), None)
                earnings_date_str = ticker_event["earnings_date"] if ticker_event else "upcoming"
                earnings_time_str = ticker_event.get("earnings_time", "TNS") if ticker_event else "TNS"
                days_until_str = f"{ticker_event['days_until']} days" if ticker_event else "soon"
            except Exception:
                earnings_date_str = "upcoming"
                earnings_time_str = "TNS"
                days_until_str = "soon"

            holding_period = f"Pre-Earnings Accumulation (Earnings: {earnings_date_str} {earnings_time_str} — {days_until_str})"
            upside_pct = ((target_price / trigger_price) - 1) * 100
            downside_pct = ((1 - (stop_loss / trigger_price))) * 100

            thesis = (
                f"[PRE-EARNINGS BEAT CATALYST] {ticker}\n"
                f"High-conviction pre-earnings accumulation setup based on bullish channel sentiment and supplier signals.\n"
                f"Upcoming Earnings: {earnings_date_str} ({earnings_time_str}) — {days_until_str}\n"
                f"Recent News Impulses: {headlines}\n"
                f"7-Day Sentiment Momentum: +{avg_score:.2f} (Confidence: {avg_conf:.0%})\n"
                f"TA Execution Bounds: Limit Buy ${trigger_price:.2f} | Take-Profit Target ${target_price:.2f} (+{upside_pct:.1f}%) | Stop ${stop_loss:.2f} (-{downside_pct:.1f}%)"
            )

            if sector_info.get("has_tailwind"):
                thesis += f"\n🌊 Capital Inflow Tailwind: [{sector_info.get('phase')}] {sector_info.get('reason')}"

            # Enrich with detailed DeepSeek reasoning narratives (free — data already in DB)
            try:
                from .signal_fusion import build_thesis_from_reasonings
                db_thesis = build_thesis_from_reasonings(ticker, days=7, conn=conn)
                if db_thesis:
                    thesis += f"\n\nDeepSeek Catalyst Intelligence:\n{db_thesis}"
            except Exception:
                pass

            conviction_score = avg_conf * avg_score
            if sector_info.get("has_tailwind"):
                conviction_score = min(1.0, conviction_score * 1.15)

            reason_str = (
                f"PRE_EARNINGS_BEAT: Pre-earnings sentiment score +{avg_score:.2f} | "
                f"Accumulation into {earnings_date_str} report."
            )

            success = insert_catalyst_trade_signal(
                ticker=ticker,
                signal_type="BUY",
                catalyst_type=CatalystType.PRE_EARNINGS_BEAT,
                trigger_price=trigger_price,
                target_price=target_price,
                stop_loss=stop_loss,
                holding_period=holding_period,
                headline=f"Pre-Earnings Beat Momentum: {ticker}",
                investment_thesis=thesis,
                conviction_score=avg_conf * avg_score,
                sentiment_score=avg_score,
                reason=reason_str,
                conn=conn
            )

            if success:
                generated_signals.append({
                    "ticker": ticker,
                    "signal_type": "BUY",
                    "catalyst_type": CatalystType.PRE_EARNINGS_BEAT,
                    "trigger_price": trigger_price,
                    "target_price": target_price,
                    "stop_loss": stop_loss,
                    "holding_period": holding_period,
                    "headline": f"Pre-Earnings Beat Momentum: {ticker}",
                    "investment_thesis": thesis
                })

    except Exception as e:
        logger.error(f"[CATALYST_ENGINE] Error scanning pre-earnings catalysts: {e}")
    finally:
        cur.close()
        if close_conn:
            conn.close()

    return generated_signals
