# backend/app/analytics/options_pricing.py

import numpy as np
from scipy.stats import norm
from typing import Optional, Dict, List

RISK_FREE_RATE = 0.04

def days_to_years(days: int) -> float:
    """
    Convert calendar days to trading years (days / 365.25).
    """
    return days / 365.25

def _d1(S: float, K: float, T: float, r: float, sigma: float) -> float:
    """Private helper to calculate d1 for Black-Scholes."""
    return (np.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * np.sqrt(T))

def _d2(S: float, K: float, T: float, r: float, sigma: float) -> float:
    """Private helper to calculate d2 for Black-Scholes."""
    return _d1(S, K, T, r, sigma) - sigma * np.sqrt(T)

def black_scholes_price(S: float, K: float, T: float, r: float, sigma: float, option_type: str) -> float:
    """
    Standard BS model for European-style options.
    
    Args:
        S: Spot price
        K: Strike price
        T: Time to expiry in years
        r: Risk-free rate
        sigma: Volatility
        option_type: 'call' or 'put'
    """
    option_type = option_type.lower()
    
    if S <= 0 or K <= 0:
        return 0.0
    
    if T <= 0:
        if option_type == 'call':
            return max(0.0, float(S - K))
        elif option_type == 'put':
            return max(0.0, float(K - S))
        else:
            raise ValueError(f"Invalid option type: {option_type}")
            
    if sigma <= 0:
        if option_type == 'call':
            return max(0.0, float(S - K * np.exp(-r * T)))
        elif option_type == 'put':
            return max(0.0, float(K * np.exp(-r * T) - S))
        else:
            raise ValueError(f"Invalid option type: {option_type}")
            
    d1 = _d1(S, K, T, r, sigma)
    d2 = _d2(S, K, T, r, sigma)
    
    if option_type == 'call':
        return float(S * norm.cdf(d1) - K * np.exp(-r * T) * norm.cdf(d2))
    elif option_type == 'put':
        return float(K * np.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1))
    else:
        raise ValueError(f"Invalid option type: {option_type}")

def binomial_price(S: float, K: float, T: float, r: float, sigma: float, n_steps: int = 200, option_type: str = 'call', style: str = 'american') -> float:
    """
    Cox-Ross-Rubinstein binomial tree.
    
    Supports 'american' and 'european' styles.
    Vectorized tree computation.
    """
    option_type = option_type.lower()
    style = style.lower()
    
    if S <= 0 or K <= 0 or T <= 0 or sigma <= 0:
        return black_scholes_price(S, K, T, r, sigma, option_type)
        
    dt = T / n_steps
    u = np.exp(sigma * np.sqrt(dt))
    d = 1.0 / u
    p = (np.exp(r * dt) - d) / (u - d)
    
    # Initialize asset prices at maturity
    prices = np.zeros(n_steps + 1)
    for i in range(n_steps + 1):
        prices[i] = S * (u ** (n_steps - i)) * (d ** i)
        
    # Initialize option values at maturity
    values = np.zeros(n_steps + 1)
    if option_type == 'call':
        values = np.maximum(0.0, prices - K)
    elif option_type == 'put':
        values = np.maximum(0.0, K - prices)
    else:
        raise ValueError(f"Invalid option type: {option_type}")
        
    # Step backwards through tree
    discount = np.exp(-r * dt)
    for j in range(n_steps - 1, -1, -1):
        # Update values for current step using vectorized ops
        values[:j + 1] = discount * (p * values[:j + 1] + (1 - p) * values[1:j + 2])
        if style == 'american':
            # Calculate current asset prices
            current_prices = S * (u ** np.arange(j, -1, -1)) * (d ** np.arange(0, j + 1))
            if option_type == 'call':
                values[:j + 1] = np.maximum(values[:j + 1], current_prices - K)
            else:
                values[:j + 1] = np.maximum(values[:j + 1], K - current_prices)
                    
    return float(values[0])

