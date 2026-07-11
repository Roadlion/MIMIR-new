# scripts/tune_ticker_parameters.py
import os
import sys
import numpy as np
import pandas as pd
from pathlib import Path
from datetime import datetime

# Adjust path to import backend
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.database import get_db_connection
from backend.app.config import get_settings

settings = get_settings()

def load_data():
    """Loads price and sentiment data from database."""
    print("Loading price and sentiment data from database...")
    conn = get_db_connection()
    
    # 1. Load daily OHLCV prices
    price_sql = f"""
        SELECT ticker, date, open, high, low, close, volume 
        FROM {settings.mimir_schema}.v_mimir_daily_ohlcv
        WHERE date >= '2025-01-01'
        ORDER BY ticker, date ASC
    """
    df_prices = pd.read_sql(price_sql, conn)
    print(f"Loaded {len(df_prices)} price records.")
    
    # 2. Load daily average sentiment impacts (lookahead-bias-free timezone alignment)
    sent_sql = f"""
        WITH adjusted_sentiment AS (
            SELECT si.ticker, 
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
                   si.sentiment_score
            FROM {settings.mimir_schema}.mimir_sentiment_impacts si
            JOIN {settings.mimir_schema}.mimir_raw_articles a ON si.article_id = a.id
            WHERE a.published_ts >= '2025-01-01' AND si.ticker IS NOT NULL
        )
        SELECT ticker, date, AVG(sentiment_score) as sentiment
        FROM adjusted_sentiment
        GROUP BY ticker, date
    """
    df_sent = pd.read_sql(sent_sql, conn)
    print(f"Loaded {len(df_sent)} daily sentiment records.")
    
    conn.close()
    
    # Standardize data types
    df_prices['date'] = pd.to_datetime(df_prices['date']).dt.date
    df_sent['date'] = pd.to_datetime(df_sent['date']).dt.date
    
    # Merge datasets
    df = pd.merge(df_prices, df_sent, on=['ticker', 'date'], how='left')
    df['sentiment'] = df['sentiment'].fillna(0.0)
    
    return df

def calculate_indicators(df):
    """Calculates technical indicators: RSI, Support, Resistance, Volume Ratio."""
    # Calculate RSI
    def get_rsi(series, window=14):
        delta = series.diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.rolling(window=window, min_periods=window).mean()
        avg_loss = loss.rolling(window=window, min_periods=window).mean()
        rs = avg_gain / (avg_loss + 1e-15)
        return 100 - (100 / (1 + rs))

    df['rsi'] = df.groupby('ticker')['close'].transform(lambda x: get_rsi(x))
    df['support'] = df.groupby('ticker')['low'].transform(lambda x: x.rolling(20).min())
    df['resistance'] = df.groupby('ticker')['high'].transform(lambda x: x.rolling(20).max())
    df['volume_ma'] = df.groupby('ticker')['volume'].transform(lambda x: x.rolling(20).mean())
    df['volume_ratio'] = df['volume'] / (df['volume_ma'] + 1e-15)
    
    df = df.dropna(subset=['rsi', 'support', 'resistance', 'volume_ma']).copy()
    return df

