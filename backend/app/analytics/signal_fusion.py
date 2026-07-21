# backend/app/analytics/signal_fusion.py
import numpy as np
import pandas as pd
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional

from ..database import get_db_connection
from ..config import get_settings
from .technical_analysis import analyze_technical_indicators
from ..routers.prices import DEFAULT_TICKERS

settings = get_settings()

def get_recent_prices(ticker: str, days: int = 120, conn=None) -> pd.DataFrame:
    """Fetches recent daily prices for a ticker from the database."""
    close_conn = False
    if conn is None:
        conn = get_db_connection()
        close_conn = True
    cur = conn.cursor()
    
    start_date = (datetime.now() - timedelta(days=days)).date()
    
    sql = f"""
        SELECT date, open, high, low, close, volume
        FROM {settings.mimir_schema}.v_mimir_daily_ohlcv
        WHERE ticker = %s AND date >= %s
        ORDER BY date ASC
    """
    try:
        cur.execute(sql, (ticker.strip().upper(), start_date))
        rows = cur.fetchall()
        if not rows:
            return pd.DataFrame()
            
        df = pd.DataFrame(rows, columns=['date', 'open', 'high', 'low', 'close', 'volume'])
        df['date'] = pd.to_datetime(df['date'])
        df.set_index('date', inplace=True)
        # Convert numeric columns
        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = pd.to_numeric(df[col], errors='coerce')
        return df
    except Exception as e:
        print(f"[SIGNAL_FUSION] Error fetching prices for {ticker}: {e}")
        return pd.DataFrame()
    finally:
        cur.close()
        if close_conn:
            conn.close()

def get_recent_sentiment(ticker: str, days: int = 5, conn=None) -> Optional[float]:
    """Fetches the average sentiment score for a ticker over the last N days."""
    close_conn = False
    if conn is None:
        conn = get_db_connection()
        close_conn = True
    cur = conn.cursor()
    
    start_date = (datetime.now() - timedelta(days=days)).date()
    
    sql = f"""
        SELECT AVG(si.sentiment_score)
        FROM {settings.mimir_schema}.mimir_sentiment_impacts si
        JOIN {settings.mimir_schema}.mimir_raw_articles a ON si.article_id = a.id
        WHERE si.ticker = %s AND (a.published_ts AT TIME ZONE 'UTC')::date >= %s
    """
    try:
        cur.execute(sql, (ticker.strip().upper(), start_date))
        val = cur.fetchone()[0]
        return float(val) if val is not None else None
    except Exception as e:
        print(f"[SIGNAL_FUSION] Error fetching sentiment for {ticker}: {e}")
        return None
    finally:
        cur.close()
        if close_conn:
            conn.close()

def check_duplicate_signal(ticker: str, signal_type: str, conn=None) -> bool:
    """Checks if there's already a PENDING signal of the same type for this ticker."""
    close_conn = False
    if conn is None:
        conn = get_db_connection()
        close_conn = True
    cur = conn.cursor()
    
    sql = f"""
        SELECT 1 FROM {settings.mimir_schema}.mimir_trade_signals
        WHERE ticker = %s AND signal_type = %s AND status = 'PENDING'
        LIMIT 1
    """
    try:
        cur.execute(sql, (ticker, signal_type))
        return cur.fetchone() is not None
    except Exception:
        return False
    finally:
        cur.close()
        if close_conn:
            conn.close()

def insert_trade_signal(ticker: str, signal_type: str, price: float, rsi: float, sentiment: float, support: float, resistance: float, reason: str, conn=None) -> bool:
    """Inserts a new trade signal into the database."""
    close_conn = False
    if conn is None:
        conn = get_db_connection()
        close_conn = True
    cur = conn.cursor()
    
    sql = f"""
        INSERT INTO {settings.mimir_schema}.mimir_trade_signals 
        (ticker, signal_type, trigger_price, rsi_value, sentiment_score, support_level, resistance_level, reason, status)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'PENDING')
    """
    try:
        cur.execute(sql, (ticker, signal_type, price, rsi, sentiment, support, resistance, reason))
        conn.commit()
        return True
    except Exception as e:
        print(f"[SIGNAL_FUSION] Error inserting signal: {e}")
        return False
    finally:
        cur.close()
        if close_conn:
            conn.close()

def get_cached_fundamentals(ticker: str, conn=None) -> Optional[Dict[str, float]]:
    close_conn = False
    if conn is None:
        conn = get_db_connection()
        close_conn = True
    cur = conn.cursor()
    try:
        cur.execute(f"""
            SELECT pe_ratio, debt_to_equity, eps_growth, operating_margin 
            FROM {settings.mimir_schema}.mimir_asset_fundamentals 
            WHERE ticker = %s
        """, (ticker,))
        row = cur.fetchone()
        if row:
            return {
                "pe_ratio": float(row[0]) if row[0] is not None else None,
                "debt_to_equity": float(row[1]) if row[1] is not None else None,
                "eps_growth": float(row[2]) if row[2] is not None else None,
                "operating_margin": float(row[3]) if row[3] is not None else None
            }
        return None
    except Exception:
        return None
    finally:
        cur.close()
        if close_conn:
            conn.close()

