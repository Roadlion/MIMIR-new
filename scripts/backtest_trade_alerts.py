# scripts/backtest_trade_alerts.py
import os
import sys
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from pathlib import Path

# Adjust path to import backend
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.database import get_db_connection
from backend.app.config import get_settings

settings = get_settings()

def load_data():
    """Loads and merges historical price and sentiment data from database."""
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
    
    # 2. Load daily average sentiment impacts
    sent_sql = f"""
        SELECT si.ticker, 
               (a.published_ts AT TIME ZONE 'UTC')::date as date,
               AVG(si.sentiment_score) as sentiment
        FROM {settings.mimir_schema}.mimir_sentiment_impacts si
        JOIN {settings.mimir_schema}.mimir_raw_articles a ON si.article_id = a.id
        WHERE a.published_ts >= '2025-01-01' AND si.ticker IS NOT NULL
        GROUP BY si.ticker, (a.published_ts AT TIME ZONE 'UTC')::date
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
    print("Calculating technical indicators...")
    
    # Calculate RSI
    def get_rsi(series, window=14):
        delta = series.diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        
        avg_gain = gain.rolling(window=window, min_periods=window).mean()
        avg_loss = loss.rolling(window=window, min_periods=window).mean()
        
        # Wilders smoothing logic or simple MA
        rs = avg_gain / (avg_loss + 1e-15)
        return 100 - (100 / (1 + rs))

    df['rsi'] = df.groupby('ticker')['close'].transform(lambda x: get_rsi(x))
    
    # Calculate Support / Resistance (rolling 20-day min/max)
    df['support'] = df.groupby('ticker')['low'].transform(lambda x: x.rolling(20).min())
    df['resistance'] = df.groupby('ticker')['high'].transform(lambda x: x.rolling(20).max())
    
    # Calculate Volume Ratio
    df['volume_ma'] = df.groupby('ticker')['volume'].transform(lambda x: x.rolling(20).mean())
    df['volume_ratio'] = df['volume'] / (df['volume_ma'] + 1e-15)
    
    # Drop rows that don't have enough data for indicators
    df = df.dropna(subset=['rsi', 'support', 'resistance', 'volume_ma']).copy()
    return df

def run_backtest(df, sent_thresh, rsi_buy, rsi_sell, vol_ratio_thresh, holding_period=5, slippage_bps=5.0):
    """Simulates trading strategy and gathers results."""
    # Define trigger conditions
    # BUY: sentiment >= sent_thresh AND rsi <= rsi_buy AND close <= support * 1.02 AND volume_ratio >= vol_ratio_thresh
    df['buy_signal'] = (
        (df['sentiment'] >= sent_thresh) & 
        (df['rsi'] <= rsi_buy) & 
        (df['close'] <= df['support'] * 1.02) & 
        (df['volume_ratio'] >= vol_ratio_thresh)
    )
    
    # SELL: sentiment <= -sent_thresh AND rsi >= rsi_sell AND close >= resistance * 0.98 AND volume_ratio >= vol_ratio_thresh
    df['sell_signal'] = (
        (df['sentiment'] <= -sent_thresh) & 
        (df['rsi'] >= rsi_sell) & 
        (df['close'] >= df['resistance'] * 0.98) & 
        (df['volume_ratio'] >= vol_ratio_thresh)
    )
    
    trades = []
    
    # For each ticker, simulate sequential trades
    for ticker, group in df.groupby('ticker'):
        group = group.sort_values('date').reset_index(drop=True)
        buy_signals = group['buy_signal'].values
        sell_signals = group['sell_signal'].values
        opens = group['open'].values
        closes = group['close'].values
        dates = group['date'].values
        
        in_trade = False
        entry_price = 0.0
        entry_date = None
        days_held = 0
        n_rows = len(group)
        
        for idx in range(n_rows):
            if not in_trade:
                if buy_signals[idx]:
                    # Enter trade at next day's open to avoid lookahead bias
                    if idx + 1 < n_rows:
                        entry_price = float(opens[idx + 1])
                        entry_date = dates[idx + 1]
                        in_trade = True
                        days_held = 0
            else:
                days_held += 1
                # Exit condition: holding period reached OR sell signal triggered
                should_exit = (days_held >= holding_period) or sell_signals[idx]
                if should_exit:
                    exit_price = float(closes[idx])
                    exit_date = dates[idx]
                    
                    # Compute PnL % (deduct slippage for both entry and exit: 2 * slippage_bps / 10000)
                    pnl = ((exit_price - entry_price) / entry_price) - (2.0 * slippage_bps / 10000.0)
                    pnl_pct = pnl * 100.0
                    
                    trades.append({
                        'ticker': ticker,
                        'entry_date': entry_date,
                        'exit_date': exit_date,
                        'entry_price': entry_price,
                        'exit_price': exit_price,
                        'pnl_pct': pnl_pct
                    })
                    in_trade = False
                    
    if not trades:
        return {
            'total_trades': 0,
            'win_rate': 0.0,
            'avg_pnl': 0.0,
            'successful_trades': 0,
            'failed_trades': 0
        }
        
    df_trades = pd.DataFrame(trades)
    total_trades = len(df_trades)
    successful_trades = len(df_trades[df_trades['pnl_pct'] > 0])
    failed_trades = total_trades - successful_trades
    win_rate = (successful_trades / total_trades) * 100.0
    avg_pnl = df_trades['pnl_pct'].mean()
    
    return {
        'total_trades': total_trades,
        'win_rate': win_rate,
        'avg_pnl': avg_pnl,
        'successful_trades': successful_trades,
        'failed_trades': failed_trades
    }

def main():
    df = load_data()
    df = calculate_indicators(df)
    
    print("\n--- Running Grid Search Backtest ---")
    results = []
    
    # Parameters to test
    sent_thresholds = [0.1, 0.2, 0.3]
    rsi_buy_thresholds = [30, 35, 40, 45]
    rsi_sell_threshold = 65
    volume_ratio_thresholds = [1.0, 1.3]
    holding_periods = [3, 5, 10]
    
    for sent in sent_thresholds:
        for rsi in rsi_buy_thresholds:
            for vol in volume_ratio_thresholds:
                for hold in holding_periods:
                    res = run_backtest(df, sent, rsi, rsi_sell_threshold, vol, holding_period=hold)
                    if res['total_trades'] > 0:
                        results.append({
                            'sentiment': sent,
                            'rsi_buy': rsi,
                            'vol_ratio': vol,
                            'hold_days': hold,
                            'total_trades': res['total_trades'],
                            'win_rate': res['win_rate'],
                            'avg_pnl': res['avg_pnl']
                        })
                        print(f"Sent: {sent} | RSI Buy: {rsi} | Vol Ratio: {vol} | Hold: {hold}d | Trades: {res['total_trades']} | Win Rate: {res['win_rate']:.1f}% | Avg PnL: {res['avg_pnl']:.2f}%")
                        
    # Convert to df and sort
    if results:
        df_res = pd.DataFrame(results)
        df_res = df_res.sort_values(by='avg_pnl', ascending=False)
        
        def df_to_md(df):
            headers = list(df.columns)
            lines = []
            lines.append("| " + " | ".join(headers) + " |")
            lines.append("| " + " | ".join(["---"] * len(headers)) + " |")
            for _, row in df.iterrows():
                row_str = []
                for h in headers:
                    val = row[h]
                    if isinstance(val, float):
                        row_str.append(f"{val:.3f}")
                    else:
                        row_str.append(str(val))
                lines.append("| " + " | ".join(row_str) + " |")
            return "\n".join(lines)
            
        print("\n=== TOP 10 PARAMETER CONFIGURATIONS ===")
        print(df_res.head(10).to_string(index=False))
        
        # Save results to markdown file in artifact directory
        output_file = Path(r"C:\Users\ACER\.gemini\antigravity\brain\9844ec74-49a0-4927-98b3-dcb5796bd544") / "backtest_results.md"
        with open(output_file, 'w') as f:
            f.write("# Trade Alert Strategy Backtest Results\n\n")
            f.write(f"Backtested over price and sentiment data from **2025-01-01** to **{datetime.now().date()}**.\n\n")
            f.write("### Grid Search Parameter Performance\n\n")
            f.write(df_to_md(df_res.head(20)))
            f.write("\n\n*All trades executed at next day's open price to prevent lookahead bias. Slippage of 5 bps per trade deducted.*")
        print(f"\nSaved results to {output_file}")
    else:
        print("No trades generated under any parameter configuration.")

if __name__ == "__main__":
    main()
