# backend/app/analytics/casino_recommender.py

import json
import traceback
import math
from dataclasses import dataclass
from typing import List, Dict, Optional, Any, Tuple
from datetime import date, datetime, timedelta
import pandas as pd
import numpy as np

from .signal_fusion import (
    get_recent_prices,
    get_recent_sentiment,
    get_ticker_parameters,
    get_ticker_live_feedback,
    DEFAULT_TICKERS
)
from .options_data import get_options_service, OptionsChain
from .options_pricing import iv_rank, iv_percentile, calculate_greeks, RISK_FREE_RATE, days_to_years
from .strategy_builder import (
    Strategy, StrategyLeg, StrategyCategory, Greeks,
    build_long_call, build_long_put,
    build_bull_call_spread, build_bear_put_spread,
    build_bull_put_spread, build_bear_call_spread,
    build_long_straddle, build_short_straddle,
    build_long_strangle, build_short_strangle,
    build_iron_condor, build_iron_butterfly,
    build_covered_call, build_cash_secured_put,
    build_calendar_spread,
    compute_payoff_at_expiry, probability_of_profit,
    CONTRACTS_MULTIPLIER,
)
from .technical_analysis import analyze_technical_indicators
from ..sentiment.llm_client import send_chat_completion
from ..database import get_db_connection, get_db_connection_dict
from ..config import get_settings

@dataclass
class SignalBundle:
    """Aggregated signals from all MIMIR pipelines for a single ticker."""
    ticker: str
    sentiment_score: Optional[float]
    sentiment_1d: Optional[float]
    sentiment_5d: Optional[float]
    prob_buy: float
    prob_sell: float
    rsi: Optional[float]
    macd_signal: Optional[str]
    bollinger_pct_b: Optional[float]
    iv_rank_value: Optional[float]
    historical_vol: Optional[float]
    current_iv: Optional[float]
    current_price: float
    support: Optional[float]
    resistance: Optional[float]
    win_rate: Optional[float]
    avg_pnl: Optional[float]

@dataclass 
class StrategyRecommendation:
    strategy: Strategy
    conviction: float
    reasoning: str
    signal_summary: Dict[str, Any]
    risk_grade: str
    expected_value: float

CASINO_DEFAULT_TICKERS = ['AAPL', 'MSFT', 'NVDA', 'TSLA', 'AMZN', 'GOOGL', 'META', 'SPY', 'QQQ', 'IWM', 'AMD', 'NFLX']

