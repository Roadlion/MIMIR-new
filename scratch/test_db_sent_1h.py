# scratch/test_db_sent_1h.py
import sys, os
sys.path.insert(0, os.path.abspath("."))
from backend.app.database import get_db_connection
from backend.app.config import get_settings
from backend.app.analytics.strategy_backtester import load_candles
import pandas as pd
import numpy as np
from bong_strats.strategies.gold.gold_news_sentiment import GoldNewsSentimentStrategy

settings = get_settings()
conn = get_db_connection()

sql = f"""
    SELECT (created_at AT TIME ZONE 'UTC') as ts, sentiment_score
    FROM {settings.mimir_schema}.mimir_sentiment_impacts
    WHERE ticker IN ('GLD', 'XAUUSD', 'GOLD', 'GDX')
    ORDER BY ts ASC;
"""
sent_df = pd.read_sql(sql, conn)
sent_df['ts'] = pd.to_datetime(sent_df['ts'], utc=True).astype('datetime64[ns, UTC]')

df_cand = load_candles('XAUUSD', timeframe='1h', start_date='2026-06-29', end_date='2026-10-30')
df_cand['ts'] = pd.to_datetime(df_cand['timestamp'], utc=True).astype('datetime64[ns, UTC]')

# Merge nearest backward sentiment
df = pd.merge_asof(df_cand.sort_values('ts'), sent_df.sort_values('ts'), on='ts', direction='backward')
df['sentiment_score'] = df['sentiment_score'].fillna(0.0)

print("=" * 60)
print("REAL DATABASE SENTIMENT TEST ON 1H GOLD (XAUUSD)")
print("Window: June 29, 2026 to October 30, 2026 (4 Months)")
print("=" * 60)
print(f"Candles: {len(df)} | Non-zero sentiment bars: {(df['sentiment_score'] != 0).sum()}")

for label, params in [
    ("Asymmetric (New Defaults: Long TP 4.5/SL 2.0 vs Short TP 2.2/SL 1.2)", {}),
    ("Symmetric (Old Defaults: TP 4.0 / SL 2.0 for both)", {"long_tp_atr_mult": 4.0, "short_tp_atr_mult": 4.0, "long_sl_atr_mult": 2.0, "short_sl_atr_mult": 2.0})
]:
    strat = GoldNewsSentimentStrategy(**params)
    signals = strat.generate_signals(df)
    
    positions = signals.fillna(0.0).reset_index(drop=True)
    close_vals = df['close'].astype(float).reset_index(drop=True)
    mkt_returns = close_vals.pct_change().fillna(0.0)
    strat_returns = positions.shift(1).fillna(0.0) * mkt_returns
    
    equity = (1.0 + strat_returns).cumprod()
    periods_per_year = 24 * 252
    ann_ret = (equity.iloc[-1] ** (periods_per_year / len(df)) - 1.0) * 100
    hourly_sharpe = (strat_returns.mean() / (strat_returns.std() + 1e-9)) * np.sqrt(periods_per_year)
    dd = (equity / equity.cummax() - 1.0).min() * 100
    
    pos_diff = positions.diff().fillna(positions)
    trades = (pos_diff != 0).sum()
    
    print(f"\n[{label}]")
    print(f"  • Annualized Return: {ann_ret:+.2f}%")
    print(f"  • Sharpe Ratio     : {hourly_sharpe:.2f}")
    print(f"  • Max Drawdown     : {dd:.2f}%")
    print(f"  • Signal Flips     : {trades}")
