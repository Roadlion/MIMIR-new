import yfinance as yf

niche_tickers = [
    "HE=F", # Lean Hogs
    "LBS=F", # Lumber
    "^BDI", # Baltic Dry Index
    "EBS.PA", # Milling Wheat (Euronext)
    "KCU24.CME", # Just guessing some formats
    "ALI=F", # Aluminum
]

for t in niche_tickers:
    tkr = yf.Ticker(t)
    try:
        hist = tkr.history(period="1d")
        if not hist.empty:
            print(f"[OK] {t}: {hist['Close'].iloc[-1]}")
        else:
            print(f"[FAIL] {t} - empty")
    except Exception as e:
        print(f"[ERROR] {t}: {e}")