def gather_signals(ticker: str) -> SignalBundle:
    print(f"[CASINO_RECOMMENDER] Gathering signals for {ticker}")
    settings = get_settings()
    
    # 1. Sentiment
    sentiment_1d = get_recent_sentiment(ticker, days=1)
    sentiment_5d = get_recent_sentiment(ticker, days=5)
    sentiment_score = sentiment_1d if sentiment_1d is not None else sentiment_5d
    
    # 2. XGBoost Params
    params = get_ticker_parameters(ticker) or {}
    prob_buy = params.get('prob_buy', 0.5)
    prob_sell = params.get('prob_sell', 0.5)
    
    # Performance
    win_rate = params.get('win_rate', None)
    avg_pnl = params.get('avg_pnl', None)
    
    # 3. Prices & Technicals
    prices_df = get_recent_prices(ticker, days=252)
    
    rsi = None
    macd_signal = None
    bollinger_pct_b = None
    historical_vol = None
    current_price = 0.0
    support = None
    resistance = None
    
    if prices_df is not None and not prices_df.empty:
        current_price = float(prices_df['close'].iloc[-1])
        tech_indicators = analyze_technical_indicators(prices_df)
        
        # We assume analyze_technical_indicators adds columns to the dataframe or returns a dict.
        # Handling both cases for safety.
        if isinstance(tech_indicators, pd.DataFrame):
            df_tech = tech_indicators
            if 'rsi' in df_tech.columns: rsi = float(df_tech['rsi'].iloc[-1])
            if 'macd_signal' in df_tech.columns: macd_signal = 'bullish' if df_tech['macd_signal'].iloc[-1] > 0 else 'bearish'
            if 'bollinger_pct_b' in df_tech.columns: bollinger_pct_b = float(df_tech['bollinger_pct_b'].iloc[-1])
        elif isinstance(tech_indicators, dict):
            rsi = tech_indicators.get('rsi')
            macd_signal = tech_indicators.get('macd_signal')
            bollinger_pct_b = tech_indicators.get('bollinger_pct_b')
            support = tech_indicators.get('support')
            resistance = tech_indicators.get('resistance')
            
        # Compute historical vol (20-day rolling over 252 days)
        if len(prices_df) >= 20:
            returns = prices_df['close'].pct_change().dropna()
            rolling_std = returns.rolling(window=20).std() * np.sqrt(252)
            historical_vol = float(rolling_std.iloc[-1]) if not pd.isna(rolling_std.iloc[-1]) else 0.2
    
    # 4. Volatility from options chain
    svc = get_options_service()
    chain = svc.fetch_chain(ticker)
    
    current_iv = None
    iv_rank_value = None
    
    if chain is not None:
        if current_price <= 0.0 and hasattr(chain, 'underlying_price') and chain.underlying_price > 0:
            current_price = chain.underlying_price

        if hasattr(chain, 'current_iv') and chain.current_iv is not None:
            current_iv = chain.current_iv
        
        # Mocking an IV rank if real data is missing
        if hasattr(chain, 'iv_rank') and chain.iv_rank is not None:
            iv_rank_value = chain.iv_rank
        elif current_iv and historical_vol:
            # Fake IV rank for illustration if not available
            iv_rank_value = min(max((current_iv / (historical_vol * 1.5)) * 100, 0), 100)
    else:
        current_iv = historical_vol if historical_vol else 0.3
        iv_rank_value = 50.0

    if current_price <= 0.0:
        current_price = 150.0  # Fallback to realistic stock price for strategy generation

    return SignalBundle(
        ticker=ticker,
        sentiment_score=sentiment_score,
        sentiment_1d=sentiment_1d,
        sentiment_5d=sentiment_5d,
        prob_buy=prob_buy,
        prob_sell=prob_sell,
        rsi=rsi,
        macd_signal=macd_signal,
        bollinger_pct_b=bollinger_pct_b,
        iv_rank_value=iv_rank_value,
        historical_vol=historical_vol,
        current_iv=current_iv,
        current_price=current_price,
        support=support,
        resistance=resistance,
        win_rate=win_rate,
        avg_pnl=avg_pnl
    )

def scan_universe(tickers: Optional[List[str]] = None, top_k: int = 10) -> List[StrategyRecommendation]:
    target_tickers = tickers if tickers else CASINO_DEFAULT_TICKERS
    all_recs = []
    
    print(f"[CASINO_RECOMMENDER] Fast parallel scanning universe of {len(target_tickers)} tickers")
    
    def process_ticker(t: str) -> List[StrategyRecommendation]:
        return generate_recommendations(t, top_k=1, use_llm=False)

    with ThreadPoolExecutor(max_workers=min(len(target_tickers), 10)) as executor:
        results = executor.map(process_ticker, target_tickers)
        for recs in results:
            if recs:
                all_recs.extend(recs)
            
    # Sort all by conviction
    all_recs.sort(key=lambda x: x.conviction, reverse=True)
    return all_recs[:top_k]

def _get_contracts_list(chain: OptionsChain) -> List[Any]:
    if hasattr(chain, 'contracts') and chain.contracts:
        return chain.contracts
    if hasattr(chain, 'options') and chain.options:
        return chain.options
    
    all_contracts = []
    if hasattr(chain, 'calls') and chain.calls:
        for exp_date, call_list in chain.calls.items():
            for c in call_list:
                setattr(c, 'expiration', exp_date)
                setattr(c, 'option_type', 'call')
                all_contracts.append(c)
    if hasattr(chain, 'puts') and chain.puts:
        for exp_date, put_list in chain.puts.items():
            for c in put_list:
                setattr(c, 'expiration', exp_date)
                setattr(c, 'option_type', 'put')
                all_contracts.append(c)
    return all_contracts

