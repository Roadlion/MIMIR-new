# scripts/tune_ticker_parameters.py
import os
import sys
import numpy as np
import pandas as pd
from pathlib import Path
from datetime import datetime
import xgboost as xgb
import json
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
            ADD COLUMN IF NOT EXISTS optimal_prob_sell NUMERIC DEFAULT 0.55,
            ADD COLUMN IF NOT EXISTS selected_features_buy TEXT,
            ADD COLUMN IF NOT EXISTS selected_features_sell TEXT;
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
        SELECT ticker, date, AVG(sentiment_score) as sentiment, COUNT(article_id) as sent_vol
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
    df['sent_vol'] = df['sent_vol'].fillna(0.0)
    
    # Merge fundamentals
    df = pd.merge(df, df_fund, on=['ticker'], how='left')
    
    # Convert numeric columns
    for col in ['open', 'high', 'low', 'close', 'volume', 'pe_ratio', 'debt_to_equity', 'eps_growth', 'operating_margin']:
        df[col] = pd.to_numeric(df[col], errors='coerce')
        
    return df

def calculate_features_and_targets(df, holding_period=5, slippage_bps=5.0, dc_theta=0.015):
    """Calculates lagging technical, sentiment, and fundamental features.
    Also defines DC Event targets free of lookahead bias.
    """
    print(f"Calculating technical indicators and extracting DC events (theta={dc_theta})...")
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
    
    # Lagged news sentiment rolling averages and CARVS
    df['sentiment_1d'] = df['sentiment']
    df['sentiment_3d'] = df.groupby('ticker')['sentiment'].transform(lambda x: x.rolling(3, min_periods=1).mean())
    df['sentiment_5d'] = df.groupby('ticker')['sentiment'].transform(lambda x: x.rolling(5, min_periods=1).mean())
    
    df['sent_vol_1d'] = df['sent_vol']
    df['sent_vol_30d'] = df.groupby('ticker')['sent_vol'].transform(lambda x: x.rolling(30, min_periods=1).mean())
    df['relative_volume'] = df['sent_vol_1d'] / (df['sent_vol_30d'] + 1e-5)
    
    df['carvs_1d'] = df['sentiment_1d'] * df['relative_volume']
    df['carvs_3d'] = df['sentiment_3d'] * df['relative_volume']
    df['carvs_5d'] = df['sentiment_5d'] * df['relative_volume']
    
    # Momentum & Volatility
    df['price_momentum_5d'] = df.groupby('ticker')['close'].transform(lambda x: x.pct_change(5))
    df['price_momentum_10d'] = df.groupby('ticker')['close'].transform(lambda x: x.pct_change(10))
    df['pct_change'] = df.groupby('ticker')['close'].transform(lambda x: x.pct_change())
    df['volatility_20d'] = df.groupby('ticker')['pct_change'].transform(lambda x: x.rolling(20).std())
    df.drop(columns=['pct_change'], inplace=True)
    
    # New Technicals
    df['ma20'] = df.groupby('ticker')['close'].transform(lambda x: x.rolling(20).mean())
    df['ma50'] = df.groupby('ticker')['close'].transform(lambda x: x.rolling(50).mean())
    df['ma20_ma50_ratio'] = df['ma20'] / (df['ma50'] + 1e-15)
    
    def get_macd(series):
        ema12 = series.ewm(span=12, adjust=False).mean()
        ema26 = series.ewm(span=26, adjust=False).mean()
        macd_line = ema12 - ema26
        macd_signal = macd_line.ewm(span=9, adjust=False).mean()
        macd_hist = macd_line - macd_signal
        return pd.DataFrame({'macd': macd_line, 'macd_signal': macd_signal, 'macd_hist': macd_hist})

    macd_dfs = []
    for ticker, group in df.groupby('ticker'):
        macd_res = get_macd(group['close'])
        macd_res.index = group.index
        macd_dfs.append(macd_res)
    if macd_dfs:
        macd_combined = pd.concat(macd_dfs)
        df['macd'] = macd_combined['macd']
        df['macd_signal'] = macd_combined['macd_signal']
        df['macd_hist'] = macd_combined['macd_hist']
    else:
        df['macd'] = np.nan
        df['macd_signal'] = np.nan
        df['macd_hist'] = np.nan

    df['bb_std'] = df.groupby('ticker')['close'].transform(lambda x: x.rolling(20).std())
    df['bb_upper'] = df['ma20'] + (df['bb_std'] * 2)
    df['bb_lower'] = df['ma20'] - (df['bb_std'] * 2)
    df['bb_width'] = (df['bb_upper'] - df['bb_lower']) / (df['ma20'] + 1e-15)
    
    def get_obv(group):
        close_diff = group['close'].diff()
        direction = np.where(close_diff > 0, 1, np.where(close_diff < 0, -1, 0))
        obv = (direction * group['volume']).cumsum()
        return pd.Series(obv, index=group.index)
        
    df['obv'] = df.groupby('ticker', group_keys=False).apply(get_obv)
    
    df['ichimoku_tenkan'] = df.groupby('ticker', group_keys=False).apply(lambda x: (x['high'].rolling(9).max() + x['low'].rolling(9).min()) / 2)
    df['ichimoku_kijun'] = df.groupby('ticker', group_keys=False).apply(lambda x: (x['high'].rolling(26).max() + x['low'].rolling(26).min()) / 2)
    df['ichimoku_senkou_a'] = ((df['ichimoku_tenkan'] + df['ichimoku_kijun']) / 2).groupby(df['ticker']).shift(26)
    df['ichimoku_senkou_b'] = df.groupby('ticker', group_keys=False).apply(lambda x: (x['high'].rolling(52).max() + x['low'].rolling(52).min()) / 2).groupby(df['ticker']).shift(26)
    
    df.drop(columns=['ma20', 'ma50', 'bb_std'], inplace=True)

    
    # Directional Change (DC) Event Extraction
    def extract_dc_events_for_ticker(group, theta=0.015):
        events = []
        mode = 'up' # Assume starting in up mode
        extreme = group['close'].iloc[0]
        extreme_idx = group.index[0]
        
        for idx, row in group.iterrows():
            price = row['close']
            if mode == 'up':
                if price > extreme:
                    extreme = price
                    extreme_idx = idx
                elif price <= extreme * (1 - theta):
                    # Downward DC triggered
                    # The overshoot for the PREVIOUS Upward DC is the distance to this extreme
                    events.append({'trigger_idx': extreme_idx, 'dc_type': 'down_dc', 'trigger_price': extreme, 'end_price': price})
                    mode = 'down'
                    extreme = price
                    extreme_idx = idx
            else:
                if price < extreme:
                    extreme = price
                    extreme_idx = idx
                elif price >= extreme * (1 + theta):
                    # Upward DC triggered
                    events.append({'trigger_idx': extreme_idx, 'dc_type': 'up_dc', 'trigger_price': extreme, 'end_price': price})
                    mode = 'up'
                    extreme = price
                    extreme_idx = idx
                    
        # Map events back to dataframe
        group['target_overshoot'] = np.nan
        group['is_dc_event'] = 0
        for ev in events:
            # We predict the overshoot magnitude from the trigger price
            group.loc[ev['trigger_idx'], 'is_dc_event'] = 1 if ev['dc_type'] == 'up_dc' else -1
            group.loc[ev['trigger_idx'], 'target_overshoot'] = (ev['end_price'] - ev['trigger_price']) / ev['trigger_price']
            
        return group

    # Apply DC extraction dynamically per ticker
    df = df.groupby('ticker', group_keys=False).apply(lambda x: extract_dc_events_for_ticker(x, theta=dc_theta))
    
    # We only want to train on rows where a DC event occurred (eliminating time noise)
    df = df[df['is_dc_event'] != 0].copy()
    
    df['target_buy'] = df['target_overshoot'] # We'll rename this to maintain compatibility with the trainer loop for now
    df['target_sell'] = -df['target_overshoot']
    
    # Drop NaNs
    df = df.dropna(subset=['rsi', 'support', 'resistance', 'volatility_20d', 'target_overshoot']).copy()
    
    return df