def get_ticker_parameters(ticker: str, conn=None) -> Optional[Dict[str, Any]]:
    """Fetches custom optimal parameters for a ticker, if tuned."""
    close_conn = False
    if conn is None:
        conn = get_db_connection()
        close_conn = True
    cur = conn.cursor()
    try:
        cur.execute(f"""
            SELECT optimal_rsi_buy, optimal_rsi_sell, optimal_sentiment, optimal_vol_ratio, optimal_hold_days, 
                   win_rate, avg_pnl, optimal_prob_buy, optimal_prob_sell, selected_features_buy, selected_features_sell
            FROM {settings.mimir_schema}.mimir_ticker_parameters
            WHERE ticker = %s
        """, (ticker,))
        row = cur.fetchone()
        if row:
            import json
            return {
                "rsi_buy": float(row[0]),
                "rsi_sell": float(row[1]),
                "sentiment": float(row[2]),
                "vol_ratio": float(row[3]),
                "hold_days": int(row[4]),
                "win_rate": float(row[5]) if row[5] is not None else None,
                "avg_pnl": float(row[6]) if row[6] is not None else None,
                "prob_buy": float(row[7]) if row[7] is not None else 0.55,
                "prob_sell": float(row[8]) if row[8] is not None else 0.55,
                "selected_features_buy": json.loads(row[9]) if row[9] else None,
                "selected_features_sell": json.loads(row[10]) if row[10] else None
            }
        return None
    except Exception:
        return None
    finally:
        cur.close()
        if close_conn:
            conn.close()

def get_ticker_live_feedback(ticker: str, conn=None) -> Optional[float]:
    """Calculates the success rate (%) of the last 5 evaluated signals for a ticker."""
    close_conn = False
    if conn is None:
        conn = get_db_connection()
        close_conn = True
    cur = conn.cursor()
    try:
        cur.execute(f"""
            SELECT evaluation_status 
            FROM {settings.mimir_schema}.mimir_trade_signals
            WHERE ticker = %s AND evaluation_status IS NOT NULL
            ORDER BY created_at DESC
            LIMIT 5
        """, (ticker,))
        rows = cur.fetchall()
        if not rows:
            return None
        successes = sum(1 for row in rows if row[0] == 'SUCCESSFUL')
        return (successes / len(rows)) * 100.0
    except Exception:
        return None
    finally:
        cur.close()
        if close_conn:
            conn.close()