def _select_expiration(chain: OptionsChain, target_dte: int = 35) -> date:
    contracts = _get_contracts_list(chain)
    if not contracts:
        return date.today() + timedelta(days=target_dte)
        
    today = date.today()
    expirations = sorted(list(set(c.expiration for c in contracts if isinstance(c.expiration, date))))
    
    if not expirations:
        return date.today() + timedelta(days=target_dte)
        
    closest = min(expirations, key=lambda x: abs((x - today).days - target_dte))
    return closest

def _select_strike_atm(chain: OptionsChain, expiration: date, option_type: str, current_price: float) -> Optional[Any]:
    contracts = _get_contracts_list(chain)
    valid = [c for c in contracts if c.expiration == expiration and c.option_type.lower() == option_type.lower()]
    if not valid:
        return None
        
    closest = min(valid, key=lambda x: abs(x.strike - current_price))
    return closest

def _select_strike_otm(chain: OptionsChain, expiration: date, option_type: str, current_price: float, current_iv: float, delta_target: float = 0.30) -> Optional[Any]:
    contracts = _get_contracts_list(chain)
    valid = [c for c in contracts if c.expiration == expiration and c.option_type.lower() == option_type.lower()]
    if not valid:
        return None
        
    T = days_to_years((expiration - date.today()).days) or 0.01
    
    best_contract = None
    best_diff = float('inf')
    
    for c in valid:
        # Check if contract already has delta
        if hasattr(c, 'greeks') and c.greeks and hasattr(c.greeks, 'delta'):
            delta = abs(c.greeks.delta)
        else:
            # Estimate delta
            iv = c.implied_volatility if hasattr(c, 'implied_volatility') and c.implied_volatility else current_iv
            if not iv: iv = 0.3
            greeks = calculate_greeks(current_price, c.strike, T, RISK_FREE_RATE, iv, option_type)
            raw_delta = greeks.delta if hasattr(greeks, 'delta') else (greeks.get('delta', 0.5) if isinstance(greeks, dict) else 0.5)
            delta = abs(raw_delta)
            
        diff = abs(delta - delta_target)
        if diff < best_diff:
            best_diff = diff
            best_contract = c
            
    return best_contract

def _compute_risk_grade(strategy: Strategy) -> str:
    max_loss = strategy.max_loss
    if max_loss == float('inf'):
        return 'EXTREME'
        
    # Heuristic based on max loss per contract
    if max_loss < 200:
        return 'LOW'
    elif max_loss < 1000:
        return 'MEDIUM'
    else:
        return 'HIGH'

def _compute_conviction(signals: SignalBundle) -> float:
    # 0 to 1 score based on signal alignment
    score = 0.5
    
    bullish_weight = signals.prob_buy
    bearish_weight = signals.prob_sell
    
    if signals.sentiment_score is not None:
        if signals.sentiment_score > 0.3:
            bullish_weight += 0.2
        elif signals.sentiment_score < -0.3:
            bearish_weight += 0.2
            
    if signals.macd_signal == 'bullish':
        bullish_weight += 0.1
    elif signals.macd_signal == 'bearish':
        bearish_weight += 0.1
        
    if signals.rsi is not None:
        if signals.rsi < 30:
            bullish_weight += 0.15
        elif signals.rsi > 70:
            bearish_weight += 0.15
            
    max_directional = max(bullish_weight, bearish_weight)
    
    # Normalize to 0-1 range roughly
    conviction = min(max(max_directional, 0.0), 1.0)
    return conviction

