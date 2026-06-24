import yfinance as yf

# Initialize the ticker (e.g., Apple)
ticker = yf.Ticker("GULF.BK")

info = ticker.info.get('displayName')
print(info)