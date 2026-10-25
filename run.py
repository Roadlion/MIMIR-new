"""Pure Python entrypoint for MIMIR algorithmic trading backtests.

Run this file directly with Python:
    python run.py

You can either:
  1. Just run `python run.py` and type your parameters (or press Enter for default 5 pairs).
  2. Or edit the CONFIGURATION section below directly in Python!

One-line input examples:
    XAUUSD XAGUSD M5 2025-01-01 2025-06-01
    XAUUSD M5 3000
    screen nasdaq
    default5
"""

import sys
from pathlib import Path

# ==============================================================================
# CONFIGURATION (Edit here, or leave empty to enter at runtime)
# ==============================================================================
SYMBOL_OR_PAIR = ""     # e.g. "XAUUSD XAGUSD", "XAUUSD", "default5", or ""
TIMEFRAME = ""          # e.g. "M5", "M15", "H1", "H4", or "" (default: M5)
BARS = 0                # e.g. 2000, 3000, 5000, or 0 (default: 2000)

# Date range (overrides BARS when set).  Format: "YYYY-MM-DD"
START_DATE = ""         # e.g. "2025-01-01"
END_DATE = ""           # e.g. "2025-06-01" (leave empty = until now)

ESTIMATOR = "ols"       # "ols" or "kalman"
COMMISSION_BPS = 0.5    # Commission in basis points (default: 0.5)
BID_ASK_SPREAD_BPS = 0.5# Slippage / spread crossing in basis points (default: 0.5)
SAVE_CSV = True         # Auto-save equity curves and trade ledgers to outputs/
# ==============================================================================

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.backtest_user_strategies import main  # noqa: E402


def run():
    # If parameters are configured at the top of the file, pass them as arguments
    cli_args = ["run.py"]

    if ESTIMATOR:
        cli_args.extend(["--estimator", str(ESTIMATOR).lower()])

    if COMMISSION_BPS is not None:
        cli_args.extend(["--commission-bps", str(COMMISSION_BPS)])

    if BID_ASK_SPREAD_BPS is not None:
        cli_args.extend(["--bid-ask-spread-bps", str(BID_ASK_SPREAD_BPS)])

    if SAVE_CSV:
        outputs_dir = PROJECT_ROOT / "outputs"
        outputs_dir.mkdir(exist_ok=True)
        cli_args.extend([
            "--output", str(outputs_dir / "latest_equity.csv"),
            "--trades-output", str(outputs_dir / "latest_orders.csv"),
        ])

    # Date range takes precedence over bars
    if START_DATE.strip():
        cli_args.extend(["--start-date", START_DATE.strip()])
        if END_DATE.strip():
            cli_args.extend(["--end-date", END_DATE.strip()])
    elif BARS > 0:
        cli_args.extend(["--bars", str(BARS)])

    if SYMBOL_OR_PAIR.strip():
        parts = SYMBOL_OR_PAIR.strip().split()
        if parts[0].lower() in ("default5", "5pairs", "5", "default", "all"):
            cli_args.extend(["--pairs", parts[0].lower()])
        elif len(parts) >= 2 and not any(parts[1].upper().startswith(t) for t in ("M", "H", "D", "W")):
            cli_args.extend([parts[0].upper(), "--hedge-symbol", parts[1].upper()])
        else:
            cli_args.append(parts[0].upper())

    if TIMEFRAME.strip():
        cli_args.extend(["--timeframe", TIMEFRAME.strip().upper()])

    sys.argv = cli_args
    main()


if __name__ == "__main__":
    run()
