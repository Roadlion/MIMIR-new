import os
import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
from concurrent.futures import ThreadPoolExecutor
from ..database import get_db_connection_dict
from ..config import get_settings

settings = get_settings()

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
STATIC_LOGOS_DIR = os.path.join(BASE_DIR, "frontend", "static", "img", "logos")

os.makedirs(STATIC_LOGOS_DIR, exist_ok=True)

def sanitize_ticker(ticker: str) -> str:
    """Normalize ticker symbol for file naming (e.g. BTC-USD -> BTC, AAPL -> AAPL)."""
    if not ticker:
        return "UNKNOWN"
    clean = ticker.strip().upper()
    if clean.endswith("-USD"):
        clean = clean[:-4]
    return clean

def get_existing_logo_url(ticker_clean: str) -> str | None:
    """Check if logo file already exists in static/img/logos."""
    for ext in [".png", ".svg", ".jpg", ".jpeg"]:
        filename = f"{ticker_clean}{ext}"
        filepath = os.path.join(STATIC_LOGOS_DIR, filename)
        if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
            return f"/static/img/logos/{filename}"
    return None

def generate_svg_fallback(ticker_clean: str) -> str:
    """Generate a clean SVG logo with ticker initials and save to static/img/logos."""
    initials = ticker_clean[:4]
    svg_content = f"""<svg xmlns="http://www.w3.org/2000/svg" width="50" height="50" viewBox="0 0 50 50">
  <rect width="100%" height="100%" rx="10" fill="#111A20" stroke="#1A2A30" stroke-width="2"/>
  <text x="50%" y="54%" dominant-baseline="middle" text-anchor="middle" fill="#00A6B2" font-family="system-ui, -apple-system, sans-serif" font-weight="bold" font-size="14">{initials}</text>
</svg>"""
    filename = f"{ticker_clean}.svg"
    filepath = os.path.join(STATIC_LOGOS_DIR, filename)
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(svg_content)
        return f"/static/img/logos/{filename}"
    except Exception as e:
        print(f"[LOGO_DOWNLOADER] Error writing SVG fallback for {ticker_clean}: {e}")
        return ""

def ensure_local_logo(ticker: str) -> str:
    """
    Returns the local URL for a ticker's logo.
    If the file does not exist, fetches it from a CDN or generates a fallback SVG.
    """
    clean = sanitize_ticker(ticker)
    
    # 1. Check local cache
    existing = get_existing_logo_url(clean)
    if existing:
        return existing
        
    # 2. Try fetching PNG from CDNs
    sources = [
        f"https://assets.parqet.com/logos/symbol/{clean}?format=png",
        f"https://finance-logo.perplexity.ai/ticker/{clean}?format=png&fallback=404&size=50&theme=dark"
    ]
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    
    for url in sources:
        try:
            resp = requests.get(url, headers=headers, timeout=4, verify=False)
            if resp.status_code == 200 and len(resp.content) > 100:
                content_type = resp.headers.get("content-type", "").lower()
                if "image" in content_type or resp.content.startswith(b"\x89PNG") or resp.content.startswith(b"\xff\xd8"):
                    filename = f"{clean}.png"
                    filepath = os.path.join(STATIC_LOGOS_DIR, filename)
                    with open(filepath, "wb") as f:
                        f.write(resp.content)
                    print(f"[LOGO_DOWNLOADER] Successfully downloaded logo for {clean} from {url}")
                    return f"/static/img/logos/{filename}"
        except Exception as e:
            print(f"[LOGO_DOWNLOADER] Failed downloading logo for {clean} from {url}: {e}")
            
    # 3. Fallback to SVG badge
    return generate_svg_fallback(clean)

def preseed_portfolio_logos():
    """Download logos for all distinct tickers in mimir_portfolio."""
    try:
        conn = get_db_connection_dict()
        cur = conn.cursor()
        cur.execute(f"SELECT DISTINCT ticker FROM {settings.mimir_schema}.mimir_portfolio")
        rows = cur.fetchall()
        cur.close()
        conn.close()
        
        tickers = [r["ticker"] for r in rows if r.get("ticker")]
        if not tickers:
            return
            
        print(f"[LOGO_DOWNLOADER] Pre-seeding logos for {len(tickers)} portfolio tickers...")
        with ThreadPoolExecutor(max_workers=5) as executor:
            executor.map(ensure_local_logo, tickers)
        print(f"[LOGO_DOWNLOADER] Portfolio logos pre-seeded successfully.")
    except Exception as e:
        print(f"[LOGO_DOWNLOADER] Error during preseed_portfolio_logos: {e}")
