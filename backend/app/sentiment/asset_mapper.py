# backend/app/sentiment/asset_mapper.py
from typing import Optional

# Country code mapping (ISO 3166-1 alpha-2)
COUNTRY_TO_CODE = {
    "united states": "US",
    "usa": "US",
    "us": "US",
    "america": "US",
    "thailand": "TH",
    "thai": "TH",
    "china": "CN",
    "japan": "JP",
    "japanese": "JP",
    "uk": "GB",
    "britain": "GB",
    "united kingdom": "GB",
    "germany": "DE",
    "france": "FR",
    "italy": "IT",
    "spain": "ES",
    "australia": "AU",
    "canada": "CA",
    "brazil": "BR",
    "india": "IN",
    "south korea": "KR",
    "korea": "KR",
    "singapore": "SG",
    "malaysia": "MY",
    "indonesia": "ID",
    "philippines": "PH",
    "vietnam": "VN",
    "switzerland": "CH",
    "swiss": "CH",
}

# Region mapping
COUNTRY_TO_REGION = {
    # NA (North America)
    "US": "NA", "CA": "NA", "MX": "NA", "GL": "NA",
    
    # EU (Europe)
    "GB": "EU", "DE": "EU", "FR": "EU", "IT": "EU", "ES": "EU", "CH": "EU", 
    "NL": "EU", "BE": "EU", "AT": "EU", "DK": "EU", "FI": "EU", "GR": "EU", 
    "IE": "EU", "NO": "EU", "PT": "EU", "SE": "EU", "PL": "EU", "UA": "EU", 
    "RO": "EU", "HU": "EU", "CZ": "EU", "SK": "EU", "BG": "EU", "HR": "EU",
    "EE": "EU", "LV": "EU", "LT": "EU", "SI": "EU", "IS": "EU", "LU": "EU",
    "EU": "EU",

    # APAC (Asia-Pacific excluding ASEAN)
    "JP": "APAC", "CN": "APAC", "KR": "APAC", "IN": "APAC", "AU": "APAC", 
    "NZ": "APAC", "TW": "APAC", "PK": "APAC", "BD": "APAC", "LK": "APAC", 
    "NP": "APAC", "MN": "APAC", "KP": "APAC", "AF": "APAC", "HK": "APAC",

    # ASEAN (Southeast Asia)
    "SG": "ASEAN", "TH": "ASEAN", "MY": "ASEAN", "ID": "ASEAN", "PH": "ASEAN", 
    "VN": "ASEAN", "MM": "ASEAN", "KH": "ASEAN", "LA": "ASEAN", "BN": "ASEAN", 
    "TL": "ASEAN",

    # LATAM (Latin America)
    "BR": "LATAM", "AR": "LATAM", "CL": "LATAM", "CO": "LATAM", "PE": "LATAM", 
    "VE": "LATAM", "EC": "LATAM", "BO": "LATAM", "PY": "LATAM", "UY": "LATAM", 
    "GT": "LATAM", "HN": "LATAM", "SV": "LATAM", "NI": "LATAM", "CR": "LATAM", 
    "PA": "LATAM", "CU": "LATAM", "DO": "LATAM", "PR": "LATAM", "JM": "LATAM",

    # MENA (Middle East & North Africa)
    "SA": "MENA", "AE": "MENA", "IL": "MENA", "TR": "MENA", "IR": "MENA", 
    "IQ": "MENA", "JO": "MENA", "LB": "MENA", "SY": "MENA", "YE": "MENA", 
    "OM": "MENA", "QA": "MENA", "KW": "MENA", "BH": "MENA", "EG": "MENA",
    "LY": "MENA", "TN": "MENA", "DZ": "MENA", "MA": "MENA", "SD": "MENA",

    # AFRICA (Sub-Saharan Africa)
    "ZA": "AFRICA", "KE": "AFRICA", "NG": "AFRICA", "GH": "AFRICA", "TZ": "AFRICA", 
    "UG": "AFRICA", "ET": "AFRICA", "AO": "AFRICA", "MZ": "AFRICA", "CI": "AFRICA",
    "CM": "AFRICA", "SN": "AFRICA", "ZW": "AFRICA", "ZM": "AFRICA", "CD": "AFRICA", 
    "CG": "AFRICA", "GA": "AFRICA", "NE": "AFRICA", "TD": "AFRICA", "ML": "AFRICA", 
    "MR": "AFRICA", "MG": "AFRICA", "NA": "AFRICA", "BW": "AFRICA", "SZ": "AFRICA", 
    "LS": "AFRICA"
}

