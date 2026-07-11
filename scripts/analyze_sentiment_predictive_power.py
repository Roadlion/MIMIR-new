# scripts/analyze_sentiment_predictive_power.py
import os
import sys
import numpy as np
import pandas as pd
from pathlib import Path
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
        SELECT ticker, date, close 
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
    
    # For predictive power, we fill missing sentiment with 0.0 (neutral / no news)
    df['sentiment'] = df['sentiment'].fillna(0.0)
    
    return df

def calculate_forward_returns_and_ic(df):
    """Calculates forward returns for multiple horizons and computes Information Coefficient (IC)."""
    print("\nCalculating forward returns and Information Coefficient...")
    
    horizons = [1, 3, 5, 10, 20]
    
    # Calculate forward returns per ticker
    # forward_return = (close_t+N - close_t) / close_t
    for h in horizons:
        df[f'fwd_ret_{h}'] = df.groupby('ticker')['close'].shift(-h) / df['close'] - 1.0
        
    df = df.dropna().copy()
    
    ic_results = []
    
    print("\n=== OVERALL INFORMATION COEFFICIENT (IC) ===")
    print("Measures the correlation between daily sentiment and subsequent returns.")
    print("| Horizon (Days) | Pearson IC | Spearman Rank IC | t-stat | p-value | Significant? |")
    print("|---|---|---|---|---|---|")
    
    overall_ic_summary = []
    
    for h in horizons:
        # Filter out rows with zero sentiment to see correlation on active news days
        active_news_df = df[df['sentiment'] != 0.0]
        if len(active_news_df) < 30:
            continue
            
        x_series = active_news_df['sentiment']
        y_series = active_news_df[f'fwd_ret_{h}']
        
        pearson_corr = x_series.corr(y_series, method='pearson')
        # Spearman is mathematically equivalent to Pearson correlation of ranks
        spearman_corr = x_series.rank().corr(y_series.rank(), method='pearson')
        
        # Calculate t-statistic
        n = len(x_series)
        t_stat = spearman_corr * np.sqrt((n - 2) / (1 - spearman_corr**2 + 1e-15))
        is_sig = "Yes" if abs(t_stat) > 1.96 else "No"
        
        print(f"| {h}d | {pearson_corr:.4f} | {spearman_corr:.4f} | {t_stat:.2f} | {is_sig} |")
        
        overall_ic_summary.append({
            'horizon': h,
            'pearson': pearson_corr,
            'spearman': spearman_corr,
            't_stat': t_stat,
            'sig': is_sig
        })
        
    # Calculate ticker-wise average IC (cross-sectional)
    ticker_ics = []
    for ticker, group in df.groupby('ticker'):
        active_group = group[group['sentiment'] != 0.0]
        if len(active_group) < 15:
            continue
            
        row = {'ticker': ticker}
        for h in horizons:
            x_series = active_group['sentiment']
            y_series = active_group[f'fwd_ret_{h}']
            corr = x_series.rank().corr(y_series.rank(), method='pearson')
            row[f'ic_{h}d'] = corr if not np.isnan(corr) else 0.0
        ticker_ics.append(row)
        
    df_ticker_ic = pd.DataFrame(ticker_ics)
    
    # Save output report
    output_file = Path(r"C:\Users\ACER\.gemini\antigravity\brain\9844ec74-49a0-4927-98b3-dcb5796bd544") / "sentiment_predictive_power.md"
    with open(output_file, 'w') as f:
        f.write("# Sentiment Score Predictive Power Analysis\n\n")
        f.write("This report evaluates the statistical predictive power of MIMIR sentiment scores using the **Information Coefficient (IC)** metric. ")
        f.write("IC measures the correlation between today's average sentiment score and forward asset returns.\n\n")
        
        f.write("## Overall Information Coefficient (Active News Days)\n\n")
        f.write("| Horizon (Days) | Pearson Correlation | Spearman Rank Correlation | t-statistic | Significant (p < 0.05)? |\n")
        f.write("|---|---|---|---|---|\n")
        for r in overall_ic_summary:
            f.write(f"| {r['horizon']}d | {r['pearson']:.4f} | {r['spearman']:.4f} | {r['t_stat']:.2f} | {r['sig']} |\n")
            
        f.write("\n\n## Top 15 Tickers with Strongest Sentiment Predictive Power\n\n")
        f.write("Displays tickers where the sentiment score has the highest positive correlation with 5-day forward returns:\n\n")
        
        if not df_ticker_ic.empty and 'ic_5d' in df_ticker_ic.columns:
            df_top = df_ticker_ic.sort_values(by='ic_5d', ascending=False).head(15)
            f.write("| Ticker | 1d IC | 3d IC | 5d IC | 10d IC | 20d IC |\n")
            f.write("|---|---|---|---|---|---|\n")
            for _, r in df_top.iterrows():
                f.write(f"| **{r['ticker']}** | {r['ic_1d']:.4f} | {r['ic_3d']:.4f} | {r['ic_5d']:.4f} | {r['ic_10d']:.4f} | {r['ic_20d']:.4f} |\n")
        else:
            f.write("Not enough ticker-level data to construct statistics.\n")
            
        f.write("\n\n### Methodology Notes:\n")
        f.write("1. **Information Coefficient (IC)**: A standard quantitative finance metric. An IC above **0.05** is considered highly predictive for a trading alpha factor.\n")
        f.write("2. **Active News Days**: Calculated only on days when news articles actually triggered a sentiment score update for the asset.\n")
        f.write("3. **Spearman Rank Correlation**: Non-parametric correlation, robust to outliers and leverage points in asset price distributions.\n")
        
    print(f"\nSaved analysis results to {output_file}")

def main():
    df = load_data()
    calculate_forward_returns_and_ic(df)

if __name__ == "__main__":
    main()
