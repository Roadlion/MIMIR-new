# scripts/tune_ticker_parameters.py
import os
import sys
import numpy as np
import pandas as pd
from pathlib import Path
from datetime import datetime
import xgboost as xgb
from sklearn.metrics import precision_score, recall_score, accuracy_score

# Adjust path to import backend
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.database import get_db_connection
from backend.app.config import get_settings

settings = get_settings()
MODELS_DIR = PROJECT_ROOT / "backend" / "app" / "analytics" / "models"
os.makedirs(MODELS_DIR, exist_ok=True)

def check_and_migrate_db():
    """Alters the database table to add optimal probability columns if not exists."""
    print("Checking and migrating database schema...")
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute(f"""
            ALTER TABLE {settings.mimir_schema}.mimir_ticker_parameters
            ADD COLUMN IF NOT EXISTS optimal_prob_buy NUMERIC DEFAULT 0.55,
            ADD COLUMN IF NOT EXISTS optimal_prob_sell NUMERIC DEFAULT 0.55;
        """)
        conn.commit()
        print("[OK] Database migration complete: optimal_prob_buy and optimal_prob_sell columns verified.")
    except Exception as e:
        conn.rollback()
        print(f"[ERROR] Database migration failed: {e}")
    finally:
        cur.close()
        conn.close()

def load_data():
    """Loads price, sentiment, and fundamentals from the database."""
    print("Loading price and sentiment data from database...")
    conn = get_db_connection()
    
    # 1. Load daily OHLCV prices (2025 onwards)
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
    
    # 3. Load fundamentals
    fund_sql = f"""
        SELECT ticker, pe_ratio, debt_to_equity, eps_growth, operating_margin 
        FROM {settings.mimir_schema}.mimir_asset_fundamentals
    """
    df_fund = pd.read_sql(fund_sql, conn)
    print(f"Loaded fundamentals for {len(df_fund)} tickers.")
    
    conn.close()
    
    # Standardize data types
    df_prices['date'] = pd.to_datetime(df_prices['date']).dt.date
    df_sent['date'] = pd.to_datetime(df_sent['date']).dt.date
    
    # Merge datasets
    df = pd.merge(df_prices, df_sent, on=['ticker', 'date'], how='left')
    df['sentiment'] = df['sentiment'].fillna(0.0)
    
    # Merge fundamentals
    df = pd.merge(df, df_fund, on=['ticker'], how='left')
    
    # Convert numeric columns
    for col in ['open', 'high', 'low', 'close', 'volume', 'pe_ratio', 'debt_to_equity', 'eps_growth', 'operating_margin']:
        df[col] = pd.to_numeric(df[col], errors='coerce')
        
    return df

def calculate_features_and_targets(df, holding_period=5, slippage_bps=5.0):
    """Calculates lagging technical, sentiment, and fundamental features.
    Also defines binary target values (BUY/SELL) free of lookahead bias.
    """
    print("Calculating technical indicators and sentiment features...")
    df = df.sort_values(['ticker', 'date']).reset_index(drop=True)
    
    # Technicals
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
    
    df['close_to_support'] = (df['close'] - df['support']) / (df['support'] + 1e-15)
    df['close_to_resistance'] = (df['resistance'] - df['close']) / (df['resistance'] + 1e-15)
    
    # Lagged news sentiment rolling averages
    df['sentiment_1d'] = df['sentiment']
    df['sentiment_3d'] = df.groupby('ticker')['sentiment'].transform(lambda x: x.rolling(3, min_periods=1).mean())
    df['sentiment_5d'] = df.groupby('ticker')['sentiment'].transform(lambda x: x.rolling(5, min_periods=1).mean())
    
    # Momentum & Volatility
    df['price_momentum_5d'] = df.groupby('ticker')['close'].transform(lambda x: x.pct_change(5))
    df['price_momentum_10d'] = df.groupby('ticker')['close'].transform(lambda x: x.pct_change(10))
    df['pct_change'] = df.groupby('ticker')['close'].transform(lambda x: x.pct_change())
    df['volatility_20d'] = df.groupby('ticker')['pct_change'].transform(lambda x: x.rolling(20).std())
    df.drop(columns=['pct_change'], inplace=True)
    
    # Define targets
    # Shift open to represent entering at the next session's open
    df['open_next'] = df.groupby('ticker')['open'].shift(-1)
    # Shift close to represent exiting at the close of holding_period sessions later
    df['close_hold'] = df.groupby('ticker')['close'].shift(-holding_period)
    
    df['forward_return'] = (df['close_hold'] - df['open_next']) / (df['open_next'] + 1e-15)
    
    cost = 2.0 * slippage_bps / 10000.0
    df['target_buy'] = (df['forward_return'] > cost).astype(int)
    df['target_sell'] = (-df['forward_return'] > cost).astype(int)
    
    # Drop rows that don't have enough history or future dates (to avoid target NaNs)
    df = df.dropna(subset=['rsi', 'support', 'resistance', 'volatility_20d', 'open_next', 'close_hold']).copy()
    
    return df

