# backend/app/analytics/strategy_builder.py
"""
Casino Strategy Builder & Payoff Engine
========================================
Multi-leg derivatives strategy constructor with payoff computation,
probability estimation, and position sizing.

Supports 15+ standard options strategy templates, custom multi-leg builds,
and payoff curves at any date from now to expiration.
"""
import numpy as np
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import List, Dict, Optional, Literal, Tuple
from enum import Enum


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
RISK_FREE_RATE = 0.04
CONTRACTS_MULTIPLIER = 100  # Standard options contract = 100 shares


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------
class StrategyCategory(str, Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"
    VOLATILITY = "volatility"
    INCOME = "income"
    HEDGE = "hedge"


@dataclass
class Greeks:
    delta: float = 0.0
    gamma: float = 0.0
    theta: float = 0.0  # per day
    vega: float = 0.0   # per 1% IV move
    rho: float = 0.0    # per 1% rate move

    def __add__(self, other: "Greeks") -> "Greeks":
        return Greeks(
            delta=self.delta + other.delta,
            gamma=self.gamma + other.gamma,
            theta=self.theta + other.theta,
            vega=self.vega + other.vega,
            rho=self.rho + other.rho,
        )

    def scale(self, factor: float) -> "Greeks":
        return Greeks(
            delta=self.delta * factor,
            gamma=self.gamma * factor,
            theta=self.theta * factor,
            vega=self.vega * factor,
            rho=self.rho * factor,
        )

    def to_dict(self) -> dict:
        return {
            "delta": round(self.delta, 4),
            "gamma": round(self.gamma, 4),
            "theta": round(self.theta, 4),
            "vega": round(self.vega, 4),
            "rho": round(self.rho, 4),
        }


@dataclass
class StrategyLeg:
    instrument: Literal["option", "stock", "future"] = "option"
    contract_type: Optional[Literal["call", "put"]] = None  # None for stock/future
    direction: Literal["long", "short"] = "long"
    strike: Optional[float] = None
    expiration: Optional[date] = None
    quantity: int = 1
    premium: float = 0.0  # per-share cost (mid price)
    iv: Optional[float] = None

    @property
    def direction_sign(self) -> int:
        """Returns +1 for long, -1 for short."""
        return 1 if self.direction == "long" else -1

    def intrinsic_value_at(self, underlying_price: float) -> float:
        """Calculate intrinsic value at a given underlying price."""
        if self.instrument == "stock":
            return underlying_price
        if self.instrument == "future":
            return underlying_price
        if self.contract_type == "call":
            return max(0.0, underlying_price - (self.strike or 0.0))
        elif self.contract_type == "put":
            return max(0.0, (self.strike or 0.0) - underlying_price)
        return 0.0

    def pnl_at_expiry(self, underlying_price: float) -> float:
        """P&L per share at expiration for this leg."""
        if self.instrument == "stock":
            return self.direction_sign * (underlying_price - self.premium)
        intrinsic = self.intrinsic_value_at(underlying_price)
        return self.direction_sign * (intrinsic - self.premium)

    def to_dict(self) -> dict:
        return {
            "instrument": self.instrument,
            "contract_type": self.contract_type,
            "direction": self.direction,
            "strike": self.strike,
            "expiration": self.expiration.isoformat() if self.expiration else None,
            "quantity": self.quantity,
            "premium": round(self.premium, 4),
            "iv": round(self.iv, 4) if self.iv else None,
        }


@dataclass
class PayoffPoint:
    underlying_price: float
    pnl: float  # per-unit P&L (already multiplied by direction and quantity)


@dataclass
class PayoffCurve:
    points: List[PayoffPoint]
    breakevens: List[float]
    max_profit: Optional[float]   # None = unlimited
    max_loss: Optional[float]     # None = unlimited (shown as negative)

    def to_dict(self) -> dict:
        return {
            "prices": [round(p.underlying_price, 2) for p in self.points],
            "pnl": [round(p.pnl, 2) for p in self.points],
            "breakevens": [round(b, 2) for b in self.breakevens],
            "max_profit": round(self.max_profit, 2) if self.max_profit is not None else None,
            "max_loss": round(self.max_loss, 2) if self.max_loss is not None else None,
        }


@dataclass
class Strategy:
    name: str
    category: StrategyCategory
    legs: List[StrategyLeg]
    underlying_ticker: str
    underlying_price: float
    net_premium: float = 0.0        # total debit (-) or credit (+) per unit
    max_profit: Optional[float] = None
    max_loss: Optional[float] = None
    breakeven_points: List[float] = field(default_factory=list)
    probability_of_profit: float = 0.0
    risk_reward_ratio: float = 0.0
    aggregate_greeks: Greeks = field(default_factory=Greeks)
    description: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "category": self.category.value,
            "legs": [leg.to_dict() for leg in self.legs],
            "underlying_ticker": self.underlying_ticker,
            "underlying_price": round(self.underlying_price, 2),
            "net_premium": round(self.net_premium, 4),
            "max_profit": round(self.max_profit, 2) if self.max_profit is not None else None,
            "max_loss": round(self.max_loss, 2) if self.max_loss is not None else None,
            "breakeven_points": [round(b, 2) for b in self.breakeven_points],
            "probability_of_profit": round(self.probability_of_profit, 4),
            "risk_reward_ratio": round(self.risk_reward_ratio, 4),
            "aggregate_greeks": self.aggregate_greeks.to_dict(),
            "description": self.description,
        }


@dataclass
class PositionSize:
    optimal_contracts: int
    max_loss_total: float
    capital_at_risk_pct: float
    kelly_fraction: float