def run_backtest_for_ticker(ticker, df_ticker, sent_thresh, rsi_buy, rsi_sell, vol_ratio_thresh, holding_period=5, slippage_bps=5.0):
    """Simulates trading strategy on a single ticker's dataframe."""
    df_ticker = df_ticker.copy().sort_values('date').reset_index(drop=True)
    
    buy_signals = (
        (df_ticker['sentiment'] >= sent_thresh) & 
        (df_ticker['rsi'] <= rsi_buy) & 
        (df_ticker['close'] <= df_ticker['support'] * 1.02) & 
        (df_ticker['volume_ratio'] >= vol_ratio_thresh)
    ).values
    
    sell_signals = (
        (df_ticker['sentiment'] <= -sent_thresh) & 
        (df_ticker['rsi'] >= rsi_sell) & 
        (df_ticker['close'] >= df_ticker['resistance'] * 0.98) & 
        (df_ticker['volume_ratio'] >= vol_ratio_thresh)
    ).values
    
    opens = df_ticker['open'].values
    closes = df_ticker['close'].values
    dates = df_ticker['date'].values
    
    trades = []
    in_trade = False
    entry_price = 0.0
    entry_date = None
    days_held = 0
    n_rows = len(df_ticker)
    
    for idx in range(n_rows):
        if not in_trade:
            if buy_signals[idx]:
                if idx + 1 < n_rows:
                    entry_price = float(opens[idx + 1])
                    entry_date = dates[idx + 1]
                    in_trade = True
                    days_held = 0
        else:
            days_held += 1
            should_exit = (days_held >= holding_period) or sell_signals[idx]
            if should_exit:
                exit_price = float(closes[idx])
                exit_date = dates[idx]
                
                # Compute PnL % (deduct slippage: 2 * slippage_bps / 10000)
                pnl = ((exit_price - entry_price) / entry_price) - (2.0 * slippage_bps / 10000.0)
                pnl_pct = pnl * 100.0
                
                trades.append(pnl_pct)
                in_trade = False
                
    if not trades:
        return 0, 0.0, 0.0
        
    total_trades = len(trades)
    successful_trades = sum(1 for p in trades if p > 0)
    win_rate = (successful_trades / total_trades) * 100.0
    avg_pnl = np.mean(trades)
    
    return total_trades, win_rate, avg_pnl