FEATURE_COLS = [
    'rsi', 'volume_ratio', 'close_to_support', 'close_to_resistance',
    'sentiment_1d', 'sentiment_3d', 'sentiment_5d',
    'price_momentum_5d', 'price_momentum_10d', 'volatility_20d',
    'pe_ratio', 'debt_to_equity', 'eps_growth', 'operating_margin'
]

def split_chronological_with_purging(df_ticker, val_ratio=0.15, test_ratio=0.15, holding_period=5):
    """Splits single ticker dataframe chronologically, and purges overlapping boundaries
    to prevent forward data leakage.
    """
    df_ticker = df_ticker.sort_values('date').reset_index(drop=True)
    n = len(df_ticker)
    
    val_size = int(n * val_ratio)
    test_size = int(n * test_ratio)
    train_size = n - val_size - test_size
    
    # Boundaries
    train_end = train_size
    val_end = train_size + val_size
    
    # Purge Train target overlaps (drop last H rows of train before Val starts)
    train_indices = np.arange(0, train_end - holding_period)
    val_indices = np.arange(train_end, val_end - holding_period)
    test_indices = np.arange(val_end, n)
    
    train_indices = train_indices[train_indices >= 0]
    val_indices = val_indices[val_indices >= 0]
    
    df_train = df_ticker.iloc[train_indices]
    df_val = df_ticker.iloc[val_indices]
    df_test = df_ticker.iloc[test_indices]
    
    return df_train, df_val, df_test

def train_xgb_model(X_train, y_train, X_val, y_val, model_name="model"):
    """Trains a regularized XGBoost model, tuning depth and estimators."""
    best_model = None
    best_score = -1.0
    
    # Micro grid search for depth & trees to prevent overfitting and stay fast
    depths = [3, 4]
    n_est = [50, 100]
    
    for d in depths:
        for n in n_est:
            # We add L1 and L2 regularization to prevent overfitting
            model = xgb.XGBClassifier(
                max_depth=d,
                n_estimators=n,
                learning_rate=0.05,
                reg_alpha=0.2,       # L1 regularization
                reg_lambda=1.5,      # L2 regularization
                subsample=0.8,
                colsample_bytree=0.8,
                random_state=42,
                eval_metric="logloss"
            )
            model.fit(X_train, y_train)
            
            # Evaluate using validation precision of positive class (successful trades)
            preds = model.predict(X_val)
            # Avoid division by zero warnings
            score = precision_score(y_val, preds, zero_division=0)
            
            if score > best_score:
                best_score = score
                best_model = model
                
    if best_model is None:
        # Fallback default model
        best_model = xgb.XGBClassifier(
            max_depth=3,
            n_estimators=50,
            learning_rate=0.05,
            eval_metric="logloss"
        )
        best_model.fit(X_train, y_train)
        
    return best_model

def optimize_threshold(model, X_val, df_val, side="buy"):
    """Simulates trading at various probability thresholds on validation set
    to pick the optimal threshold maximizing cumulative return.
    """
    if len(df_val) == 0:
        return 0.55, 0.0, 0.0, 0
        
    probs = model.predict_proba(X_val)[:, 1]
    
    thresholds = [0.51, 0.53, 0.55, 0.57, 0.60, 0.63, 0.65]
    best_thresh = 0.55
    best_pnl = -999.0
    best_win_rate = 0.0
    best_trades = 0
    
    returns = df_val['forward_return'].values
    
    for th in thresholds:
        signals = probs >= th
        trade_returns = returns[signals] if side == "buy" else -returns[signals]
        
        if len(trade_returns) >= 1: # Require at least 1 trade in val window to score
            total_trades = len(trade_returns)
            win_rate = sum(1 for r in trade_returns if r > 0) / total_trades * 100.0
            avg_pnl = np.mean(trade_returns) * 100.0
            
            # Objective: Maximize total cumulative PnL
            total_pnl = np.sum(trade_returns) * 100.0
            
            if total_pnl > best_pnl:
                best_pnl = total_pnl
                best_thresh = th
                best_win_rate = win_rate
                best_trades = total_trades
                
    if best_trades == 0:
        # If no trades triggered, fallback default
        return 0.55, 0.0, 0.0, 0
        
    # Return optimal values
    avg_pnl = (best_pnl / best_trades) if best_trades > 0 else 0.0
    return best_thresh, best_win_rate, avg_pnl, best_trades

