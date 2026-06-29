#!/usr/bin/env python3
"""
Seed the mimir_asset_relationships table from existing data structures:
  - HEATMAP_INDICES (sector -> constituent equities)
  - SECTOR_TICKERS (sector name -> US ETF ticker)
  - COUNTRY_SECTOR_TICKERS (country+sector -> ETF ticker)
  - COUNTRY_CB_MAP, COUNTRY_BOND_MAP, COUNTRY_INDEX_MAP (macro -> assets)
  - Hand-curated thematic rules
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.app.database import get_db_connection
from backend.app.routers.prices import HEATMAP_INDICES
from backend.app.routers.sentiment import (
    SECTOR_TICKERS, COUNTRY_SECTOR_TICKERS, SECTOR_NORM_MAP,
    COUNTRY_CB_MAP, COUNTRY_BOND_MAP, COUNTRY_INDEX_MAP,
)

# ============================================================================
# Sector name normalization (extracted from sentiment.py's normalize_sector_name)
# ============================================================================
def _norm_sector(raw: str) -> str:
    """Map HEATMAP_INDICES sector labels to canonical names used in SECTOR_TICKERS."""
    u = raw.upper()
    if "TECH" in u: return "Technology"
    if "ENERGY" in u: return "Energy"
    if "CYCLICAL" in u or "DISCRETIONARY" in u: return "Consumer Cyclical"
    if "DEFENSIVE" in u or "STAPLES" in u: return "Consumer Defensive"
    if "COMM" in u: return "Communication Services"
    if "INDUSTRIAL" in u: return "Industrials"
    if "FINANCIAL" in u: return "Financial Services"
    if "UTILIT" in u: return "Utilities"
    if "MATERIAL" in u or "BASIC" in u: return "Basic Materials"
    if "REAL" in u: return "Real Estate"
    if "HEALTH" in u: return "Healthcare"
    return raw


# ============================================================================
# Relationship rows: (source_type, source_key, target_type, target_key, decay)
# ============================================================================
RELATIONSHIPS = []

# --- 1. Sector ETF -> Constituent equities (from HEATMAP_INDICES) ---
# For each index, map sector -> ticker using "US.<Sector>" as source key
for idx_key, idx_info in HEATMAP_INDICES.items():
    # Determine country prefix from the index
    country_prefix = {
        "sp500": "US", "set50": "TH", "kospi200": "KR", "nifty50": "IN",
        "nikkei225": "JP", "ftse100": "GB", "dax": "DE", "cac40": "FR",
        "stoxx600": "EU", "spchina500": "CN",
    }.get(idx_key, "US")

    for c in idx_info.get("constituents", []):
        ticker = c.get("ticker")
        sector_raw = c.get("sector")
        if not ticker or not sector_raw:
            continue
        sector_canon = _norm_sector(sector_raw)
        source_key = f"{country_prefix}.{sector_canon}"

        RELATIONSHIPS.append((
            "sector", source_key, "ticker", ticker, 0.45
        ))

# --- 2. Sector name -> US Sector ETF (from SECTOR_TICKERS) ---
for sector_name, etf_ticker in SECTOR_TICKERS.items():
    source_key = f"US.{sector_name}"
    RELATIONSHIPS.append((
        "sector", source_key, "ticker", etf_ticker, 0.50
    ))
    # Also add the ETF -> sector (reverse, for constituent spillover)
    RELATIONSHIPS.append((
        "constituent", etf_ticker, "sector", source_key, 0.30
    ))

# --- 3. Country+Sector -> Country Sector ETF ---
for country_code, sectors in COUNTRY_SECTOR_TICKERS.items():
    if isinstance(sectors, str):
        continue  # alias like "DE": "EU", skip
    for sector_name, etf_ticker in sectors.items():
        source_key = f"{country_code}.{sector_name}"
        RELATIONSHIPS.append((
            "sector", source_key, "ticker", etf_ticker, 0.50
        ))

# --- 4. Macro: Central Bank -> currency, bonds, index ---
CB_ASSETS = {
    "Federal Reserve": {"country": "US", "currency": "US Dollar", "bond": "^TNX", "index": "SPY"},
    "ECB":              {"country": "EU", "currency": "Euro",     "bond": "DE10YT=RR", "index": "^STOXX50E"},
    "BOJ":              {"country": "JP", "currency": "Japanese Yen", "bond": "JP10YT=RR", "index": "^N225"},
    "PBOC":             {"country": "CN", "currency": "Chinese Yuan", "bond": "CN10YT=RR", "index": "000300.SS"},
    "BOE":              {"country": "GB", "currency": "British Pound", "bond": "BG07.L", "index": "^FTSE"},
}
for cb_name, assets in CB_ASSETS.items():
    # CB -> currency
    RELATIONSHIPS.append(("macro", cb_name, "asset_name", assets["currency"], 0.35))
    # CB -> bonds
    RELATIONSHIPS.append(("macro", cb_name, "ticker", assets["bond"], 0.30))
    # CB -> country index
    RELATIONSHIPS.append(("macro", cb_name, "ticker", assets["index"], 0.25))
    # CB -> country economy
    country_economy = f"{assets['country']} Economy"
    RELATIONSHIPS.append(("macro", cb_name, "asset_name", country_economy, 0.30))

# --- 5. Country Economy -> country index, currency, bonds ---
COUNTRY_MACRO = {
    "US": {"currency": "US Dollar", "bond": "^TNX", "index": "SPY"},
    "GB": {"currency": "British Pound", "bond": "BG07.L", "index": "^FTSE"},
    "JP": {"currency": "Japanese Yen", "bond": "JP10YT=RR", "index": "^N225"},
    "DE": {"currency": "Euro", "bond": "DE10YT=RR", "index": "^GDAXI"},
    "FR": {"currency": "Euro", "bond": "FR10YT=RR", "index": "^FCHI"},
    "CN": {"currency": "Chinese Yuan", "bond": "CN10YT=RR", "index": "000300.SS"},
    "KR": {"currency": "South Korean Won", "bond": "KR10YT=RR", "index": "^KS11"},
    "TH": {"currency": "Thai Baht", "bond": "TH10YT=RR", "index": "^SET50.BK"},
    "IN": {"currency": "Indian Rupee", "bond": "IN10YT=RR", "index": "^NSEI"},
    "EU": {"currency": "Euro", "bond": "DE10YT=RR", "index": "^STOXX50E"},
}
for country_code, assets in COUNTRY_MACRO.items():
    economy_name = f"{country_code} Economy"
    RELATIONSHIPS.append(("macro", economy_name, "asset_name", assets["currency"], 0.15))
    RELATIONSHIPS.append(("macro", economy_name, "ticker", assets["bond"], 0.15))
    RELATIONSHIPS.append(("macro", economy_name, "ticker", assets["index"], 0.20))

# --- 6. Global Economy -> major indices ---
GLOBAL_TARGETS = ["SPY", "^N225", "^STOXX50E", "000300.SS", "GLD", "DX-Y.NYB"]
for target in GLOBAL_TARGETS:
    RELATIONSHIPS.append(("macro", "Global Economy", "ticker", target, 0.15))

# --- 7. Geopolitical Risk -> defense, oil, gold ---
RISK_TARGETS = [
    ("ticker", "LMT", 0.20), ("ticker", "RTX", 0.20),
    ("ticker", "GD", 0.18), ("ticker", "NOC", 0.18),
    ("ticker", "XLE", 0.15), ("ticker", "GLD", 0.15),
    ("ticker", "BDRY", 0.12), ("ticker", "DX-Y.NYB", 0.10),
]
for tt, tk, decay in RISK_TARGETS:
    RELATIONSHIPS.append(("thematic", "Geopolitical Risk", tt, tk, decay))

# --- 8. Risk-On / Risk-Off -> broad indices ---
RISK_ON_TARGETS = [
    ("ticker", "SPY", 0.15), ("ticker", "QQQ", 0.15),
    ("ticker", "EEM", 0.12), ("ticker", "^N225", 0.10),
]
RISK_OFF_TARGETS = [
    ("ticker", "GLD", 0.20), ("ticker", "TLT", 0.20),
    ("ticker", "DX-Y.NYB", 0.15), ("ticker", "^TNX", -0.15),
]
for tt, tk, decay in RISK_ON_TARGETS:
    RELATIONSHIPS.append(("thematic", "Risk-On", tt, tk, decay))
for tt, tk, decay in RISK_OFF_TARGETS:
    RELATIONSHIPS.append(("thematic", "Risk-Off", tt, tk, decay))


# ============================================================================
# Insert
# ============================================================================
UPSERT_SQL = """
INSERT INTO yggdrasil.mimir_asset_relationships
    (source_type, source_key, target_type, target_key, decay_factor, is_active)
VALUES (%s, %s, %s, %s, %s, TRUE)
ON CONFLICT (source_type, source_key, target_type, target_key)
DO UPDATE SET decay_factor = EXCLUDED.decay_factor, updated_at = NOW();
"""


def main():
    conn = get_db_connection()
    cur = conn.cursor()

    # Ensure table exists
    cur.execute("""
        SELECT EXISTS (
            SELECT FROM information_schema.tables
            WHERE table_schema = 'yggdrasil'
              AND table_name = 'mimir_asset_relationships'
        )
    """)
    exists = cur.fetchone()[0]
    if not exists:
        print("[ERROR] mimir_asset_relationships table does not exist. Run create_sentiment_v2.sql first.")
        cur.close()
        conn.close()
        sys.exit(1)

    inserted = 0
    for row in RELATIONSHIPS:
        try:
            cur.execute(UPSERT_SQL, row)
            inserted += cur.rowcount
        except Exception as e:
            print(f"[WARN] Failed to insert {row}: {e}")

    conn.commit()
    cur.close()
    conn.close()

    print(f"[OK] Seeded {inserted} asset relationships (from {len(RELATIONSHIPS)} rows).")


if __name__ == "__main__":
    main()