# ---------------------------------------------------------------------------
# Payoff Computation Engine
# ---------------------------------------------------------------------------
def compute_payoff_at_expiry(strategy: Strategy, n_points: int = 200) -> PayoffCurve:
    """
    Compute the P&L curve at expiration for a multi-leg strategy.
    Returns a PayoffCurve with prices, P&L, breakevens, max profit/loss.
    Price range: underlying_price ± 2 standard deviations (estimated from avg IV).
    """
    price = strategy.underlying_price
    if price <= 0:
        price = 1.0

    # Estimate price range from average IV across legs (or default ±30%)
    avg_iv = _estimate_avg_iv(strategy)
    std_dev = price * avg_iv
    low = max(0.01, price - 2.5 * std_dev)
    high = price + 2.5 * std_dev

    prices = np.linspace(low, high, n_points)
    pnl_values = np.zeros(n_points)

    for leg in strategy.legs:
        for i, p in enumerate(prices):
            pnl_values[i] += leg.pnl_at_expiry(p) * leg.quantity * leg.direction_sign
            # Undo double direction_sign from pnl_at_expiry (it already includes direction)
            # Actually pnl_at_expiry already applies direction_sign, so don't multiply again

    # Recalculate correctly: pnl_at_expiry already factors in direction_sign
    pnl_values = np.zeros(n_points)
    for leg in strategy.legs:
        for i, p in enumerate(prices):
            leg_pnl = leg.pnl_at_expiry(p) * leg.quantity
            pnl_values[i] += leg_pnl

    # Scale to per-contract (100 shares)
    pnl_per_contract = pnl_values * CONTRACTS_MULTIPLIER

    # Find breakevens (zero crossings)
    breakevens = _find_zero_crossings(prices, pnl_per_contract)

    # Max profit/loss
    max_profit = float(np.max(pnl_per_contract))
    max_loss = float(np.min(pnl_per_contract))

    # Check for unlimited profit/loss (if P&L keeps increasing/decreasing at edges)
    edge_threshold = 0.01 * price
    if abs(pnl_per_contract[-1] - pnl_per_contract[-2]) > edge_threshold:
        max_profit = None  # unlimited upside
    if abs(pnl_per_contract[0] - pnl_per_contract[1]) > edge_threshold:
        max_loss = None  # unlimited downside

    points = [PayoffPoint(float(prices[i]), float(pnl_per_contract[i])) for i in range(n_points)]

    return PayoffCurve(
        points=points,
        breakevens=breakevens,
        max_profit=max_profit,
        max_loss=max_loss,
    )


def compute_payoff_at_date(
    strategy: Strategy,
    target_date: date,
    n_points: int = 200,
) -> PayoffCurve:
    """
    Compute P&L curve at a specific date before expiration using Black-Scholes
    model to account for remaining time value.
    """
    from .options_pricing import black_scholes_price, RISK_FREE_RATE

    price = strategy.underlying_price
    if price <= 0:
        price = 1.0

    avg_iv = _estimate_avg_iv(strategy)
    std_dev = price * avg_iv
    low = max(0.01, price - 2.5 * std_dev)
    high = price + 2.5 * std_dev

    prices = np.linspace(low, high, n_points)
    pnl_values = np.zeros(n_points)

    for leg in strategy.legs:
        if leg.instrument != "option" or leg.strike is None or leg.expiration is None:
            # Stock/future legs: simple linear P&L
            for i, p in enumerate(prices):
                pnl_values[i] += leg.pnl_at_expiry(p) * leg.quantity
            continue

        days_remaining = (leg.expiration - target_date).days
        T = max(days_remaining / 365.25, 0.0001)  # avoid zero
        sigma = leg.iv if leg.iv and leg.iv > 0 else avg_iv

        for i, p in enumerate(prices):
            if days_remaining <= 0:
                # At or past expiration, use intrinsic
                current_value = leg.intrinsic_value_at(p)
            else:
                current_value = black_scholes_price(
                    S=p, K=leg.strike, T=T, r=RISK_FREE_RATE,
                    sigma=sigma, option_type=leg.contract_type
                )

            leg_pnl = leg.direction_sign * (current_value - leg.premium) * leg.quantity
            pnl_values[i] += leg_pnl

    pnl_per_contract = pnl_values * CONTRACTS_MULTIPLIER
    breakevens = _find_zero_crossings(prices, pnl_per_contract)
    max_profit = float(np.max(pnl_per_contract))
    max_loss = float(np.min(pnl_per_contract))

    points = [PayoffPoint(float(prices[i]), float(pnl_per_contract[i])) for i in range(n_points)]

    return PayoffCurve(points=points, breakevens=breakevens, max_profit=max_profit, max_loss=max_loss)


def compute_payoff_surface(
    strategy: Strategy,
    n_dates: int = 5,
    n_points: int = 200,
) -> Dict[str, PayoffCurve]:
    """
    Compute payoff curves at multiple dates from now to expiration.
    Returns a dict of {date_label: PayoffCurve} for the animated time-decay slider.
    """
    # Find the earliest expiration across all legs
    expirations = [leg.expiration for leg in strategy.legs if leg.expiration]
    if not expirations:
        # No expiring legs, return just the current payoff
        return {"Now": compute_payoff_at_expiry(strategy, n_points)}

    earliest_exp = min(expirations)
    today = date.today()
    days_to_exp = (earliest_exp - today).days

    if days_to_exp <= 0:
        return {"Expiry": compute_payoff_at_expiry(strategy, n_points)}

    # Generate evenly spaced dates
    curves = {}
    for i in range(n_dates):
        days_forward = int(i * days_to_exp / (n_dates - 1)) if n_dates > 1 else 0
        target = today + timedelta(days=days_forward)

        if target >= earliest_exp:
            label = "Expiry"
            curves[label] = compute_payoff_at_expiry(strategy, n_points)
        else:
            dte = (earliest_exp - target).days
            label = f"T-{dte}" if dte > 0 else "Expiry"
            curves[label] = compute_payoff_at_date(strategy, target, n_points)

    return curves


