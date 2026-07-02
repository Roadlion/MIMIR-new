# backend/app/analytics/backtester.py
import numpy as np
import pandas as pd
from datetime import datetime, date
from typing import Dict, List, Any, Optional

from ..database import get_db_connection
from ..config import get_settings
from .expression_parser import FormulaParser

settings = get_settings()

def resolve_ticker_market(ticker: str) -> str:
    ticker = ticker.strip().upper()
    if '-' in ticker and ticker.endswith('-USD'):
        return 'crypto'
    if '=' in ticker:
        if ticker.endswith('=X'):
            return 'forex'
        if ticker.endswith('=F'):
            return 'commodity'
    if '.' in ticker:
        suffix = ticker.split('.')[-1]
        if suffix in ('SS', 'SZ'):
            return 'china'
        elif suffix == 'KS':
            return 'korea'
        elif suffix == 'BK':
            return 'thailand'
        elif suffix == 'T':
            return 'japan'
        elif suffix in ('MI', 'DE', 'PA', 'L', 'F', 'VI', 'AS', 'MC'):
            return 'europe'
    return 'us'

class BacktestEngine:
    def __init__(self, start_date: str, end_date: str, universe: str = 'core', markets: Optional[List[str]] = None):
        self.start_date = pd.to_datetime(start_date).date()
        self.end_date = pd.to_datetime(end_date).date()
        self.universe = universe
        self.markets = markets
        self.dfs = {}
        
    def load_data(self) -> None:
        """Loads price and sentiment data from database, aligning them into pivoted dataframes."""
        conn = get_db_connection()
        cur = conn.cursor()
        
        # 1. Fetch Universe Ticker List
        if self.universe == 'core':
            from ..routers.prices import DEFAULT_TICKERS
            # Clean list
            tickers = [t.strip().lstrip('$').upper() for t in DEFAULT_TICKERS if t]
            tickers = [t for t in tickers if not (t.startswith('0P') or '.F' in t or t.endswith('.F'))]
        else:
            # Load all tickers that exist in dynamic tickers
            cur.execute(f"SELECT DISTINCT ticker FROM {settings.mimir_schema}.mimir_dynamic_tickers WHERE ticker IS NOT NULL")
            tickers = [row[0].strip().upper() for row in cur.fetchall()]
            
        if self.markets:
            allowed_markets = [m.lower() for m in self.markets]
            tickers = [t for t in tickers if resolve_ticker_market(t) in allowed_markets]

        if not tickers:
            raise ValueError("No tickers found matching the selected stock markets filter.")
            
        tickers = tuple(set(tickers))

        # 2. Fetch daily OHLCV prices from view
        price_sql = f"""
            SELECT ticker, date, open, high, low, close, volume 
            FROM {settings.mimir_schema}.v_mimir_daily_ohlcv
            WHERE ticker IN %s AND date >= %s AND date <= %s
            ORDER BY date ASC
        """
        cur.execute(price_sql, (tickers, self.start_date, self.end_date))
        price_rows = cur.fetchall()
        
        # 3. Fetch direct daily sentiment
        sentiment_sql = f"""
            SELECT si.ticker, a.published_ts::date AS date, AVG(si.sentiment_score) AS sentiment_score
            FROM {settings.mimir_schema}.mimir_sentiment_impacts si
            JOIN {settings.mimir_schema}.mimir_raw_articles a ON si.article_id = a.id
            WHERE si.ticker IN %s AND a.published_ts::date >= %s AND a.published_ts::date <= %s
            GROUP BY si.ticker, a.published_ts::date
        """
        cur.execute(sentiment_sql, (tickers, self.start_date, self.end_date))
        sentiment_rows = cur.fetchall()

        # 4. Fetch daily social chatter
        social_sql = f"""
            SELECT ticker, bucket_ts::date AS date, AVG(sentiment_score) AS sentiment_score
            FROM {settings.mimir_schema}.mimir_social_chatter
            WHERE ticker IN %s AND bucket_ts::date >= %s AND bucket_ts::date <= %s
            GROUP BY ticker, bucket_ts::date
        """
        cur.execute(social_sql, (tickers, self.start_date, self.end_date))
        social_rows = cur.fetchall()

        cur.close()
        conn.close()

        # Convert to DataFrames and Pivot
        df_prices = pd.DataFrame(price_rows, columns=['ticker', 'date', 'open', 'high', 'low', 'close', 'volume'])
        df_sent = pd.DataFrame(sentiment_rows, columns=['ticker', 'date', 'sentiment'])
        df_social = pd.DataFrame(social_rows, columns=['ticker', 'date', 'social_chatter'])

        # Cast decimals/numeric values to float for compatibility with numpy calculations
        if not df_prices.empty:
            for col in ['open', 'high', 'low', 'close']:
                df_prices[col] = df_prices[col].astype(float)
        if not df_sent.empty:
            df_sent['sentiment'] = df_sent['sentiment'].astype(float)
        if not df_social.empty:
            df_social['social_chatter'] = df_social['social_chatter'].astype(float)

        if df_prices.empty:
            raise ValueError(f"No price data available between {self.start_date} and {self.end_date}.")

        # Align Dates (index) and Tickers (columns)
        # Create full combinations of dates and tickers to prevent shape mismatches
        all_dates = pd.to_datetime(df_prices['date'].unique()).sort_values()
        all_tickers = sorted(df_prices['ticker'].unique())
        
        # Pivot helpers
        def pivot_and_align(df, val_col, fill_method=None, fill_val=0.0):
            if df.empty:
                return pd.DataFrame(fill_val, index=all_dates, columns=all_tickers)
            df['date'] = pd.to_datetime(df['date'])
            pivoted = df.pivot(index='date', columns='ticker', values=val_col)
            # Reindex to match all dates and tickers
            pivoted = pivoted.reindex(index=all_dates, columns=all_tickers)
            if fill_method == 'ffill':
                # Use ffill and bfill to prevent 0.0 price points
                pivoted = pivoted.ffill().bfill().fillna(fill_val)
            elif fill_method == 'sentiment_decay':
                # Sentiment decays over 3 days, then defaults to 0
                pivoted = pivoted.ffill(limit=3).fillna(0.0)
            else:
                pivoted = pivoted.fillna(fill_val)
            return pivoted

        self.dfs['open'] = pivot_and_align(df_prices, 'open', 'ffill')
        self.dfs['high'] = pivot_and_align(df_prices, 'high', 'ffill')
        self.dfs['low'] = pivot_and_align(df_prices, 'low', 'ffill')
        self.dfs['close'] = pivot_and_align(df_prices, 'close', 'ffill')
        self.dfs['volume'] = pivot_and_align(df_prices, 'volume', 'ffill')
        
        # Precompute typical WorldQuant variables
        self.dfs['vwap'] = (self.dfs['high'] + self.dfs['low'] + self.dfs['close']) / 3.0
        self.dfs['returns'] = self.dfs['close'].pct_change().fillna(0.0)
        
        self.dfs['sentiment'] = pivot_and_align(df_sent, 'sentiment', 'sentiment_decay')
        self.dfs['social_chatter'] = pivot_and_align(df_social, 'social_chatter', 'sentiment_decay')
        
        # Handle sentiment_spillover as sentiment + decaying asset relationships (graph overlay)
        # For simplicity, default it to sentiment for now
        self.dfs['sentiment_spillover'] = self.dfs['sentiment']

    def run(self, formula: str, holding_period: int = 1, slippage_bps: float = 5.0, 
            style: str = 'long_short', portfolio_size: Optional[int] = None) -> Dict[str, Any]:
        """Runs the quant strategy simulation and returns diagnostics, returns curve, and trade log."""
        if not self.dfs:
            self.load_data()
            
        parser = FormulaParser(self.dfs)
        raw_weights = parser.evaluate(formula)
        
        # Align weights shape
        close_df = self.dfs['close']
        raw_weights = raw_weights.reindex(index=close_df.index, columns=close_df.columns).fillna(0.0)
        
        # 1. Apply portfolio size constraint (keep only top/bottom N weights on each date)
        if portfolio_size is not None and portfolio_size > 0:
            def filter_top_n(row):
                # Separate long and short candidates
                n = min(portfolio_size, len(row))
                non_zero = row[row != 0]
                if len(non_zero) == 0:
                    return row
                
                # Find cuts
                sorted_vals = np.sort(row.values)
                bottom_cut = sorted_vals[n - 1] if len(sorted_vals) >= n else sorted_vals[-1]
                top_cut = sorted_vals[-n] if len(sorted_vals) >= n else sorted_vals[0]
                
                new_row = pd.Series(0.0, index=row.index)
                if style == 'long_only':
                    new_row[row >= top_cut] = row[row >= top_cut]
                else:
                    new_row[row >= top_cut] = row[row >= top_cut]
                    new_row[row <= bottom_cut] = row[row <= bottom_cut]
                return new_row

            raw_weights = raw_weights.apply(filter_top_n, axis=1)

        # 2. Enforce holding style rules
        if style == 'long_only':
            # Zero out negative weights
            raw_weights = raw_weights.clip(lower=0.0)
            # Scale so sum of weights equals 1.0
            daily_sum = raw_weights.sum(axis=1)
            weights = raw_weights.div(daily_sum.replace(0, 1e-15), axis=0)
        else: # long_short
            # Neutralize (de-mean weights so net sum is 0.0)
            daily_mean = raw_weights.mean(axis=1)
            neutral_weights = raw_weights.sub(daily_mean, axis=0)
            # Scale so absolute sum equals 1.0 (leverage = 1.0)
            abs_sum = neutral_weights.abs().sum(axis=1)
            weights = neutral_weights.div(abs_sum.replace(0, 1e-15), axis=0)

        # 3. Holding Period Smoothing (Decay)
        if holding_period > 1:
            weights = weights.rolling(window=holding_period, min_periods=1).mean()
            # Re-scale weights to 1.0 leverage after smoothing
            if style == 'long_only':
                weights = weights.div(weights.sum(axis=1).replace(0, 1e-15), axis=0)
            else:
                weights = weights.div(weights.abs().sum(axis=1).replace(0, 1e-15), axis=0)

        # 4. Calculate Returns and Slippage
        asset_returns = close_df.pct_change().fillna(0.0)
        # Clean infinite returns caused by division by zero/tiny numbers
        asset_returns = asset_returns.replace([np.inf, -np.inf], 0.0).fillna(0.0)
        
        # Strategy Return = Sum(weights[t-1] * asset_returns[t])
        strategy_returns = (weights.shift(1) * asset_returns).sum(axis=1)
        
        # Calculate Turnover & Slippage: Slippage is paid on rebalancing adjustments
        weight_changes = weights - weights.shift(1).fillna(0.0)
        daily_turnover = weight_changes.abs().sum(axis=1)
        
        slippage_fee = slippage_bps / 10000.0  # 1 bps = 0.0001
        daily_slippage_cost = daily_turnover * slippage_fee
        
        net_returns = strategy_returns - daily_slippage_cost
        
        # 5. Compute Metrics
        cum_wealth = (1 + net_returns).cumprod()
        cum_returns = cum_wealth - 1
        
        total_days = len(net_returns)
        if total_days < 2:
            raise ValueError("Backtest date range is too short.")
            
        ann_factor = 252.0
        ann_return = (cum_wealth.iloc[-1]) ** (ann_factor / total_days) - 1 if cum_wealth.iloc[-1] > 0 else -1.0
        
        daily_vol = net_returns.std()
        ann_vol = daily_vol * np.sqrt(ann_factor)
        
        sharpe = (net_returns.mean() / (daily_vol if daily_vol > 0 else 1e-15)) * np.sqrt(ann_factor)
        
        # Max Drawdown
        running_max = cum_wealth.cummax()
        drawdowns = (cum_wealth - running_max) / running_max
        max_drawdown = drawdowns.min()
        
        # Turnover
        avg_turnover = daily_turnover.mean()
        
        # Win Rate
        win_rate = (net_returns > 0).mean()
        
        # Information Coefficient (IC)
        # Correlation between signal of day t-1 and returns of day t
        daily_ic = weights.shift(1).corrwith(asset_returns, axis=1).fillna(0.0)
        mean_ic = daily_ic.mean()
        
        # Fitness Metric
        fitness = sharpe * np.sqrt(np.abs(ann_return)) / (avg_turnover if avg_turnover > 0 else 1e-15)
        
        # Benchmark (SPY) Comparison
        spy_ticker = 'SPY' if 'SPY' in close_df.columns else close_df.columns[0]
        spy_returns = asset_returns[spy_ticker]
        spy_cum = (1 + spy_returns).cumprod() - 1

        # 6. Format Return Chart Series
        chart_data = []
        for d, r, s, dd in zip(weights.index, cum_returns, spy_cum, drawdowns):
            chart_data.append({
                "date": d.strftime("%Y-%m-%d"),
                "strategy": round(float(r) * 100, 2),
                "benchmark": round(float(s) * 100, 2),
                "drawdown": round(float(dd) * 100, 2)
            })

        # 7. Generate Trade Log (last 100 trades or top allocations)
        # Find non-zero allocations on the last 5 days
        trade_log = []
        recent_dates = weights.index[-5:]
        for d in recent_dates:
            w_row = weights.loc[d]
            c_row = close_df.loc[d]
            active_tickers = w_row[w_row != 0.0]
            for ticker, weight in active_tickers.items():
                price = c_row[ticker]
                trade_log.append({
                    "date": d.strftime("%Y-%m-%d"),
                    "ticker": ticker,
                    "action": "BUY (Long)" if weight > 0 else "SELL (Short)",
                    "weight": round(float(weight) * 100, 2),
                    "price": round(float(price), 2),
                })
        
        # Sort log reverse chronological
        trade_log = sorted(trade_log, key=lambda x: x["date"], reverse=True)

        return {
            "metrics": {
                "sharpe": round(float(sharpe), 2),
                "annualized_return": round(float(ann_return) * 100, 2),
                "max_drawdown": round(float(max_drawdown) * 100, 2),
                "turnover": round(float(avg_turnover) * 100, 2),
                "win_rate": round(float(win_rate) * 100, 2),
                "ic": round(float(mean_ic), 4),
                "fitness": round(float(fitness), 2)
            },
            "chart": chart_data,
            "trades": trade_log[:150]  # Cap trade list size for API response efficiency
        }
