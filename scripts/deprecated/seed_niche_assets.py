"""Seed yggdrasil.mimir_niche_assets with known niche tickers for Guerilla Quant."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from backend.app.database import get_db_connection

NICHE_TICKERS = [
    ("CORN", "Teucrium Corn ETF", "COMMODITY", "NYSEARCA"),
    ("WEAT", "Teucrium Wheat ETF", "COMMODITY", "NYSEARCA"),
    ("SOYB", "Teucrium Soybean ETF", "COMMODITY", "NYSEARCA"),
    ("BDRY", "Breakwave Dry Bulk Shipping ETF", "COMMODITY", "NYSEARCA"),
    ("SBLK", "Star Bulk Carriers", "EQUITY", "NASDAQ"),
    ("GOGL", "Golden Ocean Group", "EQUITY", "NASDAQ"),
    ("URA", "Global X Uranium ETF", "EQUITY", "NYSEARCA"),
    ("NLR", "VanEck Uranium+Nuclear Energy ETF", "EQUITY", "NYSEARCA"),
    ("GDX", "VanEck Gold Miners ETF", "EQUITY", "NYSEARCA"),
    ("GDXJ", "VanEck Junior Gold Miners ETF", "EQUITY", "NYSEARCA"),
    ("COPX", "Global X Copper Miners ETF", "EQUITY", "NYSEARCA"),
    ("LIT", "Global X Lithium & Battery Tech ETF", "EQUITY", "NYSEARCA"),
    ("XLE", "Energy Select Sector SPDR", "EQUITY", "NYSEARCA"),
    ("XOP", "SPDR S&P Oil & Gas E&P ETF", "EQUITY", "NYSEARCA"),
]

def main():
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        for ticker, name, asset_class, exchange in NICHE_TICKERS:
            cur.execute("""
                INSERT INTO yggdrasil.mimir_niche_assets (ticker, name, asset_class, exchange)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (ticker) DO NOTHING
            """, (ticker, name, asset_class, exchange))
        conn.commit()
        count = cur.rowcount
        print(f"[OK] Seeded {count} niche asset(s) into yggdrasil.mimir_niche_assets.")
        cur.close()
        conn.close()
    except Exception as e:
        print(f"[ERROR] {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