def get_daily_sentiment_history(ticker: str, days: int = 20, conn=None) -> pd.DataFrame:
    """Fetches a DataFrame of daily average sentiment scores for a single ticker,
    timezone-aligned to prevent lookahead leakage.
    """
    close_conn = False
    if conn is None:
        conn = get_db_connection()
        close_conn = True
    cur = conn.cursor()
    
    start_date = (datetime.now() - timedelta(days=days)).date()
    
    sql = f"""
        WITH adjusted_sentiment AS (
            SELECT 
                   CASE 
                       -- Crypto (closes at 00:00 UTC)
                       WHEN si.ticker LIKE '%%-USD' THEN 
                           (a.published_ts AT TIME ZONE 'UTC')::date
                       
                       -- Forex / Commodity (settles at 17:00 NY)
                       WHEN si.ticker LIKE '%%=X' OR si.ticker LIKE '%%=F' THEN
                           CASE 
                               WHEN EXTRACT(HOUR FROM (a.published_ts AT TIME ZONE 'America/New_York')) * 60 + EXTRACT(MINUTE FROM (a.published_ts AT TIME ZONE 'America/New_York')) >= 1020 THEN
                                   ((a.published_ts AT TIME ZONE 'America/New_York') + INTERVAL '1 day')::date
                               ELSE
                                   (a.published_ts AT TIME ZONE 'America/New_York')::date
                           END

                       -- China (closes at 15:00 Shanghai)
                       WHEN si.ticker LIKE '%%.SS' OR si.ticker LIKE '%%.SZ' THEN
                           CASE 
                               WHEN EXTRACT(HOUR FROM (a.published_ts AT TIME ZONE 'Asia/Shanghai')) * 60 + EXTRACT(MINUTE FROM (a.published_ts AT TIME ZONE 'Asia/Shanghai')) >= 900 THEN
                                   ((a.published_ts AT TIME ZONE 'Asia/Shanghai') + INTERVAL '1 day')::date
                               ELSE
                                   (a.published_ts AT TIME ZONE 'Asia/Shanghai')::date
                           END

                       -- Korea (closes at 15:30 Seoul)
                       WHEN si.ticker LIKE '%%.KS' THEN
                           CASE 
                               WHEN EXTRACT(HOUR FROM (a.published_ts AT TIME ZONE 'Asia/Seoul')) * 60 + EXTRACT(MINUTE FROM (a.published_ts AT TIME ZONE 'Asia/Seoul')) >= 930 THEN
                                   ((a.published_ts AT TIME ZONE 'Asia/Seoul') + INTERVAL '1 day')::date
                               ELSE
                                   (a.published_ts AT TIME ZONE 'Asia/Seoul')::date
                           END

                       -- Japan (closes at 15:00 Tokyo)
                       WHEN si.ticker LIKE '%%.T' THEN
                           CASE 
                               WHEN EXTRACT(HOUR FROM (a.published_ts AT TIME ZONE 'Asia/Tokyo')) * 60 + EXTRACT(MINUTE FROM (a.published_ts AT TIME ZONE 'Asia/Tokyo')) >= 900 THEN
                                   ((a.published_ts AT TIME ZONE 'Asia/Tokyo') + INTERVAL '1 day')::date
                               ELSE
                                   (a.published_ts AT TIME ZONE 'Asia/Tokyo')::date
                           END

                       -- US (closes at 16:00 NY)
                       ELSE
                           CASE 
                               WHEN EXTRACT(HOUR FROM (a.published_ts AT TIME ZONE 'America/New_York')) * 60 + EXTRACT(MINUTE FROM (a.published_ts AT TIME ZONE 'America/New_York')) >= 960 THEN
                                   ((a.published_ts AT TIME ZONE 'America/New_York') + INTERVAL '1 day')::date
                               ELSE
                                   (a.published_ts AT TIME ZONE 'America/New_York')::date
                           END
                   END as date,
                   si.sentiment_score,
                   si.article_id
            FROM {settings.mimir_schema}.mimir_sentiment_impacts si
            JOIN {settings.mimir_schema}.mimir_raw_articles a ON si.article_id = a.id
            WHERE si.ticker = %s AND (a.published_ts AT TIME ZONE 'UTC')::date >= %s
        )
        SELECT date, AVG(sentiment_score) as sentiment, COUNT(article_id) as sent_vol
        FROM adjusted_sentiment
        GROUP BY date
        ORDER BY date ASC
    """
    try:
        cur.execute(sql, (ticker, start_date))
        rows = cur.fetchall()
        if not rows:
            return pd.DataFrame(columns=['date', 'sentiment', 'sent_vol'])
        df_sent = pd.DataFrame(rows, columns=['date', 'sentiment', 'sent_vol'])
        df_sent['date'] = pd.to_datetime(df_sent['date']).dt.date
        return df_sent
    except Exception as e:
        print(f"[SIGNAL_FUSION] Error fetching sentiment history for {ticker}: {e}")
        return pd.DataFrame(columns=['date', 'sentiment'])
    finally:
        cur.close()
        if close_conn:
            conn.close()

_MODEL_CACHE: Dict[str, Any] = {}
_MODEL_CACHE_MTIME: Dict[str, float] = {}

def get_xgb_prediction(ticker: str, features: pd.DataFrame, side: str) -> float:
    """Loads ticker-specific or global fallback XGBoost model to predict signal probability with in-memory caching."""
    import xgboost as xgb
    from pathlib import Path
    
    # Models are saved in backend/app/analytics/models/
    models_dir = Path(__file__).parent / "models"
    
    ticker_model_path = models_dir / f"{ticker.lower()}_{side}.json"
    global_model_path = models_dir / f"global_{side}.json"
    
    model_path = None
    if ticker_model_path.exists():
        model_path = ticker_model_path
    elif global_model_path.exists():
        model_path = global_model_path
        
    if model_path is None:
        return 0.50
        
    try:
        path_str = str(model_path)
        mtime = model_path.stat().st_mtime
        
        # Load or refresh cached model if modified
        if path_str not in _MODEL_CACHE or _MODEL_CACHE_MTIME.get(path_str) != mtime:
            booster = xgb.Booster()
            booster.load_model(path_str)
            _MODEL_CACHE[path_str] = booster
            _MODEL_CACHE_MTIME[path_str] = mtime
        else:
            booster = _MODEL_CACHE[path_str]
            
        if booster.feature_names:
            missing = [f for f in booster.feature_names if f not in features.columns]
            if missing:
                for m in missing:
                    features[m] = np.nan
            features = features[booster.feature_names]
            
        dmat = xgb.DMatrix(features)
        pred = booster.predict(dmat)
        return float(pred[0])
    except Exception as e:
        print(f"[SIGNAL_FUSION] Error predicting with XGBoost model for {ticker} ({side}): {e}")
        return 0.50

