import yfinance as yf

ASSET_TO_TICKER = {
    # ==================== CURRENCIES (Forex & Dollar Index) ====================
    "us dollar": "DX-Y.NYB",
    "euro": "EURUSD=X",
    "japanese yen": "USDJPY=X",
    "british pound": "GBPUSD=X",
    "swiss franc": "USDCHF=X",
    "chinese yuan": "USDCNY=X",
    "thai baht": "USDTHB=X",
    "australian dollar": "AUDUSD=X",
    "canadian dollar": "USDCAD=X",
    "new zealand dollar": "NZDUSD=X",
    "south korean won": "USDKRW=X",
    "singapore dollar": "USDSGD=X",
    "indian rupee": "USDINR=X",
    "brazilian real": "USDBRL=X",
    "mexican peso": "USDMXN=X",
    "south african rand": "USDZAR=X",
    "russian ruble": "USDRUB=X",
    "turkish lira": "USDTRY=X",
    "hong kong dollar": "USDHKD=X",
    "euro pound": "EURGBP=X",
    "euro yen": "EURJPY=X",
    "pound yen": "GBPJPY=X",
    # ==================== COMMODITIES ====================
    "gold": "GC=F",
    "silver": "SI=F",
    "platinum": "PL=F",
    "palladium": "PA=F",
    "crude oil": "CL=F",
    "brent crude": "BZ=F",
    "natural gas": "NG=F",
    "rboB gasoline": "RB=F",
    "heating oil": "HO=F",
    "copper": "HG=F",
    "aluminium": "ALI=F",
    "zinc": "^NQCIZNTR",
    "nickel": "^NQCINIER",
    "wheat": "ZW=F",
    "corn": "ZC=F",
    "soybeans": "ZS=F",
    "soybean meal": "ZM=F",
    "soybean oil": "ZL=F",
    "oats": "ZO=F",
    "rough rice": "ZR=F",
    "sugar": "SB=F",
    "coffee": "KC=F",
    "cocoa": "CC=F",
    "cotton": "CT=F",
    "orange juice": "OJ=F",
    "live cattle": "LE=F",
    "lean hogs": "HE=F",
    "feeder cattle": "GF=F",
    # ==================== STOCK INDICES ====================
    "s&p 500": "SPY",
    "spx": "^GSPC",
    "nasdaq": "QQQ",
    "nasdaq composite": "^IXIC",
    "dow jones": "^DJI",
    "russell 2000": "^RUT",
    "vix": "^VIX",
    "set index": "^SET.BK",
    "nikkei 225": "^N225",
    "dax": "^GDAXI",
    "ftse 100": "^FTSE",
    "cac 40": "^FCHI",
    "smi": "^SSMI",
    "mib": "FTSEMIB.MI",
    "ibex 35": "^IBEX",
    "hang seng": "^HSI",
    "shanghai composite": "000001.SS",
    "shenzhen": "399001.SZ",
    "csi 300": "000300.SS",
    "kospi": "^KS11",
    "nifty 50": "^NSEI",
    "bovespa": "^BVSP",
    "asx 200": "^AXJO",
    "straits times": "^STI",
    "taiwan weighted": "^TWII",
    "msci world": "URTH",
    # ==================== GOVERNMENT BONDS (yields) ====================
    "us 10y treasury": "^TNX",
    "us 5y treasury": "^FVX",
    "us 30y treasury": "^TYX",
    "uk gilt": "BG07.L",
    # ==================== MAJOR COMPANIES (Equities) ====================
    "apple": "AAPL",
    "microsoft": "MSFT",
    "alphabet": "GOOGL",
    "google": "GOOGL",
    "amazon": "AMZN",
    "nvidia": "NVDA",
    "meta": "META",
    "facebook": "META",
    "tesla": "TSLA",
    "broadcom": "AVGO",
    "oracle": "ORCL",
    "cisco": "CSCO",
    "ibm": "IBM",
    "salesforce": "CRM",
    "adobe": "ADBE",
    "amd": "AMD",
    "intel": "INTC",
    "qualcomm": "QCOM",
    "texas instruments": "TXN",
    "servicenow": "NOW",
    "palantir": "PLTR",
    "jpmorgan": "JPM",
    "visa": "V",
    "mastercard": "MA",
    "bank of america": "BAC",
    "wells fargo": "WFC",
    "goldman sachs": "GS",
    "morgan stanley": "MS",
    "blackrock": "BLK",
    "berkshire hathaway": "BRK-B",
    "johnson & johnson": "JNJ",
    "unitedhealth": "UNH",
    "pfizer": "PFE",
    "merck": "MRK",
    "abbvie": "ABBV",
    "eli lilly": "LLY",
    "novo nordisk": "NVO",
    "astrazeneca": "AZN",
    "novartis": "NVS",
    "roche": "RHHBY",
    "amgen": "AMGN",
    "gilead": "GILD",
    "medtronic": "MDT",
    "procter & gamble": "PG",
    "coca-cola": "KO",
    "pepsi": "PEP",
    "walmart": "WMT",
    "costco": "COST",
    "home depot": "HD",
    "lowes": "LOW",
    "mcdonald's": "MCD",
    "nike": "NKE",
    "starbucks": "SBUX",
    "target": "TGT",
    "lululemon": "LULU",
    "exxon mobil": "XOM",
    "chevron": "CVX",
    "shell": "SHEL",
    "bp": "BP",
    "totalenergies": "TTE",
    "conocophillips": "COP",
    "general electric": "GE",
    "caterpillar": "CAT",
    "honeywell": "HON",
    "boeing": "BA",
    "raytheon": "RTX",
    "lockheed martin": "LMT",
    "union pacific": "UNP",
    "deere": "DE",
    "fedex": "FDX",
    "ups": "UPS",
    "3m": "MMM",
    "at&t": "T",
    "verizon": "VZ",
    "tmobile": "TMUS",
    "netflix": "NFLX",
    "disney": "DIS",
    "comcast": "CMCSA",
    "warner bros": "WBD",
    "linde": "LIN",
    "sherwin williams": "SHW",
    "bhp": "BHP",
    "rio tinto": "RIO",
    "prologis": "PLD",
    "american tower": "AMT",
    "tsmc": "TSM",
    "samsung": "005930.KS",
    "toyota": "TM",
    "honda": "HMC",
    "lvmh": "MC.PA",
    "nestle": "NESN.SW",
    "alibaba": "BABA",
    "tencent": "TCEHY",
    "meituan": "MPNGY",
    "reliance": "RELIANCE.NS",
    "icici bank": "IBN",
    "hdfc bank": "HDB",
    "aia group": "AAGIY",
    "softbank": "SFTBY",
    "asml": "ASML",
    "sap": "SAP",
    "siemens": "SIEGY",
    "schneider": "SBGSY",
    "anheuser-busch": "BUD",
    "unilever": "UL",
    "diageo": "DEO",
    "glencore": "GLNCY",
    "endeavour": "EDV",
    # ... existing ...
    "united parks & resorts": "PRKS",
    "lucid group": "LCID",
    "americas gold and silver": "USAS",
    "greenidge": "GREE",
    "blacksky technology": "BKSY",
    "netstreit": "NTST",
    "sherwin-williams": "SHW",
    "installed building products": "IBP",
    "apogee enterprises": "APOG",
    "howmet aerospace": "HWM",
    "hercules capital": "HTGC",
    "ashok leyland": "ASHOKLEY.NS",
    "tata motors": "TMCV.NS",
    "astera labs": "ALAB",
    "sitime": "SITM",
    "summit therapeutics": "SMMT",
    "agilon health": "AGL",
    "exodus movement": "EXOD",
}