def compute_aggregate_greeks(strategy: Strategy) -> Greeks:
    """Compute net portfolio Greeks across all option legs."""
    from .options_pricing import calculate_greeks, RISK_FREE_RATE, days_to_years

    total = Greeks()
    for leg in strategy.legs:
        if leg.instrument != "option" or leg.strike is None or leg.expiration is None:
            # Stock leg: delta = 1 per share
            if leg.instrument == "stock":
                stock_greeks = Greeks(delta=leg.direction_sign * leg.quantity)
                total = total + stock_greeks
            continue

        days_remaining = (leg.expiration - date.today()).days
        T = days_to_years(max(days_remaining, 1))
        sigma = leg.iv if leg.iv and leg.iv > 0 else 0.3  # fallback 30% IV

        greeks_dict = calculate_greeks(
            S=strategy.underlying_price,
            K=leg.strike,
            T=T,
            r=RISK_FREE_RATE,
            sigma=sigma,
            option_type=leg.contract_type,
        )

        leg_greeks = Greeks(
            delta=greeks_dict["delta"],
            gamma=greeks_dict["gamma"],
            theta=greeks_dict["theta"],
            vega=greeks_dict["vega"],
            rho=greeks_dict["rho"],
        )

        # Scale by direction and quantity
        scaled = leg_greeks.scale(leg.direction_sign * leg.quantity)
        total = total + scaled

    return total


# ---------------------------------------------------------------------------
# Probability & Sizing
# ---------------------------------------------------------------------------
def probability_of_profit(strategy: Strategy) -> float:
    """
    Estimate probability of profit using lognormal distribution.
    Assumes log-normal returns with volatility = average IV across legs.
    PoP = P(strategy P&L > 0 at expiration)
    """
    if not strategy.breakeven_points:
        payoff = compute_payoff_at_expiry(strategy)
        strategy.breakeven_points = payoff.breakevens

    if not strategy.breakeven_points:
        return 0.5  # Can't estimate

    S = strategy.underlying_price
    if S <= 0:
        return 0.5

    avg_iv = _estimate_avg_iv(strategy)
    avg_dte = _estimate_avg_dte(strategy)
    T = max(avg_dte / 365.25, 0.001)

    # For each breakeven, compute probability of being above/below it
    # using lognormal CDF
    from scipy.stats import norm

    # Determine if profit is above or below breakevens by checking payoff direction
    payoff = compute_payoff_at_expiry(strategy, n_points=50)
    pnl_at_high = payoff.points[-1].pnl

    # Simple heuristic: if P&L at high price is positive, profit is above breakevens
    if len(strategy.breakeven_points) == 1:
        be = strategy.breakeven_points[0]
        d = (np.log(be / S) - (RISK_FREE_RATE - 0.5 * avg_iv ** 2) * T) / (avg_iv * np.sqrt(T))
        if pnl_at_high > 0:
            # Profit when price > breakeven
            pop = 1.0 - float(norm.cdf(d))
        else:
            # Profit when price < breakeven
            pop = float(norm.cdf(d))
    elif len(strategy.breakeven_points) == 2:
        be_low, be_high = sorted(strategy.breakeven_points)
        # Check if profit is between or outside breakevens
        mid_price_idx = len(payoff.points) // 2
        pnl_at_mid = payoff.points[mid_price_idx].pnl

        d_low = (np.log(be_low / S) - (RISK_FREE_RATE - 0.5 * avg_iv ** 2) * T) / (avg_iv * np.sqrt(T))
        d_high = (np.log(be_high / S) - (RISK_FREE_RATE - 0.5 * avg_iv ** 2) * T) / (avg_iv * np.sqrt(T))

        if pnl_at_mid > 0:
            # Profit between breakevens (e.g., iron condor, short straddle)
            pop = float(norm.cdf(d_high) - norm.cdf(d_low))
        else:
            # Profit outside breakevens (e.g., long straddle)
            pop = 1.0 - float(norm.cdf(d_high) - norm.cdf(d_low))
    else:
        # Multiple breakevens: use Monte Carlo approximation
        pop = _monte_carlo_pop(strategy, S, avg_iv, T, n_sims=10000)

    return max(0.0, min(1.0, pop))


def kelly_criterion_size(
    strategy: Strategy,
    bankroll: float,
    max_risk_pct: float = 5.0,
) -> PositionSize:
    """
    Calculate optimal position size using Kelly Criterion, capped by max risk %.
    Kelly fraction = (p * b - q) / b
    where p = probability of profit, q = 1-p, b = reward/risk ratio
    """
    pop = strategy.probability_of_profit if strategy.probability_of_profit > 0 else probability_of_profit(strategy)
    max_loss = abs(strategy.max_loss) if strategy.max_loss is not None else abs(strategy.net_premium) * CONTRACTS_MULTIPLIER
    max_profit = strategy.max_profit if strategy.max_profit is not None else max_loss * 2  # estimate

    if max_loss <= 0:
        return PositionSize(optimal_contracts=0, max_loss_total=0, capital_at_risk_pct=0, kelly_fraction=0)

    b = max_profit / max_loss if max_loss > 0 else 1.0  # reward-to-risk
    q = 1.0 - pop
    kelly_f = (pop * b - q) / b if b > 0 else 0.0
    kelly_f = max(0.0, min(kelly_f, 0.25))  # Cap at 25% (fractional Kelly)

    # Calculate contracts from Kelly
    kelly_capital = bankroll * kelly_f
    contracts_kelly = int(kelly_capital / max_loss) if max_loss > 0 else 0

    # Also cap by max risk %
    max_risk_capital = bankroll * (max_risk_pct / 100.0)
    contracts_risk = int(max_risk_capital / max_loss) if max_loss > 0 else 0

    optimal = max(1, min(contracts_kelly, contracts_risk))
    total_risk = optimal * max_loss
    risk_pct = (total_risk / bankroll) * 100.0 if bankroll > 0 else 0.0

    return PositionSize(
        optimal_contracts=optimal,
        max_loss_total=round(total_risk, 2),
        capital_at_risk_pct=round(risk_pct, 2),
        kelly_fraction=round(kelly_f, 4),
    )