def scan_ticker_for_signals(
    ticker: str, 
    conn=None,
    df_prices: Optional[pd.DataFrame] = None,
    df_sent: Optional[pd.DataFrame] = None,
    ticker_params: Optional[Dict[str, Any]] = None,
    fundamentals: Optional[Dict[str, float]] = None,
    live_success_rate: Optional[float] = None
) -> Optional[Dict[str, Any]]:
    """Scans a single ticker and returns a signal dict if triggered and inserted."""
    ticker = ticker.strip().upper()
    
    if df_prices is None:
        df = get_recent_prices(ticker, days=120, conn=conn)
    else:
        df = df_prices.copy()
        
    if df.empty or len(df) < 20:
        return None
        
    if df_sent is None:
        df_sent = get_daily_sentiment_history(ticker, days=30, conn=conn)
    else:
        df_sent = df_sent.copy()
    
    # Merge price and sentiment timezone-aligned
    df['date_only'] = df.index.date
    df_merged = pd.merge(df, df_sent, left_on='date_only', right_on='date', how='left')
    df_merged['sentiment'] = df_merged['sentiment'].fillna(0.0)
    
    # Calculate feature columns exactly as constructed in training
    def get_rsi(series, window=14):
        delta = series.diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.rolling(window=window, min_periods=window).mean()
        avg_loss = loss.rolling(window=window, min_periods=window).mean()
        rs = avg_gain / (avg_loss + 1e-15)
        return 100 - (100 / (1 + rs))

    df_merged['rsi'] = get_rsi(df_merged['close'])
    df_merged['support'] = df_merged['low'].rolling(20).min()
    df_merged['resistance'] = df_merged['high'].rolling(20).max()
    df_merged['volume_ma'] = df_merged['volume'].rolling(20).mean()
    df_merged['volume_ratio'] = df_merged['volume'] / (df_merged['volume_ma'] + 1e-15)
    df_merged['close_to_support'] = (df_merged['close'] - df_merged['support']) / (df_merged['support'] + 1e-15)
    df_merged['close_to_resistance'] = (df_merged['resistance'] - df_merged['close']) / (df_merged['resistance'] + 1e-15)
    
    df_merged['sentiment_1d'] = df_merged['sentiment']
    df_merged['sentiment_3d'] = df_merged['sentiment'].rolling(3, min_periods=1).mean()
    df_merged['sentiment_5d'] = df_merged['sentiment'].rolling(5, min_periods=1).mean()
    
    if 'sent_vol' not in df_merged.columns:
        df_merged['sent_vol'] = 0.0
    else:
        df_merged['sent_vol'] = df_merged['sent_vol'].fillna(0.0)
        
    df_merged['sent_vol_1d'] = df_merged['sent_vol']
    df_merged['sent_vol_30d'] = df_merged['sent_vol'].rolling(30, min_periods=1).mean()
    df_merged['relative_volume'] = df_merged['sent_vol_1d'] / (df_merged['sent_vol_30d'] + 1e-5)
    
    df_merged['carvs_1d'] = df_merged['sentiment_1d'] * df_merged['relative_volume']
    df_merged['carvs_3d'] = df_merged['sentiment_3d'] * df_merged['relative_volume']
    df_merged['carvs_5d'] = df_merged['sentiment_5d'] * df_merged['relative_volume']
    
    df_merged['price_momentum_5d'] = df_merged['close'].pct_change(5)
    df_merged['price_momentum_10d'] = df_merged['close'].pct_change(10)
    pct_change = df_merged['close'].pct_change()
    df_merged['volatility_20d'] = pct_change.rolling(20).std()
    
    # New Technicals
    df_merged['ma20'] = df_merged['close'].rolling(20).mean()
    df_merged['ma50'] = df_merged['close'].rolling(50).mean()
    df_merged['ma20_ma50_ratio'] = df_merged['ma20'] / (df_merged['ma50'] + 1e-15)
    
    ema12 = df_merged['close'].ewm(span=12, adjust=False).mean()
    ema26 = df_merged['close'].ewm(span=26, adjust=False).mean()
    df_merged['macd'] = ema12 - ema26
    df_merged['macd_signal'] = df_merged['macd'].ewm(span=9, adjust=False).mean()
    df_merged['macd_hist'] = df_merged['macd'] - df_merged['macd_signal']
    
    bb_std = df_merged['close'].rolling(20).std()
    df_merged['bb_upper'] = df_merged['ma20'] + (bb_std * 2)
    df_merged['bb_lower'] = df_merged['ma20'] - (bb_std * 2)
    df_merged['bb_width'] = (df_merged['bb_upper'] - df_merged['bb_lower']) / (df_merged['ma20'] + 1e-15)
    
    close_diff = df_merged['close'].diff()
    direction = np.where(close_diff > 0, 1, np.where(close_diff < 0, -1, 0))
    df_merged['obv'] = (direction * df_merged['volume']).cumsum()
    
    df_merged['ichimoku_tenkan'] = (df_merged['high'].rolling(9).max() + df_merged['low'].rolling(9).min()) / 2
    df_merged['ichimoku_kijun'] = (df_merged['high'].rolling(26).max() + df_merged['low'].rolling(26).min()) / 2
    df_merged['ichimoku_senkou_a'] = ((df_merged['ichimoku_tenkan'] + df_merged['ichimoku_kijun']) / 2).shift(26)
    df_merged['ichimoku_senkou_b'] = ((df_merged['high'].rolling(52).max() + df_merged['low'].rolling(52).min()) / 2).shift(26)
    
    # Fundamentals
    if fundamentals is None:
        fundamentals = get_cached_fundamentals(ticker, conn=conn)
        
    if fundamentals:
        df_merged['pe_ratio'] = fundamentals.get("pe_ratio")
        df_merged['debt_to_equity'] = fundamentals.get("debt_to_equity")
        df_merged['eps_growth'] = fundamentals.get("eps_growth")
        df_merged['operating_margin'] = fundamentals.get("operating_margin")
    else:
        for col in ['pe_ratio', 'debt_to_equity', 'eps_growth', 'operating_margin']:
            df_merged[col] = np.nan
            
    # Take the latest row (today)
    today_row = df_merged.iloc[-1]
    feature_cols = [
        'rsi', 'volume_ratio', 'close_to_support', 'close_to_resistance',
        'sentiment_1d', 'sentiment_3d', 'sentiment_5d',
        'relative_volume', 'carvs_1d', 'carvs_3d', 'carvs_5d',
        'price_momentum_5d', 'price_momentum_10d', 'volatility_20d',
        'pe_ratio', 'debt_to_equity', 'eps_growth', 'operating_margin',
        'ma20_ma50_ratio', 'macd', 'macd_signal', 'macd_hist',
        'bb_upper', 'bb_lower', 'bb_width', 'obv',
        'ichimoku_tenkan', 'ichimoku_kijun', 'ichimoku_senkou_a', 'ichimoku_senkou_b'
    ]
    features_df = today_row[feature_cols].to_frame().T.astype(float)
    
    # Load custom optimal thresholds or defaults
    if ticker_params is None:
        ticker_params = get_ticker_parameters(ticker, conn=conn)
    buy_features = feature_cols
    sell_features = feature_cols
    
    if ticker_params:
        target_prob_buy = ticker_params["prob_buy"]
        target_prob_sell = ticker_params["prob_sell"]
        p_win_rate = ticker_params["win_rate"]
        p_avg_pnl = ticker_params["avg_pnl"]
        p_hold_days = ticker_params["hold_days"]
        if ticker_params.get("selected_features_buy"):
            buy_features = ticker_params["selected_features_buy"]
        if ticker_params.get("selected_features_sell"):
            sell_features = ticker_params["selected_features_sell"]
        win_str = f"{p_win_rate:.1f}%" if p_win_rate is not None else "N/A"
        pnl_str = f"{p_avg_pnl:.2f}%" if p_avg_pnl is not None else "N/A"
        param_src = f"Tuned Profile (Hold: {p_hold_days}d, Est. Win Rate: {win_str}, Est. PnL: {pnl_str})"
    else:
        target_prob_buy = 0.62
        target_prob_sell = 0.62
        p_hold_days = 5
        param_src = "Global Fallback Default (High Conviction)"
        
    # Check live feedback to adjust thresholds
    feedback_note = ""
    if live_success_rate is None:
        live_success_rate = get_ticker_live_feedback(ticker, conn=conn)
    if live_success_rate is not None:
        if live_success_rate <= 40.0:
            # If recent trade alert performance is poor, tighten probability boundaries to protect capital
            target_prob_buy = min(0.70, target_prob_buy + 0.05)
            target_prob_sell = min(0.70, target_prob_sell + 0.05)
            feedback_note = f" | [LEARNING LOOP] Poor recent success rate ({live_success_rate:.1f}%). Tightening threshold to 70% max bounds."
            
    # Run XGBoost inference
    features_df_buy = features_df[buy_features] if all(col in features_df.columns for col in buy_features) else features_df
    features_df_sell = features_df[sell_features] if all(col in features_df.columns for col in sell_features) else features_df
    prob_buy = get_xgb_prediction(ticker, features_df_buy, "buy")
    prob_sell = get_xgb_prediction(ticker, features_df_sell, "sell")
    
    signal_type = None
    reason = []
    
    current_price = float(today_row['close'])
    rsi = float(today_row['rsi'])
    sentiment = float(today_row['sentiment'])
    support = float(today_row['support']) if not pd.isna(today_row['support']) else current_price
    resistance = float(today_row['resistance']) if not pd.isna(today_row['resistance']) else current_price
    
    # 1. BUY Signal check
    if prob_buy >= target_prob_buy:
        passes_fundamentals = True
        fund_fail_reason = ""
        
        if fundamentals:
            pe = fundamentals.get("pe_ratio")
            de = fundamentals.get("debt_to_equity")
            eps = fundamentals.get("eps_growth")
            
            if pe is not None and (pe < 0 or pe > 35):
                passes_fundamentals = False
                fund_fail_reason = f"PE ratio ({pe:.1f}) is out of bounds (0-35)"
            if de is not None and de > 250:
                passes_fundamentals = False
                fund_fail_reason = f"Debt-to-Equity ({de:.1f}%) exceeds threshold (250%)"
            if eps is not None and eps < -0.2:
                passes_fundamentals = False
                fund_fail_reason = f"EPS growth ({eps * 100:.1f}%) is worse than -20%"
                
        if not passes_fundamentals:
            print(f"[SIGNAL_FUSION] {ticker} rejected by fundamentals overlay: {fund_fail_reason}")
        else:
            signal_type = 'BUY'
            reason.append(f"XGBoost BUY prediction prob ({prob_buy * 100.0:.1f}%) >= threshold ({target_prob_buy * 100.0:.1f}%). [Params: {param_src}{feedback_note}]")
            
    # 2. SELL Signal check
    elif prob_sell >= target_prob_sell:
        signal_type = 'SELL'
        reason.append(f"XGBoost SELL prediction prob ({prob_sell * 100.0:.1f}%) >= threshold ({target_prob_sell * 100.0:.1f}%). [Params: {param_src}{feedback_note}]")
        
    if signal_type:
        reason_str = " | ".join(reason)
        if not check_duplicate_signal(ticker, signal_type, conn=conn):
            success = insert_trade_signal(ticker, signal_type, current_price, rsi, sentiment, support, resistance, reason_str, conn=conn)
            if success:
                return {
                    "ticker": ticker,
                    "signal_type": signal_type,
                    "trigger_price": current_price,
                    "rsi": rsi,
                    "sentiment": sentiment,
                    "support": support,
                    "resistance": resistance,
                    "reason": reason_str
                }
    return None

