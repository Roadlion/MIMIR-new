# backend/app/utils/ticker_validator.py
"""
MIMIR Ticker Validator
=======================
Validates whether a ticker symbol is a real equity/ETF/crypto/futures
ticker that yfinance can handle — and filters out structured products,
ISINs, and other non-standard identifiers that cause 404 noise.

The main culprit is mimir_dynamic_tickers picking up ISIN-formatted
securities like DE000SLA3WW5.SG which yfinance cannot service.

Public API:
    is_yfinance_compatible(ticker)   → bool
    filter_yfinance_tickers(tickers) → List[str]  (keeps only valid ones)
"""

import re
from typing import List

# ── Pattern: what a valid yfinance ticker looks like ─────────────────────────
#
# US equities:         AAPL, MSFT, BRK.B, BF.A  (1-5 letters, optional .B suffix)
# US ETFs:             SPY, QQQ, ARKK            (same as equities)
# International:       CPALL.BK, 005930.KS, 9988.HK, RELIANCE.NS
# Crypto (yf):         BTC-USD, ETH-USD, BNB-USD
# Forex:               EURUSD=X, JPY=X, DXY
# Futures:             GC=F, CL=F, ES=F
# Indices:             ^GSPC, ^VIX, ^IXIC
# ADRs:                BABA, NIO, TSM            (same as US)
#
# NOT valid for yfinance:
#   ISIN codes:        DE000SLA3WW5.SG  (12-char ISIN + exchange suffix)
#   Full ISINs:        US0378331005
#   Structured:        XS1234567890, LU0123456789
#   ISIN country prefixes that appear in dynamic tickers:
#       DE, US, GB, FR, LU, XS, IE, CH, AU, JP, HK, SG, NL, IT, AT

# Match ISIN-like tickers: 2 letter country code + 10 alphanumeric = 12 chars
# Optionally followed by .XX exchange suffix
_ISIN_PATTERN = re.compile(
    r'^[A-Z]{2}[0-9A-Z]{10}(\.[A-Z]{1,4})?$',
    re.IGNORECASE
)

# Valid yfinance ticker patterns
_VALID_PATTERNS = [
    # Standard US/international equity: 1-6 letters, optional .B, .A suffix
    re.compile(r'^[A-Z]{1,6}(\.[A-Z]{1,2})?$'),
    # International with numeric component: 005930.KS, 9988.HK, 1234.T
    re.compile(r'^[0-9]{1,6}\.[A-Z]{1,3}$'),
    # International equity with letters+numbers: RELIANCE.NS, TATAPOWER.NS
    re.compile(r'^[A-Z0-9&]{1,20}\.[A-Z]{2,3}$'),
    # Crypto: BTC-USD, ETH-USD
    re.compile(r'^[A-Z]{2,8}-[A-Z]{2,4}$'),
    # Forex / futures index: EURUSD=X, GC=F, ^GSPC, ^VIX
    re.compile(r'^(\^|)[A-Z0-9]{1,10}(=X|=F)?$'),
]

# Explicit ISIN country prefixes — if ticker starts with these 2 letters
# followed by digits/uppercase, very likely an ISIN
_ISIN_COUNTRY_PREFIXES = {
    'DE', 'US', 'GB', 'FR', 'LU', 'XS', 'IE', 'CH', 'AU', 'JP',
    'HK', 'SG', 'NL', 'IT', 'AT', 'BE', 'DK', 'FI', 'NO', 'SE',
    'ES', 'PT', 'CA', 'MX', 'BR', 'KR', 'IN', 'TW', 'ID', 'TH',
}


def is_yfinance_compatible(ticker: str) -> bool:
    """
    Returns True if the ticker is likely a valid yfinance-compatible symbol.
    Filters out ISINs, structured products, and other non-standard identifiers.
    """
    if not ticker or not isinstance(ticker, str):
        return False

    t = ticker.strip().upper()

    if len(t) < 1 or len(t) > 25:
        return False

    # ── Hard reject: ISIN pattern ──────────────────────────────────────────
    # ISINs are exactly 12 chars: 2 letter country code + 10 alphanumeric
    # Often come with an exchange suffix like .SG, .DE, .FR
    if _ISIN_PATTERN.match(t):
        return False

    # Additional check: 2-letter country prefix + 10 chars (even without suffix)
    base = t.split('.')[0]  # strip exchange suffix for this check
    if len(base) == 12 and base[:2] in _ISIN_COUNTRY_PREFIXES and base[2:].isalnum():
        return False

    # ── Hard reject: pure numeric tickers with no exchange suffix ──────────
    # E.g. "1234567890" — no exchange means nothing
    if base.isdigit() and '.' not in t:
        return False

    # ── Check against known valid patterns ─────────────────────────────────
    for pattern in _VALID_PATTERNS:
        if pattern.match(t):
            return True

    # Default reject for anything that doesn't match known patterns
    return False


def filter_yfinance_tickers(tickers: List[str]) -> List[str]:
    """
    Filters a list of tickers to only those that are yfinance-compatible.
    Logs a single summary line for any rejected tickers.
    """
    valid = []
    rejected = []
    for t in tickers:
        if is_yfinance_compatible(t):
            valid.append(t)
        else:
            rejected.append(t)

    if rejected:
        import logging
        logger = logging.getLogger(__name__)
        logger.debug(
            f"[TICKER_VALIDATOR] Filtered out {len(rejected)} non-yfinance tickers: "
            f"{rejected[:10]}{'...' if len(rejected) > 10 else ''}"
        )

    return valid