def select_strategies(signals: SignalBundle, chain: OptionsChain, top_k: int = 5) -> List[Strategy]:
    print(f"[CASINO_RECOMMENDER] Selecting strategies for {signals.ticker}")
    strategies = []
    
    price = signals.current_price
    iv = signals.current_iv or 0.3
    iv_rank_val = signals.iv_rank_value or 50.0
    
    high_iv = iv_rank_val > 50
    strong_bullish = signals.prob_buy > 0.65 and (signals.sentiment_score or 0) > 0.3
    strong_bearish = signals.prob_sell > 0.65 and (signals.sentiment_score or 0) < -0.3
    
    exp_date = _select_expiration(chain, 35)
    
    def _mid_price(c: Any) -> float:
        if not c: return 0.0
        bid = getattr(c, 'bid', 0.0)
        ask = getattr(c, 'ask', 0.0)
        return (bid + ask) / 2 if bid and ask else getattr(c, 'last_price', 0.0)
    
    atm_call = _select_strike_atm(chain, exp_date, 'call', price)
    atm_put = _select_strike_atm(chain, exp_date, 'put', price)
    otm_call = _select_strike_otm(chain, exp_date, 'call', price, iv, 0.3)
    otm_put = _select_strike_otm(chain, exp_date, 'put', price, iv, 0.3)
    
    try:
        if strong_bullish:
            if high_iv:
                if atm_put and otm_put:
                    strategies.append(build_bull_put_spread(signals.ticker, price, otm_put.strike, atm_put.strike, exp_date, _mid_price(otm_put), _mid_price(atm_put)))
                if otm_put:
                    strategies.append(build_cash_secured_put(signals.ticker, price, otm_put.strike, exp_date, _mid_price(otm_put)))
            else:
                if otm_call:
                    strategies.append(build_long_call(signals.ticker, price, otm_call.strike, exp_date, _mid_price(otm_call)))
                if atm_call and otm_call:
                    strategies.append(build_bull_call_spread(signals.ticker, price, atm_call.strike, otm_call.strike, exp_date, _mid_price(atm_call), _mid_price(otm_call)))
                    
        elif strong_bearish:
            if high_iv:
                if atm_call and otm_call:
                    strategies.append(build_bear_call_spread(signals.ticker, price, atm_call.strike, otm_call.strike, exp_date, _mid_price(atm_call), _mid_price(otm_call)))
            else:
                if otm_put:
                    strategies.append(build_long_put(signals.ticker, price, otm_put.strike, exp_date, _mid_price(otm_put)))
                if atm_put and otm_put:
                    strategies.append(build_bear_put_spread(signals.ticker, price, otm_put.strike, atm_put.strike, exp_date, _mid_price(otm_put), _mid_price(atm_put)))
        else:
            if atm_call and otm_call:
                strategies.append(build_bull_call_spread(signals.ticker, price, atm_call.strike, otm_call.strike, exp_date, _mid_price(atm_call), _mid_price(otm_call)))
            if atm_put and otm_put:
                strategies.append(build_bear_put_spread(signals.ticker, price, otm_put.strike, atm_put.strike, exp_date, _mid_price(otm_put), _mid_price(atm_put)))
            if otm_call:
                strategies.append(build_long_call(signals.ticker, price, otm_call.strike, exp_date, _mid_price(otm_call)))

        # Fallback if no specific strategy matched
        if not strategies and otm_call:
            strategies.append(build_long_call(signals.ticker, price, otm_call.strike, exp_date, _mid_price(otm_call)))
    except Exception as e:
        print(f"[CASINO_RECOMMENDER] Error building strategies: {e}")
        traceback.print_exc()

    return strategies[:top_k]

def rank_strategies(candidates: List[Strategy], signals: SignalBundle) -> List[StrategyRecommendation]:
    recs = []
    base_conviction = _compute_conviction(signals)
    
    for strat in candidates:
        pop = probability_of_profit(strat)
        max_prof = strat.max_profit
        max_loss = strat.max_loss
        
        # Expected value rough estimation
        if max_loss != float('inf'):
            ev = (pop * max_prof) - ((1 - pop) * max_loss)
        else:
            # Penalty for undefined risk
            ev = (pop * max_prof) - ((1 - pop) * (signals.current_price * 100 * 0.2)) # Arbitrary 20% downside
            
        # Adjust conviction based on strategy match with PoP
        strat_conviction = min(base_conviction * (pop + 0.5), 1.0)
        
        recs.append(
            StrategyRecommendation(
                strategy=strat,
                conviction=strat_conviction,
                reasoning="",
                signal_summary={
                    "prob_buy": signals.prob_buy,
                    "prob_sell": signals.prob_sell,
                    "sentiment": signals.sentiment_score,
                    "iv_rank": signals.iv_rank_value
                },
                risk_grade=_compute_risk_grade(strat),
                expected_value=ev
            )
        )
        
    recs.sort(key=lambda x: x.conviction * x.expected_value, reverse=True)
    return recs