# Asset to ticker mapping
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
    "gulf": "GULF.BK",
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

_DYNAMIC_TICKERS = {}

def load_dynamic_tickers():
    """Load dynamic ticker mappings from the database."""
    global _DYNAMIC_TICKERS
    try:
        from backend.app.database import get_db_connection
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT asset_name, ticker FROM yggdrasil.mimir_dynamic_tickers")
        rows = cur.fetchall()
        for row in rows:
            _DYNAMIC_TICKERS[row[0].lower().strip()] = row[1]
        cur.close()
        conn.close()
    except Exception as e:
        # Avoid blocking if database connection fails or table doesn't exist yet
        pass

# Initialize cache
try:
    load_dynamic_tickers()
except Exception:
    pass

def resolve_ticker_online(asset_name: str) -> Optional[str]:
    """
    Search Yahoo Finance API for a matching ticker for the given asset name.
    Saves the mapping to database and internal cache if found.
    """
    if not asset_name:
        return None
    
    clean_name = asset_name.strip()
    # Don't search for broad terms
    if clean_name.lower() in [
        "s&p 500", "nasdaq", "us economy", "global economy", 
        "thai economy", "china economy", "india economy", "uk economy",
        "geopolitical risk", "risk-on", "risk-off"
    ]:
        return None
        
    print(f"[ASSET_MAPPER] Resolving '{clean_name}' online via Yahoo Finance...")
    try:
        import urllib3
        import requests
        
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        url = f"https://query2.finance.yahoo.com/v1/finance/search?q={requests.utils.quote(clean_name)}&lang=en-US&region=US"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        resp = requests.get(url, headers=headers, verify=False, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        quotes = data.get("quotes", [])
        
        if not quotes:
            print(f"[ASSET_MAPPER] No online quotes found for '{clean_name}'")
            return None
            
        valid_types = {"EQUITY", "ETF", "INDEX", "CRYPTOCURRENCY"}
        filtered = [q for q in quotes if q.get("quoteType") in valid_types]
        if not filtered:
            filtered = quotes
            
        best_quote = None
        best_score = -1
        
        for q in filtered:
            symbol = q.get("symbol", "")
            if not symbol:
                continue
                
            score = 0
            if "." not in symbol:
                score += 10
            exchange = q.get("exchange", "")
            if exchange in {"NMS", "NYQ", "NYS", "NGM", "PCX"}:
                score += 5
                
            if score > best_score:
                best_score = score
                best_quote = q
                
        if best_quote:
            ticker = best_quote["symbol"]
            print(f"[ASSET_MAPPER] Resolved '{clean_name}' -> {ticker} (Type: {best_quote.get('quoteType')})")
            
            # Save to Database
            try:
                from backend.app.database import get_db_connection
                conn = get_db_connection()
                cur = conn.cursor()
                cur.execute("""
                    INSERT INTO yggdrasil.mimir_dynamic_tickers (asset_name, ticker)
                    VALUES (%s, %s)
                    ON CONFLICT (asset_name) DO UPDATE SET ticker = EXCLUDED.ticker
                """, (clean_name, ticker))
                conn.commit()
                cur.close()
                conn.close()
            except Exception as dbe:
                print(f"[ASSET_MAPPER] Database write error: {dbe}")
                
            _DYNAMIC_TICKERS[clean_name.lower().strip()] = ticker
            return ticker
            
    except Exception as e:
        print(f"[ASSET_MAPPER] Online lookup error for '{clean_name}': {e}")
        
    return None

def resolve_ticker(asset_name: str):
    """Return (ticker, found). Checks static mappings, dynamic DB cache, and online lookup."""
    from typing import Optional
    if not asset_name:
        return (None, False)
        
    key = asset_name.lower().strip()
    
    # 1. Check static mapping
    ticker = ASSET_TO_TICKER.get(key)
    if ticker:
        return (ticker, True)
        
    # 2. Check dynamic cache
    ticker = _DYNAMIC_TICKERS.get(key)
    if ticker:
        return (ticker, True)
        
    # 3. Try online lookup
    ticker = resolve_ticker_online(asset_name)
    if ticker:
        return (ticker, True)
        
    return (None, False)

def resolve_country_code(country_name: str):
    """Convert full country name to ISO code."""
    if not country_name:
        return None
    return COUNTRY_TO_CODE.get(country_name.lower().strip())

def resolve_region(country_code: str):
    """Get region from country code."""
    if not country_code:
        return None
    return COUNTRY_TO_REGION.get(country_code)