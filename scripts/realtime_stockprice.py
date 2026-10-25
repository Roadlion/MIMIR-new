import MetaTrader5 as mt5
from datetime import datetime
import csv
import os
import time

CSV_NAME = "mt5_1m_prices.csv"

# --- 1. CSV Setup ---
if not os.path.exists(CSV_NAME):
    with open(CSV_NAME, mode='w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(["symbol", "datetime", "open", "high", "low", "close", "volume"])
    print(f"[SUCCESS] Created file: {CSV_NAME}")

# --- 2. Connect to MT5 ---
if not mt5.initialize():
    print(f"[ERROR] MT5 initialization failed. Error code: {mt5.last_error()}")
    quit()
print("[SUCCESS] Connected to MT5 Terminal.")

# ⚠️ DOUBLE CHECK THESE: Make sure these match your Market Watch exactly!
symbols = [
    "NVDA.US", "AAPL.US", "GOOGL.US", "MSFT.US", "AMZN.US", 
    "AVGO.US", "META.US", "TSLA.US", "MU.US", "BRK.B.US", 
    "LLY.US", "JPM.US", "WMT.US", "AMD.US", "V.US", 
    "JNJ.US", "XOM.US", "INTC.US", "MA.US", "AMAT.US", 
    "CSCO.US", "LRCX.US", "ABBV.US", "CAT.US", "BAC.US", 
    "COST.US", "UNH.US", "GE.US", "ORCL.US", "CVX.US", 
    "MS.US", "KO.US", "PG.US", "HD.US", "GS.US", 
    "PLTR.US", "NFLX.US", "KLAC.US", "MRK.US", "DELL.US", 
    "PANW.US", "GEV.US", "TXN.US", "AXP.US", "LIN.US", 
    "ANET.US", "C.US", "CRWD.US", "IBM.US", "TMUS.US", "BTCUSD"
]

# Verify symbols actively
active_symbols = []
for symbol in symbols:
    if mt5.symbol_select(symbol, True):
        print(f"[SYMBOL OK] Active and tracking: {symbol}")
        active_symbols.append(symbol)
    else:
        print(f"[SYMBOL ERROR] '{symbol}' not found! Check your broker's exact spelling (e.g., AAPL.US or #AAPL)")

if not active_symbols:
    print("[CRITICAL] No valid symbols to track. Stopping script.")
    mt5.shutdown()
    quit()

def fetch_and_log():
    log_time = datetime.now().strftime('%H:%M:%S')
    batch_rows = []
    
    for symbol in active_symbols:
        # copy_rates_from_pos is correct: (symbol, timeframe, start_position, count)
        rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M1, 1, 1)
        
        if rates is not None and len(rates) > 0:
            bar = rates[0]
            bar_time = datetime.fromtimestamp(int(bar['time'])).strftime('%Y-%m-%d %H:%M:%S')
            
            batch_rows.append([
                symbol,
                bar_time,
                float(bar['open']),
                float(bar['high']),
                float(bar['low']),
                float(bar['close']),
                int(bar['tick_volume'])
            ])
        else:
            print(f"[DATA ERROR] Could not get data for {symbol}. Error: {mt5.last_error()}")
            
    if batch_rows:
        with open(CSV_NAME, mode='a', newline='') as f:
            writer = csv.writer(f)
            writer.writerows(batch_rows)
        
        print(f"\n--- Logged Batch to CSV at {log_time} ---")
        for row in batch_rows:
            print(f"{row[0]:<6} | Time: {row[1]} | Close Price: {row[5]:<8} | Vol: {row[6]}")

# --- 3. Run Instantly First ---
print("\n[STARTING] Pulling initial batch immediately...")
fetch_and_log()

print("\n[RUNNING] Initial pull done. Now entering 1-minute clock sync loop...")

try:
    while True:
        # Perfect 1-minute clock alignment loop
        current_time = time.time()
        sleep_time = 60 - (current_time % 60)
        time.sleep(sleep_time)
        
        # Trigger every turnaround minute
        fetch_and_log()

except KeyboardInterrupt:
    print("\n[STOPPING] Script terminated by user.")

finally:
    mt5.shutdown()