# ---------------------------------------------------------------------------
# Strategy Templates
# ---------------------------------------------------------------------------
def build_long_call(
    ticker: str, spot: float, strike: float, expiration: date,
    premium: float, iv: float = 0.3, quantity: int = 1,
) -> Strategy:
    """Long Call — unlimited upside, limited downside."""
    leg = StrategyLeg(
        instrument="option", contract_type="call", direction="long",
        strike=strike, expiration=expiration, quantity=quantity,
        premium=premium, iv=iv,
    )
    net = -premium
    max_loss = premium * CONTRACTS_MULTIPLIER * quantity
    breakeven = strike + premium

    return _finalize_strategy(
        name="Long Call", category=StrategyCategory.BULLISH,
        legs=[leg], ticker=ticker, spot=spot, net_premium=net,
        max_profit=None, max_loss=-max_loss,
        breakevens=[breakeven],
        description=f"Buy {strike}C @ ${premium:.2f}. Bullish bet with unlimited upside.",
    )


def build_long_put(
    ticker: str, spot: float, strike: float, expiration: date,
    premium: float, iv: float = 0.3, quantity: int = 1,
) -> Strategy:
    """Long Put — profit on downside, limited upside loss."""
    leg = StrategyLeg(
        instrument="option", contract_type="put", direction="long",
        strike=strike, expiration=expiration, quantity=quantity,
        premium=premium, iv=iv,
    )
    net = -premium
    max_loss = premium * CONTRACTS_MULTIPLIER * quantity
    max_profit = (strike - premium) * CONTRACTS_MULTIPLIER * quantity
    breakeven = strike - premium

    return _finalize_strategy(
        name="Long Put", category=StrategyCategory.BEARISH,
        legs=[leg], ticker=ticker, spot=spot, net_premium=net,
        max_profit=max_profit, max_loss=-max_loss,
        breakevens=[breakeven],
        description=f"Buy {strike}P @ ${premium:.2f}. Bearish bet with large downside profit potential.",
    )


def build_bull_call_spread(
    ticker: str, spot: float, long_strike: float, short_strike: float,
    expiration: date, long_premium: float, short_premium: float,
    iv: float = 0.3, quantity: int = 1,
) -> Strategy:
    """Bull Call Spread (debit) — buy lower call, sell higher call."""
    long_leg = StrategyLeg(
        instrument="option", contract_type="call", direction="long",
        strike=long_strike, expiration=expiration, quantity=quantity,
        premium=long_premium, iv=iv,
    )
    short_leg = StrategyLeg(
        instrument="option", contract_type="call", direction="short",
        strike=short_strike, expiration=expiration, quantity=quantity,
        premium=short_premium, iv=iv,
    )
    net = short_premium - long_premium  # negative = debit
    width = short_strike - long_strike
    max_profit = (width + net) * CONTRACTS_MULTIPLIER * quantity
    max_loss = net * CONTRACTS_MULTIPLIER * quantity  # net is negative for debit
    breakeven = long_strike + abs(net)

    return _finalize_strategy(
        name="Bull Call Spread", category=StrategyCategory.BULLISH,
        legs=[long_leg, short_leg], ticker=ticker, spot=spot, net_premium=net,
        max_profit=max_profit, max_loss=max_loss,
        breakevens=[breakeven],
        description=f"Buy {long_strike}C, Sell {short_strike}C. Defined-risk bullish spread.",
    )


def build_bear_put_spread(
    ticker: str, spot: float, long_strike: float, short_strike: float,
    expiration: date, long_premium: float, short_premium: float,
    iv: float = 0.3, quantity: int = 1,
) -> Strategy:
    """Bear Put Spread (debit) — buy higher put, sell lower put."""
    long_leg = StrategyLeg(
        instrument="option", contract_type="put", direction="long",
        strike=long_strike, expiration=expiration, quantity=quantity,
        premium=long_premium, iv=iv,
    )
    short_leg = StrategyLeg(
        instrument="option", contract_type="put", direction="short",
        strike=short_strike, expiration=expiration, quantity=quantity,
        premium=short_premium, iv=iv,
    )
    net = short_premium - long_premium  # negative = debit
    width = long_strike - short_strike
    max_profit = (width + net) * CONTRACTS_MULTIPLIER * quantity
    max_loss = net * CONTRACTS_MULTIPLIER * quantity
    breakeven = long_strike + net  # long_strike - |net_debit|

    return _finalize_strategy(
        name="Bear Put Spread", category=StrategyCategory.BEARISH,
        legs=[long_leg, short_leg], ticker=ticker, spot=spot, net_premium=net,
        max_profit=max_profit, max_loss=max_loss,
        breakevens=[breakeven],
        description=f"Buy {long_strike}P, Sell {short_strike}P. Defined-risk bearish spread.",
    )


def build_bull_put_spread(
    ticker: str, spot: float, short_strike: float, long_strike: float,
    expiration: date, short_premium: float, long_premium: float,
    iv: float = 0.3, quantity: int = 1,
) -> Strategy:
    """Bull Put Spread (credit) — sell higher put, buy lower put."""
    short_leg = StrategyLeg(
        instrument="option", contract_type="put", direction="short",
        strike=short_strike, expiration=expiration, quantity=quantity,
        premium=short_premium, iv=iv,
    )
    long_leg = StrategyLeg(
        instrument="option", contract_type="put", direction="long",
        strike=long_strike, expiration=expiration, quantity=quantity,
        premium=long_premium, iv=iv,
    )
    net = short_premium - long_premium  # positive = credit
    width = short_strike - long_strike
    max_profit = net * CONTRACTS_MULTIPLIER * quantity
    max_loss = -(width - net) * CONTRACTS_MULTIPLIER * quantity
    breakeven = short_strike - net

    return _finalize_strategy(
        name="Bull Put Spread", category=StrategyCategory.BULLISH,
        legs=[short_leg, long_leg], ticker=ticker, spot=spot, net_premium=net,
        max_profit=max_profit, max_loss=max_loss,
        breakevens=[breakeven],
        description=f"Sell {short_strike}P, Buy {long_strike}P. Credit spread — bullish, profit if stays above breakeven.",
    )