def validate_tickers(asset_dict):
    # Extract unique tickers to optimize API calls
    unique_tickers = list(set(asset_dict.values()))

    print(f"Validating {len(unique_tickers)} unique tickers via yfinance...")

    # Fetch data for all symbols at once
    tickers_obj = yf.Tickers(" ".join(unique_tickers))

    valid_tickers = set()
    invalid_tickers = set()

    for ticker_symbol in unique_tickers:
        ticker = tickers_obj.tickers[ticker_symbol]
        try:
            # yfinance creates an internal history metadata dictionary if the ticker exists.
            # Checking ticker.history_metadata is much faster and more reliable than .info
            # which frequently triggers rate limits/404 errors in recent yfinance versions.
            meta = ticker.history_metadata

            if meta and "symbol" in meta:
                valid_tickers.add(ticker_symbol)
            else:
                invalid_tickers.add(ticker_symbol)
        except Exception:
            invalid_tickers.add(ticker_symbol)

    # Cross-reference with the original asset dictionary to present clear results
    print("\n" + "=" * 40)
    print(" RESULTS ")
    print("=" * 40)

    if invalid_tickers:
        print(f"❌ Found {len(invalid_tickers)} INVALID ticker(s):")
        for name, ticker in asset_dict.items():
            if ticker in invalid_tickers:
                print(f"  - '{name}': '{ticker}'")
    else:
        print("✅ All tickers are valid and actively returning metadata!")


if __name__ == "__main__":
    # Ensure you have the library installed: pip install yfinance
    validate_tickers(ASSET_TO_TICKER)