def generate_explanation(strategy: Strategy, signals: SignalBundle) -> str:
    prompt = f"""
Explain why the '{strategy.name}' options strategy is recommended for {signals.ticker}.
Current Signals:
- Price: ${signals.current_price:.2f}
- Buy Probability (XGBoost): {signals.prob_buy:.2f}
- Sell Probability (XGBoost): {signals.prob_sell:.2f}
- Sentiment: {signals.sentiment_score}
- IV Rank: {signals.iv_rank_value}

Strategy Details:
- Cost/Credit: ${strategy.net_cost if strategy.net_cost > 0 else -strategy.net_credit:.2f}
- Max Profit: {'Unlimited' if strategy.max_profit == float('inf') else f'${strategy.max_profit:.2f}'}
- Max Loss: {'Unlimited' if strategy.max_loss == float('inf') else f'${strategy.max_loss:.2f}'}

Keep the explanation concise, professional, and max 3 sentences. Focus on how the strategy fits the signals.
"""
    try:
        response = send_chat_completion([{"role": "user", "content": prompt}], max_tokens=100)
        if response and response.strip():
            return response.strip()
    except Exception as e:
        print(f"[CASINO_RECOMMENDER] LLM explanation failed: {e}")
        
    # Rule-based fallback
    return f"The {strategy.name} strategy aligns with {signals.ticker}'s current signal profile, offering defined risk/reward characteristics suitable for an IV Rank of {signals.iv_rank_value:.1f} and buy probability of {signals.prob_buy:.2f}."

def generate_recommendations(ticker: str, top_k: int = 5, use_llm: bool = True) -> List[StrategyRecommendation]:
    try:
        signals = gather_signals(ticker)
        svc = get_options_service()
        chain = svc.fetch_chain(ticker)
        
        if not chain:
            print(f"[CASINO_RECOMMENDER] No options chain available for {ticker}")
            return []
            
        candidates = select_strategies(signals, chain, top_k=top_k*2)
        ranked = rank_strategies(candidates, signals)[:top_k]
        
        for rec in ranked:
            if use_llm:
                rec.reasoning = generate_explanation(rec.strategy, signals)
            else:
                iv_rank_str = f"{signals.iv_rank_value:.1f}" if signals.iv_rank_value is not None else "50.0"
                rec.reasoning = f"The {rec.strategy.name} strategy aligns with {signals.ticker}'s signal profile, offering defined risk/reward for an IV Rank of {iv_rank_str}."
            
        return ranked
    except Exception as e:
        print(f"[CASINO_RECOMMENDER] Error generating recommendations for {ticker}: {e}")
        traceback.print_exc()
        return []

from concurrent.futures import ThreadPoolExecutor

def scan_universe(tickers: Optional[List[str]] = None, top_k: int = 10) -> List[StrategyRecommendation]:
    target_tickers = tickers if tickers else DEFAULT_TICKERS
    all_recs = []
    
    print(f"[CASINO_RECOMMENDER] Fast parallel scanning universe of {len(target_tickers)} tickers")
    
    def process_ticker(t: str) -> List[StrategyRecommendation]:
        return generate_recommendations(t, top_k=1, use_llm=False)

    with ThreadPoolExecutor(max_workers=min(len(target_tickers), 10)) as executor:
        results = executor.map(process_ticker, target_tickers)
        for recs in results:
            if recs:
                all_recs.extend(recs)
            
    # Sort all by conviction
    all_recs.sort(key=lambda x: x.conviction, reverse=True)
    return all_recs[:top_k]