def build_bear_call_spread(
    ticker: str, spot: float, short_strike: float, long_strike: float,
    expiration: date, short_premium: float, long_premium: float,
    iv: float = 0.3, quantity: int = 1,
) -> Strategy:
    """Bear Call Spread (credit) — sell lower call, buy higher call."""
    short_leg = StrategyLeg(
        instrument="option", contract_type="call", direction="short",
        strike=short_strike, expiration=expiration, quantity=quantity,
        premium=short_premium, iv=iv,
    )
    long_leg = StrategyLeg(
        instrument="option", contract_type="call", direction="long",
        strike=long_strike, expiration=expiration, quantity=quantity,
        premium=long_premium, iv=iv,
    )
    net = short_premium - long_premium  # positive = credit
    width = long_strike - short_strike
    max_profit = net * CONTRACTS_MULTIPLIER * quantity
    max_loss = -(width - net) * CONTRACTS_MULTIPLIER * quantity
    breakeven = short_strike + net

    return _finalize_strategy(
        name="Bear Call Spread", category=StrategyCategory.BEARISH,
        legs=[short_leg, long_leg], ticker=ticker, spot=spot, net_premium=net,
        max_profit=max_profit, max_loss=max_loss,
        breakevens=[breakeven],
        description=f"Sell {short_strike}C, Buy {long_strike}C. Credit spread — bearish, profit if stays below breakeven.",
    )


def build_long_straddle(
    ticker: str, spot: float, strike: float, expiration: date,
    call_premium: float, put_premium: float, iv: float = 0.3, quantity: int = 1,
) -> Strategy:
    """Long Straddle — profit on large move in either direction."""
    call_leg = StrategyLeg(
        instrument="option", contract_type="call", direction="long",
        strike=strike, expiration=expiration, quantity=quantity,
        premium=call_premium, iv=iv,
    )
    put_leg = StrategyLeg(
        instrument="option", contract_type="put", direction="long",
        strike=strike, expiration=expiration, quantity=quantity,
        premium=put_premium, iv=iv,
    )
    total_premium = call_premium + put_premium
    net = -total_premium
    max_loss = total_premium * CONTRACTS_MULTIPLIER * quantity
    be_upper = strike + total_premium
    be_lower = strike - total_premium

    return _finalize_strategy(
        name="Long Straddle", category=StrategyCategory.VOLATILITY,
        legs=[call_leg, put_leg], ticker=ticker, spot=spot, net_premium=net,
        max_profit=None, max_loss=-max_loss,
        breakevens=[be_lower, be_upper],
        description=f"Buy {strike}C + {strike}P. Profit on volatility — big move in either direction.",
    )


def build_short_straddle(
    ticker: str, spot: float, strike: float, expiration: date,
    call_premium: float, put_premium: float, iv: float = 0.3, quantity: int = 1,
) -> Strategy:
    """Short Straddle — profit if price stays near strike."""
    call_leg = StrategyLeg(
        instrument="option", contract_type="call", direction="short",
        strike=strike, expiration=expiration, quantity=quantity,
        premium=call_premium, iv=iv,
    )
    put_leg = StrategyLeg(
        instrument="option", contract_type="put", direction="short",
        strike=strike, expiration=expiration, quantity=quantity,
        premium=put_premium, iv=iv,
    )
    total_premium = call_premium + put_premium
    net = total_premium
    max_profit = total_premium * CONTRACTS_MULTIPLIER * quantity
    be_upper = strike + total_premium
    be_lower = strike - total_premium

    return _finalize_strategy(
        name="Short Straddle", category=StrategyCategory.NEUTRAL,
        legs=[call_leg, put_leg], ticker=ticker, spot=spot, net_premium=net,
        max_profit=max_profit, max_loss=None,
        breakevens=[be_lower, be_upper],
        description=f"Sell {strike}C + {strike}P. Income play — profit if price stays near {strike}.",
    )


def build_long_strangle(
    ticker: str, spot: float, put_strike: float, call_strike: float,
    expiration: date, call_premium: float, put_premium: float,
    iv: float = 0.3, quantity: int = 1,
) -> Strategy:
    """Long Strangle — cheaper vol play than straddle, wider breakevens."""
    call_leg = StrategyLeg(
        instrument="option", contract_type="call", direction="long",
        strike=call_strike, expiration=expiration, quantity=quantity,
        premium=call_premium, iv=iv,
    )
    put_leg = StrategyLeg(
        instrument="option", contract_type="put", direction="long",
        strike=put_strike, expiration=expiration, quantity=quantity,
        premium=put_premium, iv=iv,
    )
    total_premium = call_premium + put_premium
    net = -total_premium
    max_loss = total_premium * CONTRACTS_MULTIPLIER * quantity
    be_upper = call_strike + total_premium
    be_lower = put_strike - total_premium

    return _finalize_strategy(
        name="Long Strangle", category=StrategyCategory.VOLATILITY,
        legs=[call_leg, put_leg], ticker=ticker, spot=spot, net_premium=net,
        max_profit=None, max_loss=-max_loss,
        breakevens=[be_lower, be_upper],
        description=f"Buy {call_strike}C + {put_strike}P OTM. Volatility bet — cheaper than straddle.",
    )


