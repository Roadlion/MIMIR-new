# backend/app/analytics/user_strategies/pair_screener.py
"""
Pair Screening & Cointegration Discovery Module.

Screens asset universes (e.g. Top 10 NASDAQ, Commodities, FX, Indices)
to discover the highest-correlated and most stationary (cointegrated) pairs.
"""

from __future__ import annotations

import datetime
from typing import Sequence
import numpy as np
import pandas as pd

from .indicators import OUCalibrator, compute_adf_stat

# Curated institutional asset universes available across MT5 brokers
UNIVERSES: dict[str, list[str]] = {
    "nasdaq": [
        "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL",
        "META", "TSLA", "AVGO", "COST", "AMD"
    ],
    "commodities": [
        "XAUUSD", "XAGUSD", "WTI", "BRENT"
    ],
    "fx": [
        "EURUSD", "GBPUSD", "AUDUSD", "NZDUSD",
        "USDJPY", "USDCAD", "USDCHF"
    ],
    "indices": [
        "US500", "USTEC", "US30"
    ],
}
UNIVERSES["all"] = (
    UNIVERSES["commodities"]
    + UNIVERSES["nasdaq"]
    + UNIVERSES["indices"]
    + UNIVERSES["fx"]
)


def resolve_universe(name_or_list: str | Sequence[str]) -> list[str]:
    """Resolve a universe keyword or comma-separated string to a list of symbols."""
    if isinstance(name_or_list, str):
        key = name_or_list.strip().lower()
        if key in UNIVERSES:
            return UNIVERSES[key]
        return [s.strip().upper() for s in name_or_list.split(",") if s.strip()]
    return [str(s).strip().upper() for s in name_or_list]


def fetch_universe_prices(
    symbols: list[str],
    timeframe: str = "H1",
    bars: int = 1000,
    start_date: str | datetime.datetime | None = None,
    end_date: str | datetime.datetime | None = None,
) -> pd.DataFrame:
    """Download and synchronize close prices for all symbols in the universe."""
    try:
        import MetaTrader5 as mt5
    except ImportError as exc:
        raise RuntimeError("MetaTrader5 is not installed.") from exc

    if mt5.terminal_info() is None and not mt5.initialize():
        raise RuntimeError("Could not connect to MT5 terminal.")

    timeframe_map = {
        "M1": mt5.TIMEFRAME_M1,
        "M5": mt5.TIMEFRAME_M5,
        "M15": mt5.TIMEFRAME_M15,
        "M30": mt5.TIMEFRAME_M30,
        "H1": mt5.TIMEFRAME_H1,
        "H4": mt5.TIMEFRAME_H4,
        "D1": mt5.TIMEFRAME_D1,
        "W1": mt5.TIMEFRAME_W1,
    }
    tf_enum = timeframe_map.get(timeframe.upper(), mt5.TIMEFRAME_H1)

    prices: dict[str, pd.Series] = {}

    for sym in symbols:
        clean = sym.strip().upper()
        # Ensure symbol is active in Market Watch
        mt5.symbol_select(clean, True)
        symbol_info = mt5.symbol_info(clean)
        actual_name = clean

        if not symbol_info:
            # Try alternate naming (.US, #, etc.)
            candidates = [f"{clean}.US", f"#{clean}", clean.replace(".", "")]
            matched = next((c for c in candidates if mt5.symbol_info(c)), None)
            if matched:
                actual_name = matched
                mt5.symbol_select(actual_name, True)
            else:
                continue

        if start_date is not None:
            d_from = pd.to_datetime(start_date, utc=True).to_pydatetime()
            d_to = (
                pd.to_datetime(end_date, utc=True).to_pydatetime()
                if end_date
                else datetime.datetime.now(datetime.timezone.utc)
            )
            rates = mt5.copy_rates_range(actual_name, tf_enum, d_from, d_to)
        else:
            rates = mt5.copy_rates_from_pos(actual_name, tf_enum, 1, bars)

        if rates is not None and len(rates) >= 30:
            df = pd.DataFrame(rates)
            df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
            prices[clean] = df.set_index("time")["close"].astype(float)

    if len(prices) < 2:
        raise RuntimeError(
            f"Fewer than 2 symbols could be loaded from MT5 out of {symbols}."
        )

    # Synchronize on overlapping closed timestamps
    price_df = pd.DataFrame(prices).dropna()
    return price_df