def train_and_tune_all():
    check_and_migrate_db()
    
    df = load_data()
    holding_period = 5
    df = calculate_features_and_targets(df, holding_period=holding_period)
    
    print("\n--- Training Global Models ---")
    # To build global models, we pool all data and split chronologically by date
    unique_dates = sorted(df['date'].unique())
    n_dates = len(unique_dates)
    
    val_date_boundary = unique_dates[int(n_dates * 0.70)]
    test_date_boundary = unique_dates[int(n_dates * 0.85)]
    
    # Define Train/Val/Test mask based on dates
    # Purging overlap dates: we drop the last H dates from Train and Val boundaries
    train_dates = unique_dates[:int(n_dates * 0.70) - holding_period]
    val_dates = unique_dates[int(n_dates * 0.70):int(n_dates * 0.85) - holding_period]
    test_dates = unique_dates[int(n_dates * 0.85):]
    
    df_global_train = df[df['date'].isin(train_dates)]
    df_global_val = df[df['date'].isin(val_dates)]
    
    print(f"Global Train set size: {len(df_global_train)}")
    print(f"Global Val set size: {len(df_global_val)}")
    
    # Train global BUY model
    print("Training Global BUY model...")
    global_buy_model = train_xgb_model(
        df_global_train[FEATURE_COLS], df_global_train['target_buy'],
        df_global_val[FEATURE_COLS], df_global_val['target_buy'],
        model_name="global_buy"
    )
    global_buy_model.save_model(str(MODELS_DIR / "global_buy.json"))
    
    # Train global SELL model
    print("Training Global SELL model...")
    global_sell_model = train_xgb_model(
        df_global_train[FEATURE_COLS], df_global_train['target_sell'],
        df_global_val[FEATURE_COLS], df_global_val['target_sell'],
        model_name="global_sell"
    )
    global_sell_model.save_model(str(MODELS_DIR / "global_sell.json"))
    
    print("Global models saved successfully.")
    
    print("\n--- Tuning Individual Tickers ---")
    conn = get_db_connection()
    cur = conn.cursor()
    
    # Fetch tickers that have active sentiment to tune
    cur.execute(f"SELECT DISTINCT ticker FROM {settings.mimir_schema}.mimir_sentiment_impacts WHERE ticker IS NOT NULL")
    tickers_with_sentiment = {row[0].strip().upper() for row in cur.fetchall()}
    
    ticker_groups = list(df.groupby('ticker'))
    total_tickers = len(ticker_groups)
    
    tuned_count = 0
    
    for i, (ticker, group) in enumerate(ticker_groups, 1):
        ticker_upper = ticker.upper()
        
        # If ticker has no sentiment news, write fallback default profile to database
        if ticker_upper not in tickers_with_sentiment:
            cur.execute(f"""
                INSERT INTO {settings.mimir_schema}.mimir_ticker_parameters (
                    ticker, optimal_rsi_buy, optimal_rsi_sell, optimal_sentiment, optimal_vol_ratio, optimal_hold_days, 
                    win_rate, avg_pnl, total_trades, optimal_prob_buy, optimal_prob_sell, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (ticker) DO UPDATE SET
                    optimal_rsi_buy = EXCLUDED.optimal_rsi_buy,
                    optimal_rsi_sell = EXCLUDED.optimal_rsi_sell,
                    optimal_sentiment = EXCLUDED.optimal_sentiment,
                    optimal_vol_ratio = EXCLUDED.optimal_vol_ratio,
                    optimal_hold_days = EXCLUDED.optimal_hold_days,
                    win_rate = EXCLUDED.win_rate,
                    avg_pnl = EXCLUDED.avg_pnl,
                    total_trades = EXCLUDED.total_trades,
                    optimal_prob_buy = EXCLUDED.optimal_prob_buy,
                    optimal_prob_sell = EXCLUDED.optimal_prob_sell,
                    updated_at = NOW();
            """, (ticker, 30.0, 65.0, 0.3, 1.0, holding_period, None, None, 0, 0.55, 0.55))
            continue
            
        print(f"[{i}/{total_tickers}] Tuning parameters for {ticker}...")
        
        df_train, df_val, df_test = split_chronological_with_purging(group, holding_period=holding_period)
        
        buy_model = global_buy_model
        sell_model = global_sell_model
        model_source = "Global Fallback"
        
        # If we have enough ticker-specific samples, train ticker-specific models
        if len(df_train) >= 150:
            try:
                ticker_buy = train_xgb_model(
                    df_train[FEATURE_COLS], df_train['target_buy'],
                    df_val[FEATURE_COLS], df_val['target_buy'],
                    model_name=f"{ticker}_buy"
                )
                ticker_buy.save_model(str(MODELS_DIR / f"{ticker.lower()}_buy.json"))
                buy_model = ticker_buy
                
                ticker_sell = train_xgb_model(
                    df_train[FEATURE_COLS], df_train['target_sell'],
                    df_val[FEATURE_COLS], df_val['target_sell'],
                    model_name=f"{ticker}_sell"
                )
                ticker_sell.save_model(str(MODELS_DIR / f"{ticker.lower()}_sell.json"))
                sell_model = ticker_sell
                
                model_source = "Individual Ticker XGBoost"
            except Exception as e:
                print(f"  -> Error training individual model for {ticker}, falling back to global: {e}")
                
        # Optimize probability thresholds on the ticker-specific validation set
        opt_buy_thresh, buy_win, buy_pnl, buy_trades = optimize_threshold(buy_model, df_val[FEATURE_COLS], df_val, side="buy")
        opt_sell_thresh, sell_win, sell_pnl, sell_trades = optimize_threshold(sell_model, df_val[FEATURE_COLS], df_val, side="sell")
        
        overall_trades = buy_trades + sell_trades
        overall_win_rate = (buy_win * buy_trades + sell_win * sell_trades) / overall_trades if overall_trades > 0 else 0.0
        overall_avg_pnl = (buy_pnl * buy_trades + sell_pnl * sell_trades) / overall_trades if overall_trades > 0 else 0.0
        
        print(f"  -> Model Source: {model_source}")
        print(f"  -> Optimal BUY Threshold: {opt_buy_thresh:.2f} (Trades: {buy_trades}, Est. Win Rate: {buy_win:.1f}%, Est. PnL: {buy_pnl:.2f}%)")
        print(f"  -> Optimal SELL Threshold: {opt_sell_thresh:.2f} (Trades: {sell_trades}, Est. Win Rate: {sell_win:.1f}%, Est. PnL: {sell_pnl:.2f}%)")
        
        # Save optimal parameters and validation metrics to db
        cur.execute(f"""
            INSERT INTO {settings.mimir_schema}.mimir_ticker_parameters (
                ticker, optimal_rsi_buy, optimal_rsi_sell, optimal_sentiment, optimal_vol_ratio, optimal_hold_days, 
                win_rate, avg_pnl, total_trades, optimal_prob_buy, optimal_prob_sell, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (ticker) DO UPDATE SET
                optimal_rsi_buy = EXCLUDED.optimal_rsi_buy,
                optimal_rsi_sell = EXCLUDED.optimal_rsi_sell,
                optimal_sentiment = EXCLUDED.optimal_sentiment,
                optimal_vol_ratio = EXCLUDED.optimal_vol_ratio,
                optimal_hold_days = EXCLUDED.optimal_hold_days,
                win_rate = EXCLUDED.win_rate,
                avg_pnl = EXCLUDED.avg_pnl,
                total_trades = EXCLUDED.total_trades,
                optimal_prob_buy = EXCLUDED.optimal_prob_buy,
                optimal_prob_sell = EXCLUDED.optimal_prob_sell,
                updated_at = NOW();
        """, (
            ticker, 
            30.0, 65.0, 0.3, 1.0, holding_period, 
            float(overall_win_rate) if overall_trades > 0 else None, 
            float(overall_avg_pnl) if overall_trades > 0 else None, 
            int(overall_trades), 
            float(opt_buy_thresh), 
            float(opt_sell_thresh)
        ))
        tuned_count += 1
        
    conn.commit()
    cur.close()
    conn.close()
    print(f"\n[OK] Tuning complete. Successfully trained models and parameters for {tuned_count} tickers.")

if __name__ == "__main__":
    train_and_tune_all()