def build_short_strangle(
    ticker: str, spot: float, put_strike: float, call_strike: float,
    expiration: date, call_premium: float, put_premium: float,
    iv: float = 0.3, quantity: int = 1,
) -> Strategy:
    """Short Strangle — income play, profit if price stays in range."""
    call_leg = StrategyLeg(
        instrument="option", contract_type="call", direction="short",
        strike=call_strike, expiration=expiration, quantity=quantity,
        premium=call_premium, iv=iv,
    )
    put_leg = StrategyLeg(
        instrument="option", contract_type="put", direction="short",
        strike=put_strike, expiration=expiration, quantity=quantity,
        premium=put_premium, iv=iv,
    )
    total_premium = call_premium + put_premium
    net = total_premium
    max_profit = total_premium * CONTRACTS_MULTIPLIER * quantity
    be_upper = call_strike + total_premium
    be_lower = put_strike - total_premium

    return _finalize_strategy(
        name="Short Strangle", category=StrategyCategory.NEUTRAL,
        legs=[call_leg, put_leg], ticker=ticker, spot=spot, net_premium=net,
        max_profit=max_profit, max_loss=None,
        breakevens=[be_lower, be_upper],
        description=f"Sell {call_strike}C + {put_strike}P OTM. Income — profit if price stays between {put_strike}-{call_strike}.",
    )


def build_iron_condor(
    ticker: str, spot: float,
    put_long_strike: float, put_short_strike: float,
    call_short_strike: float, call_long_strike: float,
    expiration: date,
    put_long_prem: float, put_short_prem: float,
    call_short_prem: float, call_long_prem: float,
    iv: float = 0.3, quantity: int = 1,
) -> Strategy:
    """Iron Condor — defined-risk neutral strategy. Profit if price stays in range."""
    legs = [
        StrategyLeg("option", "put", "long", put_long_strike, expiration, quantity, put_long_prem, iv),
        StrategyLeg("option", "put", "short", put_short_strike, expiration, quantity, put_short_prem, iv),
        StrategyLeg("option", "call", "short", call_short_strike, expiration, quantity, call_short_prem, iv),
        StrategyLeg("option", "call", "long", call_long_strike, expiration, quantity, call_long_prem, iv),
    ]
    net_credit = (put_short_prem + call_short_prem) - (put_long_prem + call_long_prem)
    put_width = put_short_strike - put_long_strike
    call_width = call_long_strike - call_short_strike
    max_width = max(put_width, call_width)
    max_loss = (max_width - net_credit) * CONTRACTS_MULTIPLIER * quantity
    max_profit = net_credit * CONTRACTS_MULTIPLIER * quantity
    be_lower = put_short_strike - net_credit
    be_upper = call_short_strike + net_credit

    return _finalize_strategy(
        name="Iron Condor", category=StrategyCategory.NEUTRAL,
        legs=legs, ticker=ticker, spot=spot, net_premium=net_credit,
        max_profit=max_profit, max_loss=-max_loss,
        breakevens=[be_lower, be_upper],
        description=f"Iron Condor: {put_long_strike}/{put_short_strike}P — {call_short_strike}/{call_long_strike}C. Range-bound play.",
    )


def build_iron_butterfly(
    ticker: str, spot: float, center_strike: float,
    put_wing: float, call_wing: float,
    expiration: date,
    center_call_prem: float, center_put_prem: float,
    wing_put_prem: float, wing_call_prem: float,
    iv: float = 0.3, quantity: int = 1,
) -> Strategy:
    """Iron Butterfly — tighter range than condor, higher premium collected."""
    legs = [
        StrategyLeg("option", "put", "long", put_wing, expiration, quantity, wing_put_prem, iv),
        StrategyLeg("option", "put", "short", center_strike, expiration, quantity, center_put_prem, iv),
        StrategyLeg("option", "call", "short", center_strike, expiration, quantity, center_call_prem, iv),
        StrategyLeg("option", "call", "long", call_wing, expiration, quantity, wing_call_prem, iv),
    ]
    net_credit = (center_call_prem + center_put_prem) - (wing_put_prem + wing_call_prem)
    width = center_strike - put_wing  # assume symmetric
    max_loss = (width - net_credit) * CONTRACTS_MULTIPLIER * quantity
    max_profit = net_credit * CONTRACTS_MULTIPLIER * quantity
    be_lower = center_strike - net_credit
    be_upper = center_strike + net_credit

    return _finalize_strategy(
        name="Iron Butterfly", category=StrategyCategory.NEUTRAL,
        legs=legs, ticker=ticker, spot=spot, net_premium=net_credit,
        max_profit=max_profit, max_loss=-max_loss,
        breakevens=[be_lower, be_upper],
        description=f"Iron Butterfly @ {center_strike}. Tighter range, higher premium.",
    )


def build_covered_call(
    ticker: str, spot: float, strike: float, expiration: date,
    premium: float, iv: float = 0.3, quantity: int = 1,
) -> Strategy:
    """Covered Call — own stock + sell call. Income strategy."""
    stock_leg = StrategyLeg(
        instrument="stock", contract_type=None, direction="long",
        strike=None, expiration=None, quantity=quantity * 100,
        premium=spot, iv=None,
    )
    call_leg = StrategyLeg(
        instrument="option", contract_type="call", direction="short",
        strike=strike, expiration=expiration, quantity=quantity,
        premium=premium, iv=iv,
    )
    net = premium  # credit from selling call
    max_profit = (strike - spot + premium) * CONTRACTS_MULTIPLIER * quantity
    max_loss = -(spot - premium) * CONTRACTS_MULTIPLIER * quantity  # stock goes to zero
    breakeven = spot - premium

    return _finalize_strategy(
        name="Covered Call", category=StrategyCategory.INCOME,
        legs=[stock_leg, call_leg], ticker=ticker, spot=spot, net_premium=net,
        max_profit=max_profit, max_loss=max_loss,
        breakevens=[breakeven],
        description=f"Own {ticker} + Sell {strike}C @ ${premium:.2f}. Income from premium, capped upside.",
    )