def scan_all_tickers() -> List[Dict[str, Any]]:
    """Runs a full scan of default tickers and dynamic tickers for signals using bulk pre-fetching."""
    import json
    from collections import defaultdict
    
    conn = get_db_connection()
    cur = conn.cursor()
    
    # 1. Fetch target tickers
    tickers = set([t.strip().upper() for t in DEFAULT_TICKERS if t])
    try:
        cur.execute(f"SELECT DISTINCT ticker FROM {settings.mimir_schema}.mimir_dynamic_tickers WHERE ticker IS NOT NULL")
        for row in cur.fetchall():
            tickers.add(row[0].strip().upper())
    except Exception:
        pass

    # 2. Bulk fetch prices for last 120 days
    start_date = (datetime.now() - timedelta(days=120)).date()
    prices_by_ticker = {}
    try:
        cur.execute(f"""
            SELECT ticker, date, open, high, low, close, volume
            FROM {settings.mimir_schema}.v_mimir_daily_ohlcv
            WHERE date >= %s
            ORDER BY ticker, date ASC
        """, (start_date,))
        price_rows = cur.fetchall()
        if price_rows:
            df_all = pd.DataFrame(price_rows, columns=['ticker', 'date', 'open', 'high', 'low', 'close', 'volume'])
            df_all['date'] = pd.to_datetime(df_all['date'])
            for col in ['open', 'high', 'low', 'close', 'volume']:
                df_all[col] = pd.to_numeric(df_all[col], errors='coerce')
            for t, grp in df_all.groupby('ticker'):
                t_upper = t.strip().upper()
                if t_upper in tickers:
                    prices_by_ticker[t_upper] = grp.drop(columns=['ticker']).set_index('date')
    except Exception as e:
        print(f"[SIGNAL_FUSION] Bulk price fetch warning: {e}")

    # 3. Bulk fetch sentiment history for last 30 days
    sent_start_date = (datetime.now() - timedelta(days=30)).date()
    sentiment_by_ticker = {}
    try:
        cur.execute(f"""
            WITH adjusted_sentiment AS (
                SELECT si.ticker,
                       CASE 
                           WHEN si.ticker LIKE '%%-USD' THEN (a.published_ts AT TIME ZONE 'UTC')::date
                           WHEN si.ticker LIKE '%%=X' OR si.ticker LIKE '%%=F' THEN
                               CASE WHEN EXTRACT(HOUR FROM (a.published_ts AT TIME ZONE 'America/New_York')) * 60 + EXTRACT(MINUTE FROM (a.published_ts AT TIME ZONE 'America/New_York')) >= 1020 
                                    THEN ((a.published_ts AT TIME ZONE 'America/New_York') + INTERVAL '1 day')::date
                                    ELSE (a.published_ts AT TIME ZONE 'America/New_York')::date END
                           WHEN si.ticker LIKE '%%.SS' OR si.ticker LIKE '%%.SZ' THEN
                               CASE WHEN EXTRACT(HOUR FROM (a.published_ts AT TIME ZONE 'Asia/Shanghai')) * 60 + EXTRACT(MINUTE FROM (a.published_ts AT TIME ZONE 'Asia/Shanghai')) >= 900 
                                    THEN ((a.published_ts AT TIME ZONE 'Asia/Shanghai') + INTERVAL '1 day')::date
                                    ELSE (a.published_ts AT TIME ZONE 'Asia/Shanghai')::date END
                           WHEN si.ticker LIKE '%%.KS' THEN
                               CASE WHEN EXTRACT(HOUR FROM (a.published_ts AT TIME ZONE 'Asia/Seoul')) * 60 + EXTRACT(MINUTE FROM (a.published_ts AT TIME ZONE 'Asia/Seoul')) >= 930 
                                    THEN ((a.published_ts AT TIME ZONE 'Asia/Seoul') + INTERVAL '1 day')::date
                                    ELSE (a.published_ts AT TIME ZONE 'Asia/Seoul')::date END
                           WHEN si.ticker LIKE '%%.T' THEN
                               CASE WHEN EXTRACT(HOUR FROM (a.published_ts AT TIME ZONE 'Asia/Tokyo')) * 60 + EXTRACT(MINUTE FROM (a.published_ts AT TIME ZONE 'Asia/Tokyo')) >= 900 
                                    THEN ((a.published_ts AT TIME ZONE 'Asia/Tokyo') + INTERVAL '1 day')::date
                                    ELSE (a.published_ts AT TIME ZONE 'Asia/Tokyo')::date END
                           ELSE
                               CASE WHEN EXTRACT(HOUR FROM (a.published_ts AT TIME ZONE 'America/New_York')) * 60 + EXTRACT(MINUTE FROM (a.published_ts AT TIME ZONE 'America/New_York')) >= 960 
                                    THEN ((a.published_ts AT TIME ZONE 'America/New_York') + INTERVAL '1 day')::date
                                    ELSE (a.published_ts AT TIME ZONE 'America/New_York')::date END
                       END as date,
                       si.sentiment_score,
                       si.article_id
                FROM {settings.mimir_schema}.mimir_sentiment_impacts si
                JOIN {settings.mimir_schema}.mimir_raw_articles a ON si.article_id = a.id
                WHERE (a.published_ts AT TIME ZONE 'UTC')::date >= %s AND si.ticker IS NOT NULL
            )
            SELECT ticker, date, AVG(sentiment_score) as sentiment, COUNT(article_id) as sent_vol
            FROM adjusted_sentiment
            GROUP BY ticker, date
            ORDER BY ticker, date ASC
        """, (sent_start_date,))
        sent_rows = cur.fetchall()
        if sent_rows:
            df_sent_all = pd.DataFrame(sent_rows, columns=['ticker', 'date', 'sentiment', 'sent_vol'])
            df_sent_all['date'] = pd.to_datetime(df_sent_all['date']).dt.date
            for t, grp in df_sent_all.groupby('ticker'):
                sentiment_by_ticker[t.strip().upper()] = grp[['date', 'sentiment', 'sent_vol']].reset_index(drop=True)
    except Exception as e:
        print(f"[SIGNAL_FUSION] Bulk sentiment fetch warning: {e}")

    # 4. Bulk fetch parameters
    params_map = {}
    try:
        cur.execute(f"""
            SELECT ticker, optimal_rsi_buy, optimal_rsi_sell, optimal_sentiment, optimal_vol_ratio, optimal_hold_days, 
                   win_rate, avg_pnl, optimal_prob_buy, optimal_prob_sell, selected_features_buy, selected_features_sell
            FROM {settings.mimir_schema}.mimir_ticker_parameters
        """)
        for r in cur.fetchall():
            params_map[r[0].strip().upper()] = {
                "rsi_buy": float(r[1]),
                "rsi_sell": float(r[2]),
                "sentiment": float(r[3]),
                "vol_ratio": float(r[4]),
                "hold_days": int(r[5]),
                "win_rate": float(r[6]) if r[6] is not None else None,
                "avg_pnl": float(r[7]) if r[7] is not None else None,
                "prob_buy": float(r[8]) if r[8] is not None else 0.55,
                "prob_sell": float(r[9]) if r[9] is not None else 0.55,
                "selected_features_buy": json.loads(r[10]) if r[10] else None,
                "selected_features_sell": json.loads(r[11]) if r[11] else None
            }
    except Exception as e:
        print(f"[SIGNAL_FUSION] Bulk params fetch warning: {e}")

    # 5. Bulk fetch fundamentals
    fund_map = {}
    try:
        cur.execute(f"""
            SELECT ticker, pe_ratio, debt_to_equity, eps_growth, operating_margin 
            FROM {settings.mimir_schema}.mimir_asset_fundamentals
        """)
        for r in cur.fetchall():
            fund_map[r[0].strip().upper()] = {
                "pe_ratio": float(r[1]) if r[1] is not None else None,
                "debt_to_equity": float(r[2]) if r[2] is not None else None,
                "eps_growth": float(r[3]) if r[3] is not None else None,
                "operating_margin": float(r[4]) if r[4] is not None else None
            }
    except Exception as e:
        print(f"[SIGNAL_FUSION] Bulk fundamentals fetch warning: {e}")

    # 6. Bulk fetch live feedback
    feedback_map = {}
    try:
        cur.execute(f"""
            SELECT ticker, evaluation_status
            FROM {settings.mimir_schema}.mimir_trade_signals
            WHERE evaluation_status IS NOT NULL
            ORDER BY created_at DESC
        """)
        fb_grouped = defaultdict(list)
        for r in cur.fetchall():
            t_upper = r[0].strip().upper()
            if len(fb_grouped[t_upper]) < 5:
                fb_grouped[t_upper].append(r[1])
        for t_upper, statuses in fb_grouped.items():
            if statuses:
                successes = sum(1 for s in statuses if s == 'SUCCESSFUL')
                feedback_map[t_upper] = (successes / len(statuses)) * 100.0
    except Exception as e:
        print(f"[SIGNAL_FUSION] Bulk feedback fetch warning: {e}")

    cur.close()

    # Determine which tickers to scan: only those with available price data!
    target_tickers = [t for t in tickers if t in prices_by_ticker and len(prices_by_ticker[t]) >= 20]
    print(f"[SIGNAL_FUSION] Bulk scan executing for {len(target_tickers)} active tickers (skipped {len(tickers) - len(target_tickers)} inactive/no-price tickers)...")

    new_signals = []
    try:
        for ticker in target_tickers:
            sig = scan_ticker_for_signals(
                ticker,
                conn=conn,
                df_prices=prices_by_ticker.get(ticker),
                df_sent=sentiment_by_ticker.get(ticker, pd.DataFrame(columns=['date', 'sentiment'])),
                ticker_params=params_map.get(ticker),
                fundamentals=fund_map.get(ticker),
                live_success_rate=feedback_map.get(ticker)
            )
            if sig:
                new_signals.append(sig)
                print(f"[SIGNAL_FUSION] Generated {sig['signal_type']} signal for {ticker}: {sig['reason']}")
    finally:
        conn.close()
            
    return new_signals