def tune_all_tickers():
    df = load_data()
    df = calculate_indicators(df)
    
    print("\n--- Tuning Tickers ---")
    
    # Define Parameter Grid
    sent_grid = [0.1, 0.2, 0.3]
    rsi_grid = [30, 35, 40, 45]
    vol_grid = [1.0, 1.3]
    hold_grid = [3, 5, 10]
    
    conn = get_db_connection()
    cur = conn.cursor()
    
    # 1. Fetch tickers that have active sentiment records to avoid useless grid searching
    cur.execute(f"""
        SELECT DISTINCT ticker 
        FROM {settings.mimir_schema}.mimir_sentiment_impacts 
        WHERE ticker IS NOT NULL
    """)
    tickers_with_sentiment = {row[0].strip().upper() for row in cur.fetchall()}
    print(f"Found {len(tickers_with_sentiment)} tickers with active news sentiment records.")
    
    ticker_groups = list(df.groupby('ticker'))
    total_tickers = len(ticker_groups)
    
    tuned_count = 0
    
    for i, (ticker, group) in enumerate(ticker_groups, 1):
        ticker_upper = ticker.upper()
        
        # If ticker has no sentiment news, save default parameter profile instantly to save CPU
        if ticker_upper not in tickers_with_sentiment:
            cur.execute(f"""
                INSERT INTO {settings.mimir_schema}.mimir_ticker_parameters (
                    ticker, optimal_rsi_buy, optimal_rsi_sell, optimal_sentiment, optimal_vol_ratio, optimal_hold_days, win_rate, avg_pnl, total_trades, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (ticker) DO UPDATE SET
                    optimal_rsi_buy = EXCLUDED.optimal_rsi_buy,
                    optimal_rsi_sell = EXCLUDED.optimal_rsi_sell,
                    optimal_sentiment = EXCLUDED.optimal_sentiment,
                    optimal_vol_ratio = EXCLUDED.optimal_vol_ratio,
                    optimal_hold_days = EXCLUDED.optimal_hold_days,
                    win_rate = EXCLUDED.win_rate,
                    avg_pnl = EXCLUDED.avg_pnl,
                    total_trades = EXCLUDED.total_trades,
                    updated_at = NOW();
            """, (ticker, 30.0, 65.0, 0.3, 1.0, 10, None, None, 0))
            continue
            
        print(f"[{i}/{total_tickers}] Tuning parameters for {ticker}...")
        
        best_config = None
        best_avg_pnl = -999.0
        best_win_rate = 0.0
        best_total_trades = 0
        
        # Grid Search
        for sent in sent_grid:
            for rsi in rsi_grid:
                for vol in vol_grid:
                    for hold in hold_grid:
                        trades, win_rate, avg_pnl = run_backtest_for_ticker(
                            ticker, group, sent, rsi, 65, vol, holding_period=hold
                        )
                        
                        if trades > 0:
                            # Selection Criteria:
                            # 1. Require at least 3 trades to prevent overfitting to 1-2 anomalies.
                            # 2. If it meets trade count, maximize avg_pnl.
                            is_better = False
                            if trades >= 3:
                                if best_total_trades < 3:
                                    is_better = True
                                elif avg_pnl > best_avg_pnl:
                                    is_better = True
                            elif best_total_trades < 3 and trades > best_total_trades:
                                is_better = True
                            elif best_total_trades < 3 and trades == best_total_trades and avg_pnl > best_avg_pnl:
                                is_better = True
                                
                            if is_better:
                                best_config = (rsi, sent, vol, hold)
                                best_avg_pnl = avg_pnl
                                best_win_rate = win_rate
                                best_total_trades = trades
                                
        if best_config:
            rsi_b, sent_t, vol_r, hold_d = best_config
            print(f"  -> Optimal Config for {ticker}: RSI <= {rsi_b} | Sent >= {sent_t} | Vol >= {vol_r} | Hold {hold_d}d (PnL: {best_avg_pnl:.2f}%, Win Rate: {best_win_rate:.1f}%, Trades: {best_total_trades})")
            
            # Save parameter profile to database, casting numpy types to python standard types
            cur.execute(f"""
                INSERT INTO {settings.mimir_schema}.mimir_ticker_parameters (
                    ticker, optimal_rsi_buy, optimal_rsi_sell, optimal_sentiment, optimal_vol_ratio, optimal_hold_days, win_rate, avg_pnl, total_trades, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (ticker) DO UPDATE SET
                    optimal_rsi_buy = EXCLUDED.optimal_rsi_buy,
                    optimal_rsi_sell = EXCLUDED.optimal_rsi_sell,
                    optimal_sentiment = EXCLUDED.optimal_sentiment,
                    optimal_vol_ratio = EXCLUDED.optimal_vol_ratio,
                    optimal_hold_days = EXCLUDED.optimal_hold_days,
                    win_rate = EXCLUDED.win_rate,
                    avg_pnl = EXCLUDED.avg_pnl,
                    total_trades = EXCLUDED.total_trades,
                    updated_at = NOW();
            """, (ticker, float(rsi_b), 65.0, float(sent_t), float(vol_r), int(hold_d), float(best_win_rate), float(best_avg_pnl), int(best_total_trades)))
            tuned_count += 1
        else:
            # Fallback to defaults
            print(f"  -> No historical trades generated for {ticker}. Using defaults.")
            cur.execute(f"""
                INSERT INTO {settings.mimir_schema}.mimir_ticker_parameters (
                    ticker, optimal_rsi_buy, optimal_rsi_sell, optimal_sentiment, optimal_vol_ratio, optimal_hold_days, win_rate, avg_pnl, total_trades, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (ticker) DO UPDATE SET
                    optimal_rsi_buy = EXCLUDED.optimal_rsi_buy,
                    optimal_rsi_sell = EXCLUDED.optimal_rsi_sell,
                    optimal_sentiment = EXCLUDED.optimal_sentiment,
                    optimal_vol_ratio = EXCLUDED.optimal_vol_ratio,
                    optimal_hold_days = EXCLUDED.optimal_hold_days,
                    win_rate = EXCLUDED.win_rate,
                    avg_pnl = EXCLUDED.avg_pnl,
                    total_trades = EXCLUDED.total_trades,
                    updated_at = NOW();
            """, (ticker, 30.0, 65.0, 0.3, 1.0, 10, None, None, 0))
            
    conn.commit()
    cur.close()
    conn.close()
    print(f"\n[OK] Tuning complete. Successfully tuned parameters for {tuned_count} tickers.")

if __name__ == "__main__":
    tune_all_tickers()