def build_protective_put(
    ticker: str, spot: float, strike: float, expiration: date,
    premium: float, iv: float = 0.3, quantity: int = 1,
) -> Strategy:
    """Protective Put — own stock + buy put. Hedge strategy."""
    stock_leg = StrategyLeg(
        instrument="stock", contract_type=None, direction="long",
        strike=None, expiration=None, quantity=quantity * 100,
        premium=spot, iv=None,
    )
    put_leg = StrategyLeg(
        instrument="option", contract_type="put", direction="long",
        strike=strike, expiration=expiration, quantity=quantity,
        premium=premium, iv=iv,
    )
    net = -premium
    max_loss = -(spot - strike + premium) * CONTRACTS_MULTIPLIER * quantity
    breakeven = spot + premium

    return _finalize_strategy(
        name="Protective Put", category=StrategyCategory.HEDGE,
        legs=[stock_leg, put_leg], ticker=ticker, spot=spot, net_premium=net,
        max_profit=None, max_loss=max_loss,
        breakevens=[breakeven],
        description=f"Own {ticker} + Buy {strike}P @ ${premium:.2f}. Insurance against downside.",
    )


def build_collar(
    ticker: str, spot: float, put_strike: float, call_strike: float,
    expiration: date, put_premium: float, call_premium: float,
    iv: float = 0.3, quantity: int = 1,
) -> Strategy:
    """Collar — own stock + buy put + sell call. Defined-risk hedge."""
    stock_leg = StrategyLeg(
        instrument="stock", contract_type=None, direction="long",
        strike=None, expiration=None, quantity=quantity * 100,
        premium=spot, iv=None,
    )
    put_leg = StrategyLeg(
        instrument="option", contract_type="put", direction="long",
        strike=put_strike, expiration=expiration, quantity=quantity,
        premium=put_premium, iv=iv,
    )
    call_leg = StrategyLeg(
        instrument="option", contract_type="call", direction="short",
        strike=call_strike, expiration=expiration, quantity=quantity,
        premium=call_premium, iv=iv,
    )
    net = call_premium - put_premium
    max_profit = (call_strike - spot + net) * CONTRACTS_MULTIPLIER * quantity
    max_loss = -(spot - put_strike - net) * CONTRACTS_MULTIPLIER * quantity
    breakeven = spot - net

    return _finalize_strategy(
        name="Collar", category=StrategyCategory.HEDGE,
        legs=[stock_leg, put_leg, call_leg], ticker=ticker, spot=spot,
        net_premium=net, max_profit=max_profit, max_loss=max_loss,
        breakevens=[breakeven],
        description=f"Own {ticker} + Buy {put_strike}P + Sell {call_strike}C. Defined range hedge.",
    )


def build_cash_secured_put(
    ticker: str, spot: float, strike: float, expiration: date,
    premium: float, iv: float = 0.3, quantity: int = 1,
) -> Strategy:
    """Cash-Secured Put — sell put with cash reserved. Income + acquisition strategy."""
    leg = StrategyLeg(
        instrument="option", contract_type="put", direction="short",
        strike=strike, expiration=expiration, quantity=quantity,
        premium=premium, iv=iv,
    )
    net = premium
    max_profit = premium * CONTRACTS_MULTIPLIER * quantity
    max_loss = -(strike - premium) * CONTRACTS_MULTIPLIER * quantity
    breakeven = strike - premium

    return _finalize_strategy(
        name="Cash-Secured Put", category=StrategyCategory.INCOME,
        legs=[leg], ticker=ticker, spot=spot, net_premium=net,
        max_profit=max_profit, max_loss=max_loss,
        breakevens=[breakeven],
        description=f"Sell {strike}P @ ${premium:.2f}. Income play — willing to buy at {breakeven:.2f}.",
    )


def build_calendar_spread(
    ticker: str, spot: float, strike: float,
    near_expiration: date, far_expiration: date,
    near_premium: float, far_premium: float,
    contract_type: str = "call", iv: float = 0.3, quantity: int = 1,
) -> Strategy:
    """Calendar Spread — sell near-term, buy far-term at same strike."""
    near_leg = StrategyLeg(
        instrument="option", contract_type=contract_type, direction="short",
        strike=strike, expiration=near_expiration, quantity=quantity,
        premium=near_premium, iv=iv,
    )
    far_leg = StrategyLeg(
        instrument="option", contract_type=contract_type, direction="long",
        strike=strike, expiration=far_expiration, quantity=quantity,
        premium=far_premium, iv=iv,
    )
    net = near_premium - far_premium  # usually negative (debit)

    return _finalize_strategy(
        name="Calendar Spread", category=StrategyCategory.INCOME,
        legs=[near_leg, far_leg], ticker=ticker, spot=spot, net_premium=net,
        max_profit=None, max_loss=net * CONTRACTS_MULTIPLIER * quantity,
        breakevens=[strike],  # approximate
        description=f"Sell near-term {strike}{contract_type[0].upper()}, Buy far-term {strike}{contract_type[0].upper()}. Theta decay play.",
    )


def build_custom_strategy(
    ticker: str,
    spot: float,
    legs: List[StrategyLeg],
    name: str = "Custom Strategy",
) -> Strategy:
    """Build a custom multi-leg strategy from user-defined legs."""
    # Calculate net premium
    net = sum(leg.direction_sign * leg.premium * leg.quantity for leg in legs
              if leg.instrument == "option")

    strategy = Strategy(
        name=name,
        category=StrategyCategory.NEUTRAL,  # user can reclassify
        legs=legs,
        underlying_ticker=ticker,
        underlying_price=spot,
        net_premium=net,
        description="Custom user-defined strategy.",
    )

    # Compute payoff to fill in max profit/loss/breakevens
    payoff = compute_payoff_at_expiry(strategy)
    strategy.max_profit = payoff.max_profit
    strategy.max_loss = payoff.max_loss
    strategy.breakeven_points = payoff.breakevens
    strategy.probability_of_profit = probability_of_profit(strategy)
    strategy.aggregate_greeks = compute_aggregate_greeks(strategy)

    # Risk-reward ratio
    if strategy.max_loss and strategy.max_loss != 0 and strategy.max_profit:
        strategy.risk_reward_ratio = abs(strategy.max_profit / strategy.max_loss)

    return strategy