FEATURE_COLS = [
    'rsi', 'volume_ratio', 'close_to_support', 'close_to_resistance',
    'sentiment_1d', 'sentiment_3d', 'sentiment_5d',
    'relative_volume', 'carvs_1d', 'carvs_3d', 'carvs_5d',
    'price_momentum_5d', 'price_momentum_10d', 'volatility_20d',
    'pe_ratio', 'debt_to_equity', 'eps_growth', 'operating_margin',
    'ma20_ma50_ratio', 'macd', 'macd_signal', 'macd_hist',
    'bb_upper', 'bb_lower', 'bb_width', 'obv',
    'ichimoku_tenkan', 'ichimoku_kijun', 'ichimoku_senkou_a', 'ichimoku_senkou_b'
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

def train_xgb_model(X_train, y_train, X_val, y_val, model_name="model", fast=False):
    """Trains a regularized XGBoost model, tuning depth and estimators."""
    best_model = None
    best_score = -1.0
    
    # Micro grid search for depth & trees to prevent overfitting and stay fast
    if fast:
        depths = [3]
        n_est = [50]
    else:
        depths = [3, 4]
        n_est = [50, 100]
    
    for d in depths:
        for n in n_est:
            # XGBRegressor for predicting overshoot magnitude
            model = xgb.XGBRegressor(
                max_depth=d,
                n_estimators=n,
                learning_rate=0.05,
                reg_alpha=0.2,       # L1 regularization
                reg_lambda=1.5,      # L2 regularization
                subsample=0.8,
                colsample_bytree=0.8,
                random_state=42,
                n_jobs=-1,
                eval_metric="rmse"
            )
            model.fit(X_train, y_train)
            
            # Evaluate using MSE on validation
            preds = model.predict(X_val)
            score = -np.mean((y_val - preds)**2) # Negative MSE so higher is better
            
            if score > best_score:
                best_score = score
                best_model = model
                
    if best_model is None:
        # Fallback default model
        best_model = xgb.XGBRegressor(
            max_depth=3,
            n_estimators=50,
            learning_rate=0.05,
            n_jobs=-1,
            eval_metric="rmse"
        )
        best_model.fit(X_train, y_train)
        
    return best_model

def recursive_feature_elimination(df_train, df_val, target_col, side="buy"):
    """Iteratively removes the least important features to maximize validation win rate & PnL."""
    current_features = list(FEATURE_COLS)
    best_overall_score = -float('inf')
    best_overall_features = list(current_features)
    best_overall_metrics = (0.55, 0.0, 0.0, 0)
    
    # Baseline check
    model = train_xgb_model(
        df_train[current_features], df_train[target_col],
        df_val[current_features], df_val[target_col],
        fast=True
    )
    opt_thresh, win_rate, pnl, trades = optimize_threshold(model, df_val[current_features], df_val, side=side)
    if trades == 0:
        return model, current_features, (opt_thresh, win_rate, pnl, trades)
        
    while len(current_features) >= 5: # Keep at least 5 features
        # Train current model
        model = train_xgb_model(
            df_train[current_features], df_train[target_col],
            df_val[current_features], df_val[target_col],
            fast=True
        )
        
        # Optimize threshold & get metrics
        opt_thresh, win_rate, pnl, trades = optimize_threshold(model, df_val[current_features], df_val, side=side)
        
        # Scoring function: PnL is most important, then win rate
        score = pnl * trades if trades > 0 else -999999.0
        
        if score > best_overall_score:
            best_overall_score = score
            best_overall_features = list(current_features)
            best_overall_metrics = (opt_thresh, win_rate, pnl, trades)
            
        # Get feature importances to drop the least important one
        importances = model.feature_importances_
        if len(importances) == 0:
            break
            
        # Drop the 3 least important features, or fewer if close to limit
        drop_count = min(3, len(current_features) - 4)
        if drop_count <= 0:
            break
            
        least_important_indices = np.argsort(importances)[:drop_count]
        features_to_drop = [current_features[i] for i in least_important_indices]
        for f in features_to_drop:
            current_features.remove(f)
            
    # Retrain final model with full grid search on best features
    final_model = train_xgb_model(
        df_train[best_overall_features], df_train[target_col],
        df_val[best_overall_features], df_val[target_col],
        fast=False
    )
    final_metrics = optimize_threshold(final_model, df_val[best_overall_features], df_val, side=side)
        
    return final_model, best_overall_features, final_metrics

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
                    win_rate, avg_pnl, total_trades, optimal_prob_buy, optimal_prob_sell, selected_features_buy, selected_features_sell, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
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
                    selected_features_buy = EXCLUDED.selected_features_buy,
                    selected_features_sell = EXCLUDED.selected_features_sell,
                    updated_at = NOW();
            """, (ticker, 30.0, 65.0, 0.3, 1.0, holding_period, None, None, 0, 0.55, 0.55, json.dumps(list(FEATURE_COLS)), json.dumps(list(FEATURE_COLS))))
            continue
            
        print(f"[{i}/{total_tickers}] Tuning parameters for {ticker}...")
        
        df_train, df_val, df_test = split_chronological_with_purging(group, holding_period=holding_period)
        
        buy_model = global_buy_model
        sell_model = global_sell_model
        model_source = "Global Fallback"
        
        buy_features_list = list(FEATURE_COLS)
        sell_features_list = list(FEATURE_COLS)
        
        # If we have enough ticker-specific samples, train ticker-specific models
        if len(df_train) >= 150:
            try:
                ticker_buy, buy_features_list, buy_metrics = recursive_feature_elimination(df_train, df_val, 'target_buy', side="buy")
                ticker_buy.save_model(str(MODELS_DIR / f"{ticker.lower()}_buy.json"))
                buy_model = ticker_buy
                
                ticker_sell, sell_features_list, sell_metrics = recursive_feature_elimination(df_train, df_val, 'target_sell', side="sell")
                ticker_sell.save_model(str(MODELS_DIR / f"{ticker.lower()}_sell.json"))
                sell_model = ticker_sell
                
                model_source = "Individual Ticker XGBoost RFE"
                opt_buy_thresh, buy_win, buy_pnl, buy_trades = buy_metrics
                opt_sell_thresh, sell_win, sell_pnl, sell_trades = sell_metrics
            except Exception as e:
                print(f"  -> Error training individual model for {ticker}, falling back to global: {e}")
                buy_model = global_buy_model
                sell_model = global_sell_model
                buy_features_list = list(FEATURE_COLS)
                sell_features_list = list(FEATURE_COLS)
                opt_buy_thresh, buy_win, buy_pnl, buy_trades = optimize_threshold(buy_model, df_val[FEATURE_COLS], df_val, side="buy")
                opt_sell_thresh, sell_win, sell_pnl, sell_trades = optimize_threshold(sell_model, df_val[FEATURE_COLS], df_val, side="sell")
        else:
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
                win_rate, avg_pnl, total_trades, optimal_prob_buy, optimal_prob_sell, selected_features_buy, selected_features_sell, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
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
                selected_features_buy = EXCLUDED.selected_features_buy,
                selected_features_sell = EXCLUDED.selected_features_sell,
                updated_at = NOW();
        """, (
            ticker, 
            30.0, 65.0, 0.3, 1.0, holding_period, 
            float(overall_win_rate) if overall_trades > 0 else None, 
            float(overall_avg_pnl) if overall_trades > 0 else None, 
            int(overall_trades), 
            float(opt_buy_thresh), 
            float(opt_sell_thresh),
            json.dumps(buy_features_list),
            json.dumps(sell_features_list)
        ))
        conn.commit()
        tuned_count += 1
        
    cur.close()
    conn.close()
    print(f"\n[OK] Tuning complete. Successfully trained models and parameters for {tuned_count} tickers.")

if __name__ == "__main__":
    train_and_tune_all()