def calculate_greeks(S: float, K: float, T: float, r: float, sigma: float, option_type: str) -> Dict[str, float]:
    """
    Analytical, closed-form BS Greeks.
    
    Returns dict with keys: 'delta', 'gamma', 'theta', 'vega', 'rho'
    """
    option_type = option_type.lower()
    
    if S <= 0 or K <= 0:
        return {'delta': 0.0, 'gamma': 0.0, 'theta': 0.0, 'vega': 0.0, 'rho': 0.0}
        
    if T <= 0:
        is_itm = (S > K) if option_type == 'call' else (K > S)
        if is_itm:
            delta = 1.0 if option_type == 'call' else -1.0
        else:
            delta = 0.0
        return {'delta': delta, 'gamma': 0.0, 'theta': 0.0, 'vega': 0.0, 'rho': 0.0}
        
    if sigma <= 0:
        sigma = 1e-8
        
    d1 = _d1(S, K, T, r, sigma)
    d2 = _d2(S, K, T, r, sigma)
    
    if option_type == 'call':
        delta = norm.cdf(d1)
        rho = (K * T * np.exp(-r * T) * norm.cdf(d2)) / 100.0
        theta_annual = - (S * norm.pdf(d1) * sigma) / (2 * np.sqrt(T)) - r * K * np.exp(-r * T) * norm.cdf(d2)
    elif option_type == 'put':
        delta = norm.cdf(d1) - 1.0
        rho = (-K * T * np.exp(-r * T) * norm.cdf(-d2)) / 100.0
        theta_annual = - (S * norm.pdf(d1) * sigma) / (2 * np.sqrt(T)) + r * K * np.exp(-r * T) * norm.cdf(-d2)
    else:
        raise ValueError(f"Invalid option type: {option_type}")
        
    gamma = norm.pdf(d1) / (S * sigma * np.sqrt(T))
    vega = (S * norm.pdf(d1) * np.sqrt(T)) / 100.0
    theta = theta_annual / 365.0
    
    return {
        'delta': float(delta),
        'gamma': float(gamma),
        'theta': float(theta),
        'vega': float(vega),
        'rho': float(rho)
    }

def implied_volatility_solve(market_price: float, S: float, K: float, T: float, r: float, option_type: str, max_iterations: int = 100, tolerance: float = 1e-6) -> Optional[float]:
    """
    Newton-Raphson using vega as derivative to solve for implied volatility.
    Falls back to bisection method if Newton fails to converge.
    """
    option_type = option_type.lower()
    
    # Check intrinsic value
    if option_type == 'call':
        intrinsic = max(0.0, S - K * np.exp(-r * T))
    elif option_type == 'put':
        intrinsic = max(0.0, K * np.exp(-r * T) - S)
    else:
        raise ValueError(f"Invalid option type: {option_type}")
        
    if market_price < intrinsic:
        return None
        
    if T <= 0 or S <= 0 or K <= 0:
        return None
        
    sigma = 0.5 # initial guess
    
    # Newton-Raphson
    for _ in range(max_iterations):
        price = black_scholes_price(S, K, T, r, sigma, option_type)
        diff = price - market_price
        
        if abs(diff) < tolerance:
            return float(sigma)
            
        greeks = calculate_greeks(S, K, T, r, sigma, option_type)
        vega = greeks['vega'] * 100.0 # Convert back to derivative scale
        
        if abs(vega) < 1e-8:
            break
            
        sigma = sigma - diff / vega
        
        if sigma <= 0.001 or sigma >= 5.0:
            break
            
    # Bisection fallback
    low, high = 0.001, 5.0
    for _ in range(max_iterations):
        mid = (low + high) / 2.0
        price = black_scholes_price(S, K, T, r, mid, option_type)
        diff = price - market_price
        
        if abs(diff) < tolerance:
            return float(mid)
            
        if diff > 0:
            high = mid
        else:
            low = mid
            
    return float((low + high) / 2.0)

def iv_rank(current_iv: float, iv_history: List[float]) -> float:
    """
    IV Rank = (current - 52wk_low) / (52wk_high - 52wk_low) * 100
    """
    if not iv_history:
        return 50.0
        
    iv_min = min(iv_history)
    iv_max = max(iv_history)
    
    if np.isclose(iv_min, iv_max):
        return 50.0
        
    return float(((current_iv - iv_min) / (iv_max - iv_min)) * 100.0)

def iv_percentile(current_iv: float, iv_history: List[float]) -> float:
    """
    Percentage of historical observations below current IV.
    """
    if not iv_history:
        return 50.0
        
    below_count = sum(1 for iv in iv_history if iv < current_iv)
    return float((below_count / len(iv_history)) * 100.0)