# ---------------------------------------------------------------------------
# Strategy Template Registry
# ---------------------------------------------------------------------------
STRATEGY_TEMPLATES = {
    "long_call": {"name": "Long Call", "category": "bullish", "legs": 1, "description": "Buy a call. Unlimited upside, limited risk."},
    "long_put": {"name": "Long Put", "category": "bearish", "legs": 1, "description": "Buy a put. Large downside profit, limited risk."},
    "bull_call_spread": {"name": "Bull Call Spread", "category": "bullish", "legs": 2, "description": "Buy lower call, sell higher call. Debit spread."},
    "bear_put_spread": {"name": "Bear Put Spread", "category": "bearish", "legs": 2, "description": "Buy higher put, sell lower put. Debit spread."},
    "bull_put_spread": {"name": "Bull Put Spread", "category": "bullish", "legs": 2, "description": "Sell higher put, buy lower put. Credit spread."},
    "bear_call_spread": {"name": "Bear Call Spread", "category": "bearish", "legs": 2, "description": "Sell lower call, buy higher call. Credit spread."},
    "long_straddle": {"name": "Long Straddle", "category": "volatility", "legs": 2, "description": "Buy ATM call + put. Profit on big moves."},
    "short_straddle": {"name": "Short Straddle", "category": "neutral", "legs": 2, "description": "Sell ATM call + put. Profit if range-bound."},
    "long_strangle": {"name": "Long Strangle", "category": "volatility", "legs": 2, "description": "Buy OTM call + put. Cheaper vol bet."},
    "short_strangle": {"name": "Short Strangle", "category": "neutral", "legs": 2, "description": "Sell OTM call + put. Income if range-bound."},
    "iron_condor": {"name": "Iron Condor", "category": "neutral", "legs": 4, "description": "Defined-risk range play. 4-leg credit strategy."},
    "iron_butterfly": {"name": "Iron Butterfly", "category": "neutral", "legs": 4, "description": "Tighter range than condor, higher premium."},
    "covered_call": {"name": "Covered Call", "category": "income", "legs": 2, "description": "Own stock + sell call. Income strategy."},
    "protective_put": {"name": "Protective Put", "category": "hedge", "legs": 2, "description": "Own stock + buy put. Downside insurance."},
    "collar": {"name": "Collar", "category": "hedge", "legs": 3, "description": "Own stock + buy put + sell call. Defined range."},
    "cash_secured_put": {"name": "Cash-Secured Put", "category": "income", "legs": 1, "description": "Sell put with cash. Income + acquisition."},
    "calendar_spread": {"name": "Calendar Spread", "category": "income", "legs": 2, "description": "Sell near, buy far at same strike. Theta play."},
}


# ---------------------------------------------------------------------------
# Internal Helpers
# ---------------------------------------------------------------------------
def _finalize_strategy(
    name: str, category: StrategyCategory, legs: List[StrategyLeg],
    ticker: str, spot: float, net_premium: float,
    max_profit: Optional[float], max_loss: Optional[float],
    breakevens: List[float], description: str = "",
) -> Strategy:
    """Fill in computed fields and return a complete Strategy object."""
    strategy = Strategy(
        name=name,
        category=category,
        legs=legs,
        underlying_ticker=ticker,
        underlying_price=spot,
        net_premium=net_premium,
        max_profit=max_profit,
        max_loss=max_loss,
        breakeven_points=breakevens,
        description=description,
    )

    # Probability of profit
    strategy.probability_of_profit = probability_of_profit(strategy)

    # Aggregate Greeks
    try:
        strategy.aggregate_greeks = compute_aggregate_greeks(strategy)
    except Exception:
        strategy.aggregate_greeks = Greeks()

    # Risk/reward ratio
    if max_loss and max_loss != 0 and max_profit:
        strategy.risk_reward_ratio = abs(max_profit / max_loss)

    return strategy


def _estimate_avg_iv(strategy: Strategy) -> float:
    """Estimate average implied volatility from strategy legs."""
    ivs = [leg.iv for leg in strategy.legs if leg.iv and leg.iv > 0]
    return float(np.mean(ivs)) if ivs else 0.3  # default 30%


def _estimate_avg_dte(strategy: Strategy) -> int:
    """Estimate average days to expiry from strategy legs."""
    today = date.today()
    dtes = [(leg.expiration - today).days for leg in strategy.legs
            if leg.expiration and (leg.expiration - today).days > 0]
    return int(np.mean(dtes)) if dtes else 30


def _find_zero_crossings(x: np.ndarray, y: np.ndarray) -> List[float]:
    """Find x values where y crosses zero via linear interpolation."""
    crossings = []
    for i in range(len(y) - 1):
        if y[i] * y[i + 1] < 0:  # sign change
            # Linear interpolation
            x_cross = x[i] - y[i] * (x[i + 1] - x[i]) / (y[i + 1] - y[i])
            crossings.append(float(x_cross))
        elif y[i] == 0:
            crossings.append(float(x[i]))
    return crossings


def _monte_carlo_pop(strategy: Strategy, S: float, sigma: float, T: float, n_sims: int = 10000) -> float:
    """Monte Carlo estimation of probability of profit for complex strategies."""
    np.random.seed(42)
    z = np.random.standard_normal(n_sims)
    simulated_prices = S * np.exp((RISK_FREE_RATE - 0.5 * sigma ** 2) * T + sigma * np.sqrt(T) * z)

    profitable = 0
    for sim_price in simulated_prices:
        total_pnl = 0.0
        for leg in strategy.legs:
            total_pnl += leg.pnl_at_expiry(sim_price) * leg.quantity
        if total_pnl > 0:
            profitable += 1

    return profitable / n_sims
