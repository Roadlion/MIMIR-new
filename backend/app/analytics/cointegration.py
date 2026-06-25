import yfinance as yf
import pandas as pd
import numpy as np

def calculate_z_score(ticker1, ticker2, period="1y"):
    """
    Downloads historical data for two tickers and calculates the current z-score
    of their price spread (Stat-Arb proxy).
    """
    try:
        data1 = yf.download(ticker1, period=period, progress=False)['Close']
        data2 = yf.download(ticker2, period=period, progress=False)['Close']
        
        # Flatten multi-index columns if any
        if isinstance(data1, pd.DataFrame):
            data1 = data1.iloc[:, 0]
        if isinstance(data2, pd.DataFrame):
            data2 = data2.iloc[:, 0]

        df = pd.concat([data1, data2], axis=1).dropna()
        df.columns = [ticker1, ticker2]
        
        if df.empty:
            return None

        # Calculate spread ratio (or simple difference, here we use ratio for normalized comparison)
        df['Spread'] = df[ticker1] / df[ticker2]
        
        mean_spread = df['Spread'].mean()
        std_spread = df['Spread'].std()
        
        if std_spread == 0:
            return None
            
        current_spread = df['Spread'].iloc[-1]
        z_score = (current_spread - mean_spread) / std_spread
        
        # Determine the signal
        if z_score > 2.0:
            signal = f"SHORT {ticker1}, LONG {ticker2}"
        elif z_score < -2.0:
            signal = f"LONG {ticker1}, SHORT {ticker2}"
        else:
            signal = "WAIT"

        return {
            "pair": f"{ticker1} / {ticker2}",
            "z_score": round(float(z_score), 2),
            "mean_spread": round(float(mean_spread), 4),
            "current_spread": round(float(current_spread), 4),
            "signal": signal,
            "status": "OPEN" if signal != "WAIT" else "CLOSED"
        }
    except Exception as e:
        print(f"Error calculating z-score for {ticker1}/{ticker2}: {e}")
        return None

def scan_niche_opportunities():
    """
    Scans predefined niche pairs for stat-arb opportunities.
    Using ETFs as proxies for obscure commodities since yfinance blocks raw futures often.
    - CORN (Corn) vs WEAT (Wheat)
    - BDRY (Dry Bulk Shipping) vs SBLK (Star Bulk Carriers)
    - URA (Uranium) vs NLR (Nuclear Energy)
    """
    pairs = [
        ("CORN", "WEAT"),
        ("BDRY", "SBLK"),
        ("URA", "NLR")
    ]
    
    opportunities = []
    for t1, t2 in pairs:
        res = calculate_z_score(t1, t2)
        if res:
            opportunities.append(res)
            
    return opportunities
