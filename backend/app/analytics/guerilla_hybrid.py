# backend/app/analytics/guerilla_hybrid.py
"""
DEPRECATED / COMPATIBILITY LAYER:
Guerilla Quant has been completely superseded by the Stat-Arb Auxiliary Power Unit (APU)
in backend/app/analytics/stat_arb_apu.py.

All pair evaluations now run through the Online Kalman Filter, Ornstein-Uhlenbeck half-life,
ADF stationarity, and DeepSeek sentiment circuit-breakers in StatArbAPU.
"""

from typing import List, Dict, Any
from .stat_arb_apu import StatArbAPU, APU_PAIRS
from ..database import get_db_connection
from ..config import get_settings

settings = get_settings()

# Backwards compatibility export
NICHE_ASSETS_DATA = [
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
    ("AAPL", "Apple Inc.", "EQUITY", "NASDAQ"),
    ("MSFT", "Microsoft Corp.", "EQUITY", "NASDAQ"),
    ("GOOG", "Alphabet Inc. Class C", "EQUITY", "NASDAQ"),
    ("GOOGL", "Alphabet Inc. Class A", "EQUITY", "NASDAQ"),
    ("GS", "Goldman Sachs Group Inc.", "EQUITY", "NYSE"),
    ("MS", "Morgan Stanley", "EQUITY", "NYSE"),
    ("KO", "Coca-Cola Co.", "EQUITY", "NYSE"),
    ("PEP", "PepsiCo Inc.", "EQUITY", "NASDAQ"),
    ("CVX", "Chevron Corp.", "EQUITY", "NYSE"),
    ("XOM", "Exxon Mobil Corp.", "EQUITY", "NYSE"),
]


def _ensure_niche_assets(conn=None):
    """Ensure all standard niche tickers exist in mimir_niche_assets."""
    close_conn = False
    if conn is None:
        conn = get_db_connection()
        close_conn = True
    try:
        cur = conn.cursor()
        for ticker, name, asset_class, exchange in NICHE_ASSETS_DATA:
            cur.execute(f"""
                INSERT INTO {settings.mimir_schema}.mimir_niche_assets (ticker, name, asset_class, exchange)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (ticker) DO NOTHING
            """, (ticker, name, asset_class, exchange))
        conn.commit()
        cur.close()
    finally:
        if close_conn:
            conn.close()


def get_hybrid_signals() -> List[Dict[str, Any]]:
    """
    Seamless compatibility layer: delegates directly to the institutional
    Stat-Arb APU master scan (Kalman filter, OU half-life, ADF, and DeepSeek circuit-breaker).
    """
    return StatArbAPU.run_master_scan()