def screen_pairs(
    symbols_or_universe: str | list[str],
    timeframe: str = "H1",
    bars: int = 1000,
    start_date: str | datetime.datetime | None = None,
    end_date: str | datetime.datetime | None = None,
    min_correlation: float = 0.60,
    max_half_life: float = 100.0,
    top_n: int = 5,
) -> pd.DataFrame:
    """Analyze all candidate pairs, calculate correlation, OLS beta, ADF stationarity,

    and OU half-life. Returns the ranked top pairs.
    """
    symbols = resolve_universe(symbols_or_universe)
    print(f"\n[SCREENER] Fetching {timeframe} data for {len(symbols)} candidate symbols...")
    price_df = fetch_universe_prices(
        symbols, timeframe=timeframe, bars=bars, start_date=start_date, end_date=end_date
    )
    print(f"[SCREENER] Synchronized {len(price_df)} bars across {len(price_df.columns)} active assets.")

    corr_matrix = price_df.corr()
    columns = list(price_df.columns)
    pair_records = []

    for i in range(len(columns)):
        for j in range(i + 1, len(columns)):
            p1, p2 = columns[i], columns[j]
            corr = float(corr_matrix.loc[p1, p2])

            if abs(corr) < min_correlation:
                continue

            s1 = np.log(price_df[p1].values)
            s2 = np.log(price_df[p2].values)

            # Fit OLS spread: s1 = alpha + beta * s2
            s2_var = np.var(s2)
            if s2_var < 1e-12:
                continue
            beta = float(np.cov(s1, s2)[0, 1] / s2_var)
            alpha = float(np.mean(s1) - beta * np.mean(s2))
            spread = s1 - alpha - beta * s2

            adf_t = compute_adf_stat(spread)
            hl = OUCalibrator.half_life(spread)

            # Composite ranking score:
            # Favors high correlation + strong rejection of unit root (more negative ADF) + reasonable half-life
            is_cointegrated = adf_t <= -2.5
            hl_score = 1.0 / (1.0 + hl) if (pd.notna(hl) and hl > 0) else 0.0
            stat_score = max(-adf_t, 0.0)
            composite_score = abs(corr) * 2.0 + stat_score + (hl_score * 10.0)

            pair_records.append({
                "primary": p1,
                "hedge": p2,
                "pair_name": f"{p1} vs {p2}",
                "correlation": corr,
                "hedge_ratio": beta,
                "adf_t_stat": adf_t,
                "half_life_bars": hl if pd.notna(hl) else np.nan,
                "cointegrated": "YES" if is_cointegrated else "WEAK",
                "score": composite_score,
            })

    if not pair_records:
        print(f"[SCREENER] No pairs met the minimum correlation threshold of {min_correlation:.2f}.")
        return pd.DataFrame()

    results_df = (
        pd.DataFrame(pair_records)
        .sort_values("score", ascending=False)
        .head(top_n)
        .reset_index(drop=True)
    )
    return results_df


def print_screened_pairs_table(df: pd.DataFrame) -> None:
    """Format and print screened pair candidates."""
    if df.empty:
        return
    print("\n" + "=" * 95)
    print("TOP SCREENED COINTEGRATED PAIRS (CORRELATION & ADF STATIONARITY)")
    print("=" * 95)
    formatted = df.copy()
    formatted["Rank"] = range(1, len(df) + 1)
    formatted["Correlation"] = formatted["correlation"].apply(lambda v: f"{v:+.4f}")
    formatted["Hedge Ratio (Beta)"] = formatted["hedge_ratio"].apply(lambda v: f"{v:.4f}")
    formatted["ADF t-stat"] = formatted["adf_t_stat"].apply(lambda v: f"{v:.2f}")
    formatted["Half-Life (bars)"] = formatted["half_life_bars"].apply(
        lambda v: f"{v:.1f}" if pd.notna(v) else "N/A"
    )
    formatted["Cointegrated?"] = formatted["cointegrated"]

    cols = [
        "Rank", "pair_name", "Correlation", "Hedge Ratio (Beta)",
        "ADF t-stat", "Half-Life (bars)", "Cointegrated?"
    ]
    display = formatted[cols].rename(columns={"pair_name": "Pair Candidate"})
    print(display.to_string(index=False))
    print("=" * 95 + "\n")