def validate_sentiment_with_price(price_cache):
    """Event-driven hook to invalidate LLM sentiment signals if 1-minute price action strongly diverges."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        
        # Find highly polarized sentiment impacts from the last 30 minutes
        cur.execute(f"""
            SELECT article_id, asset_name, ticker, sentiment_score 
            FROM {settings.mimir_schema}.mimir_sentiment_impacts 
            WHERE created_at >= NOW() - INTERVAL '30 minutes'
            AND abs(sentiment_score) > 0.5
        """)
        recent_impacts = cur.fetchall()
        
        for impact in recent_impacts:
            article_id, asset_name, ticker, score = impact
            
            if ticker not in price_cache or len(price_cache[ticker]) < 15:
                continue
                
            ticks = list(price_cache[ticker])
            start_price = ticks[-15]['close']
            end_price = ticks[-1]['close']
            pct_change = (end_price - start_price) / start_price
            
            # If Sentiment is very bullish (> 0.5) but price drops > 1% in 15m (Bull Trap)
            if score > 0.5 and pct_change <= -0.01:
                cur.execute(f"""
                    UPDATE {settings.mimir_schema}.mimir_sentiment_impacts
                    SET sentiment_score = 0.0
                    WHERE article_id = %s AND asset_name = %s
                """, (article_id, asset_name))
                print(f"[SENTIMENT FILTER] Neutralized BULLISH sentiment for {ticker} (Price dropped {pct_change*100:.2f}%)")
            
            # If Sentiment is very bearish (< -0.5) but price pumps > 1% in 15m (Bear Trap)
            elif score < -0.5 and pct_change >= 0.01:
                cur.execute(f"""
                    UPDATE {settings.mimir_schema}.mimir_sentiment_impacts
                    SET sentiment_score = 0.0
                    WHERE article_id = %s AND asset_name = %s
                """, (article_id, asset_name))
                print(f"[SENTIMENT FILTER] Neutralized BEARISH sentiment for {ticker} (Price surged {pct_change*100:.2f}%)")
                
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"[SENTIMENT FILTER ERROR] {e}")
    finally:
        cur.close()
        conn.close()
