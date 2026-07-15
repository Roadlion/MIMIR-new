import MetaTrader5 as mt5
import pandas as pd
from datetime import datetime

# Initialize MT5 connection
if not mt5.initialize():
    print(f"initialize() failed, error code: {mt5.last_error()}")
    quit()

symbol = "BTCUSD"
timeframe = mt5.TIMEFRAME_H1  # 1-hour bars

# Define your exact date range (Year, Month, Day, Hour, Minute)
utc_from = datetime(2026, 1, 1, 0, 0)
utc_to = datetime(2026, 6, 15, 0, 0)

# Pull the data within the range
rates = mt5.copy_rates_range(symbol, timeframe, utc_from, utc_to)

# Always remember to shut down when done fetching  
mt5.shutdown()

if rates is not None and len(rates) > 0:
    # Convert to DataFrame
    df = pd.DataFrame(rates)
    df['time'] = pd.to_datetime(df['time'], unit='s')
    
    print(f"Successfully pulled {len(df)} bars.")
    print(df.head())
else:
    print(f"No data found or error: {mt5.last_error()}")


# class strategies():
#     def __init__(self):
# 