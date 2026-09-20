# backend/app/routers/casino.py
from fastapi import APIRouter, HTTPException, Query, Depends
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any, Union
from datetime import datetime, timezone, timedelta
import json
import logging

from ..database import get_db_connection_dict, get_db_connection
from ..config import get_settings

from ..analytics.options_data import get_options_service
from ..analytics.options_pricing import calculate_greeks, RISK_FREE_RATE, days_to_years, iv_rank
from ..analytics.strategy_builder import (
    Strategy, StrategyLeg, StrategyCategory, Greeks,
    compute_payoff_at_expiry, compute_payoff_at_date, compute_payoff_surface,
    compute_aggregate_greeks, probability_of_profit, kelly_criterion_size,
    build_custom_strategy, STRATEGY_TEMPLATES, CONTRACTS_MULTIPLIER,
)
from ..analytics.casino_recommender import (
    generate_recommendations, scan_universe, StrategyRecommendation,
)

router = APIRouter()
settings = get_settings()
logger = logging.getLogger(__name__)

# --- Pydantic Models ---

class LegDict(BaseModel):
    contract_type: str
    direction: str
    strike: float
    expiration: str
    quantity: int
    premium: float
    implied_volatility: Optional[float] = None
    delta: Optional[float] = None
    gamma: Optional[float] = None
    theta: Optional[float] = None
    vega: Optional[float] = None

class PayoffRequest(BaseModel):
    underlying_ticker: str
    underlying_price: float
    legs: List[LegDict]

class GreeksRequest(BaseModel):
    underlying_ticker: str
    underlying_price: float
    legs: List[LegDict]

class CustomBuildRequest(BaseModel):
    ticker: str
    legs: List[LegDict]

class ApproveRequest(BaseModel):
    quantity: Optional[int] = None

class StrategyResponse(BaseModel):
    id: int
    ticker: str
    strategy_name: str
    strategy_type: str
    legs: List[Dict[str, Any]]
    underlying_price: float
    net_premium: float
    max_profit: Optional[float] = None
    max_loss: Optional[float] = None
    breakeven_points: List[float]
    probability_of_profit: Optional[float] = None
    risk_reward_ratio: Optional[float] = None
    conviction: Optional[float] = None
    risk_grade: Optional[str] = None
    signal_snapshot: Optional[Dict[str, Any]] = None
    reasoning: Optional[str] = None
    recommended_at: str
    status: str
    expiration_date: Optional[str] = None

class RecommendationResponse(BaseModel):
    strategy: StrategyResponse
    conviction: float
    reasoning: str
    risk_grade: str
    signal_summary: Dict[str, Any]

# --- Schema Migration ---

_tables_initialized = False

def ensure_casino_tables():
    global _tables_initialized
    if _tables_initialized:
        return
    
    conn = get_db_connection()
    if not conn:
        logger.error("Failed to connect to database for table initialization.")
        return
        
    cursor = conn.cursor()
    try:
        schema = settings.mimir_schema
        
        cursor.execute(f"""
            CREATE TABLE IF NOT EXISTS {schema}.mimir_casino_strategies (
                id SERIAL PRIMARY KEY,
                ticker VARCHAR(50) NOT NULL,
                strategy_name VARCHAR(255) NOT NULL,
                strategy_type VARCHAR(100) NOT NULL,
                legs JSONB NOT NULL,
                underlying_price FLOAT NOT NULL,
                net_premium FLOAT NOT NULL,
                max_profit FLOAT,
                max_loss FLOAT,
                breakeven_points FLOAT[],
                probability_of_profit FLOAT,
                risk_reward_ratio FLOAT,
                conviction FLOAT,
                risk_grade VARCHAR(10),
                signal_snapshot JSONB,
                reasoning TEXT,
                recommended_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                status VARCHAR(50) DEFAULT 'pending',
                expiration_date TIMESTAMP WITH TIME ZONE,
                resolved_pnl FLOAT,
                resolved_at TIMESTAMP WITH TIME ZONE
            )
        """)
        
        cursor.execute(f"""
            CREATE TABLE IF NOT EXISTS {schema}.mimir_casino_positions (
                id SERIAL PRIMARY KEY,
                strategy_id INTEGER REFERENCES {schema}.mimir_casino_strategies(id),
                ticker VARCHAR(50) NOT NULL,
                leg_index INTEGER NOT NULL,
                contract_type VARCHAR(10) NOT NULL,
                direction VARCHAR(10) NOT NULL,
                strike FLOAT NOT NULL,
                expiration TIMESTAMP WITH TIME ZONE NOT NULL,
                quantity INTEGER NOT NULL,
                entry_premium FLOAT NOT NULL,
                current_premium FLOAT,
                entry_iv FLOAT,
                current_iv FLOAT,
                opened_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                closed_at TIMESTAMP WITH TIME ZONE,
                exit_premium FLOAT,
                realized_pnl FLOAT
            )
        """)
        conn.commit()
        _tables_initialized = True
    except Exception as e:
        conn.rollback()
        logger.error(f"Error initializing casino tables: {e}")
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

def _check_tables():
    if not _tables_initialized:
        ensure_casino_tables()

# --- Helpers ---

def get_now() -> datetime:
    return datetime.now(timezone(timedelta(hours=7)))

def _parse_legs_to_objects(legs_data: List[LegDict]) -> List[StrategyLeg]:
    legs = []
    for leg in legs_data:
        exp_date = None
        if leg.expiration:
            try:
                exp_date = datetime.fromisoformat(leg.expiration.replace('Z', '+00:00')).date()
            except Exception:
                try:
                    exp_date = datetime.strptime(str(leg.expiration).split('T')[0], "%Y-%m-%d").date()
                except Exception:
                    exp_date = date.today() + timedelta(days=30)
        else:
            exp_date = date.today() + timedelta(days=30)
            
        legs.append(StrategyLeg(
            contract_type=leg.contract_type,
            direction=leg.direction,
            strike=leg.strike,
            expiration=exp_date,
            quantity=leg.quantity,
            premium=leg.premium,
            iv=leg.implied_volatility or 0.3
        ))
    return legs

# --- Endpoints ---

@router.get("/chain/{ticker}")
def get_options_chain(ticker: str):
    _check_tables()
    try:
        service = get_options_service()
        chain = service.fetch_chain(ticker)
        if not chain:
            raise HTTPException(status_code=404, detail=f"Chain data not found for {ticker}")
        
        # Limit expirations to first 5
        exp_dates = getattr(chain, 'expirations', [])
        limited_expirations = [d.isoformat() if hasattr(d, 'isoformat') else str(d) for d in exp_dates[:5]]
        
        filtered_calls = {}
        filtered_puts = {}
        calls_dict = getattr(chain, 'calls', {})
        puts_dict = getattr(chain, 'puts', {})
        
        for exp in exp_dates[:5]:
            exp_str = exp.isoformat() if hasattr(exp, 'isoformat') else str(exp)
            calls_list = calls_dict.get(exp, []) if isinstance(calls_dict, dict) else []
            puts_list = puts_dict.get(exp, []) if isinstance(puts_dict, dict) else []
            
            filtered_calls[exp_str] = [
                {
                    "strike": c.strike,
                    "bid": c.bid,
                    "ask": c.ask,
                    "mid": c.mid,
                    "last": c.last,
                    "volume": c.volume,
                    "open_interest": c.open_interest,
                    "implied_volatility": c.implied_volatility
                }
                for c in calls_list
            ]
            filtered_puts[exp_str] = [
                {
                    "strike": p.strike,
                    "bid": p.bid,
                    "ask": p.ask,
                    "mid": p.mid,
                    "last": p.last,
                    "volume": p.volume,
                    "open_interest": p.open_interest,
                    "implied_volatility": p.implied_volatility
                }
                for p in puts_list
            ]

        underlying_price = getattr(chain, 'underlying_price', 100.0)
        
        return {
            "ticker": ticker,
            "underlying_price": round(underlying_price, 2),
            "expirations": limited_expirations,
            "calls": filtered_calls,
            "puts": filtered_puts,
            "liquidity_warning": "Warning: Options with low Open Interest (OI) may have wide bid-ask spreads."
        }
    except Exception as e:
        logger.error(f"Error fetching chain for {ticker}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/iv-surface/{ticker}")
def get_iv_surface(ticker: str):
    _check_tables()
    try:
        service = get_options_service()
        surface_data = service.get_iv_surface(ticker)
        return surface_data
    except Exception as e:
        logger.error(f"Error fetching IV surface for {ticker}: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/payoff")
def get_payoff(request: PayoffRequest):
    _check_tables()
    try:
        legs = _parse_legs_to_objects(request.legs)
        ticker = request.underlying_ticker or "CUSTOM"
        strategy = Strategy(
            name="Custom Payoff",
            category=StrategyCategory.NEUTRAL,
            legs=legs,
            underlying_ticker=ticker,
            underlying_price=request.underlying_price
        )
        surface = compute_payoff_surface(strategy)
        
        # Format the surface data nicely
        curves = {}
        for date_key, date_surface in surface.items():
            key_str = date_key.isoformat() if hasattr(date_key, 'isoformat') else str(date_key)
            if hasattr(date_surface, 'to_dict'):
                curves[key_str] = date_surface.to_dict()
            else:
                curves[key_str] = {
                    "prices": [round(p, 2) for p in getattr(date_surface, 'prices', [])],
                    "pnl": [round(p, 2) for p in getattr(date_surface, 'pnl', [])],
                    "breakevens": [round(b, 2) for b in getattr(date_surface, 'breakevens', [])],
                    "max_profit": round(date_surface.max_profit, 2) if getattr(date_surface, 'max_profit', None) is not None and date_surface.max_profit != float('inf') else None,
                    "max_loss": round(date_surface.max_loss, 2) if getattr(date_surface, 'max_loss', None) is not None and date_surface.max_loss != float('inf') else None
                }
            
        return {"curves": curves}
    except Exception as e:
        logger.error(f"Error computing payoff: {e}", exc_info=True)
        raise HTTPException(status_code=400, detail=str(e))

@router.post("/greeks")
def get_aggregate_greeks(request: GreeksRequest):
    _check_tables()
    try:
        legs = _parse_legs_to_objects(request.legs)
        ticker = request.underlying_ticker or "CUSTOM"
        strategy = Strategy(
            name="Custom Strategy",
            category=StrategyCategory.NEUTRAL,
            legs=legs,
            underlying_ticker=ticker,
            underlying_price=request.underlying_price
        )
        greeks = compute_aggregate_greeks(strategy)
        return {
            "delta": round(greeks.delta, 4) if getattr(greeks, 'delta', None) is not None else 0,
            "gamma": round(greeks.gamma, 4) if getattr(greeks, 'gamma', None) is not None else 0,
            "theta": round(greeks.theta, 4) if getattr(greeks, 'theta', None) is not None else 0,
            "vega": round(greeks.vega, 4) if getattr(greeks, 'vega', None) is not None else 0,
            "rho": round(greeks.rho, 4) if getattr(greeks, 'rho', None) is not None else 0
        }
    except Exception as e:
        logger.error(f"Error computing greeks: {e}", exc_info=True)
        raise HTTPException(status_code=400, detail=str(e))

@router.get("/recommend/{ticker}")
def recommend_strategies(ticker: str, top_k: int = Query(5, ge=1, le=20)):
    _check_tables()
    conn = get_db_connection_dict()
    if not conn:
        raise HTTPException(status_code=500, detail="Database connection failed")
        
    try:
        recommendations = generate_recommendations(ticker, top_k=top_k)
        
        cursor = conn.cursor()
        schema = settings.mimir_schema
        
        saved_recs = []
        for rec in recommendations:
            strat = rec.strategy
            legs_list = [
                {
                    "contract_type": leg.contract_type,
                    "direction": leg.direction,
                    "strike": leg.strike,
                    "expiration": leg.expiration.isoformat() if hasattr(leg.expiration, 'isoformat') else str(leg.expiration),
                    "quantity": leg.quantity,
                    "premium": leg.premium
                }
                for leg in strat.legs
            ]
            legs_json = json.dumps(legs_list)
            
            exp_date = strat.legs[0].expiration if strat.legs else None
            
            cursor.execute(f"""
                INSERT INTO {schema}.mimir_casino_strategies 
                (ticker, strategy_name, strategy_type, legs, underlying_price, net_premium, 
                 max_profit, max_loss, breakeven_points, probability_of_profit, risk_reward_ratio, 
                 conviction, risk_grade, signal_snapshot, reasoning, recommended_at, status, expiration_date)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (
                ticker, strat.name, strat.category.value, legs_json,
                round(strat.underlying_price, 2), round(strat.net_premium, 2),
                round(strat.max_profit, 2) if strat.max_profit is not None and strat.max_profit != float('inf') else None,
                round(strat.max_loss, 2) if strat.max_loss is not None and strat.max_loss != float('inf') and strat.max_loss != -float('inf') else None,
                [round(bp, 2) for bp in strat.breakeven_points],
                round(strat.probability_of_profit, 4) if strat.probability_of_profit else None,
                round(strat.risk_reward_ratio, 4) if strat.risk_reward_ratio else None,
                round(rec.conviction, 2), rec.risk_grade, json.dumps(rec.signal_summary),
                rec.reasoning, get_now(), 'pending', exp_date
            ))
            row = cursor.fetchone()
            
            saved_recs.append({
                "strategy_id": row['id'],
                "strategy_name": strat.name,
                "ticker": ticker,
                "conviction": rec.conviction,
                "risk_grade": rec.risk_grade,
                "reasoning": rec.reasoning,
                "legs": legs_list,
                "strategy": {
                    "ticker": ticker,
                    "name": strat.name,
                    "underlying_price": strat.underlying_price,
                    "legs": legs_list,
                    "max_profit": strat.max_profit if strat.max_profit != float('inf') else None,
                    "max_loss": strat.max_loss if strat.max_loss != float('inf') else None
                }
            })
            
        conn.commit()
        return {"recommendations": saved_recs}
        
    except Exception as e:
        conn.rollback()
        logger.error(f"Error recommending strategies for {ticker}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if 'cursor' in locals() and cursor:
            cursor.close()
        if conn:
            conn.close()
        if conn:
            conn.close()

@router.get("/scan")
def scan_for_opportunities(top_k: int = Query(10, ge=1, le=50)):
    _check_tables()
    try:
        opportunities = scan_universe(top_k=top_k)
        formatted_strategies = []
        for opp in opportunities:
            strat = opp.strategy
            legs_data = []
            for leg in (strat.legs if hasattr(strat, 'legs') and strat.legs else []):
                exp_str = leg.expiration.isoformat() if hasattr(leg.expiration, 'isoformat') else str(leg.expiration)
                legs_data.append({
                    "contract_type": leg.contract_type,
                    "direction": leg.direction,
                    "strike": leg.strike,
                    "expiration": exp_str,
                    "quantity": leg.quantity,
                    "premium": leg.premium
                })
            category_val = strat.category.value if hasattr(strat.category, 'value') else str(strat.category)
            ticker_val = getattr(strat, 'ticker', None) or (strat.legs[0].ticker if hasattr(strat, 'legs') and strat.legs and hasattr(strat.legs[0], 'ticker') else 'SPY')
            
            # Check for infinity in max profit/loss
            mp = round(strat.max_profit, 2) if getattr(strat, 'max_profit', None) is not None and strat.max_profit != float('inf') else None
            ml = round(strat.max_loss, 2) if getattr(strat, 'max_loss', None) is not None and strat.max_loss != float('inf') else None

            formatted_strategies.append({
                "id": getattr(opp, 'strategy_id', 0),
                "ticker": ticker_val,
                "name": strat.name,
                "category": category_val,
                "strategy_type": category_val,
                "conviction": round(opp.conviction, 2),
                "risk_grade": opp.risk_grade,
                "pop": round(strat.probability_of_profit, 2) if getattr(strat, 'probability_of_profit', None) else 0.5,
                "max_profit": mp,
                "max_loss": ml,
                "net_premium": round(strat.net_premium, 2) if getattr(strat, 'net_premium', None) else 0.0,
                "breakeven_points": [round(b, 2) for b in getattr(strat, 'breakeven_points', [])] if getattr(strat, 'breakeven_points', None) else [],
                "reasoning": opp.reasoning,
                "legs": legs_data
            })
            
        return {"strategies": formatted_strategies, "opportunities": formatted_strategies}
    except Exception as e:
        logger.error(f"Error scanning universe: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/strategy/approve/{strategy_id}")
def approve_strategy(strategy_id: int, request: ApproveRequest):
    _check_tables()
    conn = get_db_connection_dict()
    if not conn:
        raise HTTPException(status_code=500, detail="Database connection failed")
        
    try:
        cursor = conn.cursor()
        schema = settings.mimir_schema
        
        cursor.execute(f"SELECT * FROM {schema}.mimir_casino_strategies WHERE id = %s FOR UPDATE", (strategy_id,))
        strategy = cursor.fetchone()
        
        if not strategy:
            raise HTTPException(status_code=404, detail="Strategy not found")
            
        if strategy['status'] != 'pending':
            raise HTTPException(status_code=400, detail=f"Strategy is not pending, current status: {strategy['status']}")
            
        qty_multiplier = request.quantity if request.quantity and request.quantity > 0 else 1
        
        legs = strategy['legs']
        now = get_now()
        
        for i, leg in enumerate(legs):
            cursor.execute(f"""
                INSERT INTO {schema}.mimir_casino_positions 
                (strategy_id, ticker, leg_index, contract_type, direction, strike, expiration, 
                 quantity, entry_premium, opened_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                strategy_id,
                strategy['ticker'],
                i,
                leg['contract_type'],
                leg['direction'],
                leg['strike'],
                leg['expiration'],
                leg['quantity'] * qty_multiplier,
                leg['premium'],
                now
            ))
            
        cursor.execute(f"""
            UPDATE {schema}.mimir_casino_strategies 
            SET status = 'approved' 
            WHERE id = %s
        """, (strategy_id,))
        
        conn.commit()
        return {"message": "Strategy approved and positions opened", "strategy_id": strategy_id}
        
    except HTTPException:
        conn.rollback()
        raise
    except Exception as e:
        conn.rollback()
        logger.error(f"Error approving strategy {strategy_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if 'cursor' in locals() and cursor:
            cursor.close()
        if conn:
            conn.close()

@router.post("/strategy/reject/{strategy_id}")
def reject_strategy(strategy_id: int):
    _check_tables()
    conn = get_db_connection()
    if not conn:
        raise HTTPException(status_code=500, detail="Database connection failed")
        
    try:
        cursor = conn.cursor()
        schema = settings.mimir_schema
        
        cursor.execute(f"""
            UPDATE {schema}.mimir_casino_strategies 
            SET status = 'rejected' 
            WHERE id = %s AND status = 'pending'
            RETURNING id
        """, (strategy_id,))
        
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Strategy not found or not pending")
            
        conn.commit()
        return {"message": "Strategy rejected", "strategy_id": strategy_id}
        
    except HTTPException:
        conn.rollback()
        raise
    except Exception as e:
        conn.rollback()
        logger.error(f"Error rejecting strategy {strategy_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if 'cursor' in locals() and cursor:
            cursor.close()
        if conn:
            conn.close()

@router.get("/positions")
def list_positions():
    _check_tables()
    conn = get_db_connection_dict()
    if not conn:
        raise HTTPException(status_code=500, detail="Database connection failed")
        
    try:
        cursor = conn.cursor()
        schema = settings.mimir_schema
        
        cursor.execute(f"""
            SELECT p.*, s.strategy_name, s.strategy_type 
            FROM {schema}.mimir_casino_positions p
            JOIN {schema}.mimir_casino_strategies s ON p.strategy_id = s.id
            WHERE p.closed_at IS NULL
            ORDER BY p.opened_at DESC
        """)
        
        positions = cursor.fetchall()
        
        # Format datetimes for JSON serialization
        for pos in positions:
            for key, val in pos.items():
                if isinstance(val, datetime):
                    pos[key] = val.isoformat()
                    
        return {"positions": positions}
        
    except Exception as e:
        logger.error(f"Error listing positions: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if 'cursor' in locals() and cursor:
            cursor.close()
        if conn:
            conn.close()

@router.get("/positions/{strategy_id}/pnl")
def get_strategy_pnl(strategy_id: int):
    _check_tables()
    conn = get_db_connection_dict()
    if not conn:
        raise HTTPException(status_code=500, detail="Database connection failed")
        
    try:
        cursor = conn.cursor()
        schema = settings.mimir_schema
        
        cursor.execute(f"""
            SELECT * FROM {schema}.mimir_casino_positions 
            WHERE strategy_id = %s
        """, (strategy_id,))
        
        positions = cursor.fetchall()
        if not positions:
            raise HTTPException(status_code=404, detail="Positions not found for strategy")
            
        # In a real app, this would query live option prices for 'current_premium'
        # For now, we mock calculating a basic realized/unrealized PNL based on existing DB fields
        
        unrealized_pnl = 0.0
        realized_pnl = 0.0
        
        for pos in positions:
            multiplier = CONTRACTS_MULTIPLIER * pos['quantity']
            if pos['closed_at']:
                if pos['realized_pnl'] is not None:
                    realized_pnl += pos['realized_pnl']
                else:
                    diff = (pos['exit_premium'] or 0) - pos['entry_premium']
                    if pos['direction'] == 'short':
                        diff = -diff
                    realized_pnl += diff * multiplier
            else:
                if pos['current_premium'] is not None:
                    diff = pos['current_premium'] - pos['entry_premium']
                    if pos['direction'] == 'short':
                        diff = -diff
                    unrealized_pnl += diff * multiplier
                    
        return {
            "strategy_id": strategy_id,
            "realized_pnl": round(realized_pnl, 2),
            "unrealized_pnl": round(unrealized_pnl, 2),
            "total_pnl": round(realized_pnl + unrealized_pnl, 2)
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error computing PNL for strategy {strategy_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if 'cursor' in locals() and cursor:
            cursor.close()
        if conn:
            conn.close()

@router.get("/performance")
def get_performance_metrics():
    _check_tables()
    conn = get_db_connection_dict()
    if not conn:
        raise HTTPException(status_code=500, detail="Database connection failed")
        
    try:
        cursor = conn.cursor()
        schema = settings.mimir_schema
        
        cursor.execute(f"""
            SELECT 
                COUNT(*) as total_trades,
                SUM(CASE WHEN resolved_pnl > 0 THEN 1 ELSE 0 END) as winning_trades,
                SUM(CASE WHEN resolved_pnl <= 0 THEN 1 ELSE 0 END) as losing_trades,
                SUM(resolved_pnl) as total_pnl
            FROM {schema}.mimir_casino_strategies
            WHERE status = 'resolved'
        """)
        
        metrics = cursor.fetchone()
        
        total = metrics['total_trades'] or 0
        win_rate = (metrics['winning_trades'] / total) if total > 0 else 0
        
        return {
            "total_trades": total,
            "winning_trades": metrics['winning_trades'] or 0,
            "losing_trades": metrics['losing_trades'] or 0,
            "win_rate": round(win_rate, 4),
            "total_pnl": round(metrics['total_pnl'] or 0, 2)
        }
        
    except Exception as e:
        logger.error(f"Error fetching performance metrics: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if 'cursor' in locals() and cursor:
            cursor.close()
        if conn:
            conn.close()

@router.get("/strategies/history")
def get_strategies_history(limit: int = Query(50, ge=1, le=100)):
    _check_tables()
    conn = get_db_connection_dict()
    if not conn:
        raise HTTPException(status_code=500, detail="Database connection failed")
        
    try:
        cursor = conn.cursor()
        schema = settings.mimir_schema
        
        cursor.execute(f"""
            SELECT * FROM {schema}.mimir_casino_strategies 
            WHERE status IN ('resolved', 'rejected', 'expired')
            ORDER BY recommended_at DESC
            LIMIT %s
        """, (limit,))
        
        history = cursor.fetchall()
        
        for record in history:
            for key, val in record.items():
                if isinstance(val, datetime):
                    record[key] = val.isoformat()
                    
        return {"history": history}
        
    except Exception as e:
        logger.error(f"Error fetching strategies history: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if 'cursor' in locals() and cursor:
            cursor.close()
        if conn:
            conn.close()

@router.post("/custom-build")
def build_custom(request: CustomBuildRequest):
    _check_tables()
    try:
        legs = _parse_legs_to_objects(request.legs)
        strategy = build_custom_strategy(request.ticker, legs)
        
        # Serialize the strategy safely
        response_legs = []
        for leg in strategy.legs:
            response_legs.append({
                "contract_type": leg.contract_type,
                "direction": leg.direction,
                "strike": leg.strike,
                "expiration": leg.expiration.isoformat(),
                "quantity": leg.quantity,
                "premium": leg.premium
            })
            
        return {
            "name": strategy.name,
            "ticker": request.ticker,
            "net_premium": round(strategy.net_premium, 2),
            "max_profit": round(strategy.max_profit, 2) if strategy.max_profit is not None else None,
            "max_loss": round(strategy.max_loss, 2) if strategy.max_loss is not None else None,
            "breakeven_points": [round(bp, 2) for bp in strategy.breakeven_points],
            "legs": response_legs
        }
    except Exception as e:
        logger.error(f"Error building custom strategy: {e}")
        raise HTTPException(status_code=400, detail=str(e))

@router.get("/templates")
def get_templates():
    return {"templates": STRATEGY_TEMPLATES}


# --- War Rig -> Casino Nitrous Express Bridge Endpoints ---

class NitrousSolveRequest(BaseModel):
    ticker: str
    trigger_price: float
    target_price: float
    stop_loss: float
    conviction_score: Optional[float] = 0.75
    risk_reward_ratio: Optional[float] = 2.5
    holding_period: Optional[str] = "5-14 Days"
    catalyst_type: Optional[str] = "WAR_RIG_CONVERGENCE"
    earnings_date: Optional[str] = None
    days_until_earnings: Optional[int] = None
    investment_thesis: Optional[str] = ""


class NitrousInjectRequest(BaseModel):
    ticker: str
    mode: str  # "MODE_A" or "MODE_B"
    strategy_payload: Dict[str, Any]
    war_rig_signal_id: Optional[int] = None


@router.get("/nitrous/active-convergences")
def get_nitrous_active_convergences(limit: int = Query(10, ge=1, le=25)):
    """
    Fetches active War Rig Crankshaft convergence signals (>= 75% conviction)
    and computes the Nitro Mode A and Mode B options configurations for each.
    """
    _check_tables()
    from ..analytics.war_rig_nitrous import get_nitrous_bridge
    bridge = get_nitrous_bridge()

    conn = get_db_connection_dict()
    schema = settings.mimir_schema
    convergences = []

    try:
        cursor = conn.cursor()
        cursor.execute(f"""
            SELECT id, ticker, trigger_price, target_price, stop_loss, conviction_score,
                   holding_period, headline, investment_thesis, reason, catalyst_type, created_at
            FROM {schema}.mimir_trade_signals
            WHERE catalyst_type = 'WAR_RIG_CONVERGENCE'
            ORDER BY created_at DESC
            LIMIT %s
        """, (limit,))
        rows = cursor.fetchall()
        cursor.close()

        if not rows:
            # Fallback to scanning top liquid names through the War Rig crankshaft
            from ..analytics.war_rig_engine import WarRigEngine
            from ..routers.prices import DEFAULT_TICKERS
            engine = WarRigEngine(conn=conn)
            scan_candidates = [t for t in DEFAULT_TICKERS[:12] if t]
            for sym in scan_candidates:
                try:
                    cand = engine.evaluate_war_rig_candidate(sym, conn=conn)
                    if cand and cand.get("conviction_score", 0) >= 0.75:
                        rows.append(cand)
                except Exception:
                    continue

        for r in rows:
            sig_dict = dict(r)
            if "id" in sig_dict:
                sig_dict["signal_id"] = sig_dict["id"]
            if isinstance(sig_dict.get("created_at"), datetime):
                sig_dict["created_at"] = sig_dict["created_at"].isoformat()

            try:
                nitrous_data = bridge.generate_nitrous_deployment(sig_dict)
                convergences.append({
                    "signal": sig_dict,
                    "nitrous": nitrous_data
                })
            except Exception as ex:
                logger.warning(f"[CASINO_NITROUS] Error building nitrous for {sig_dict.get('ticker')}: {ex}")

        return {
            "status": "success",
            "count": len(convergences),
            "convergences": convergences
        }
    except Exception as e:
        logger.error(f"[CASINO_NITROUS] Error getting active convergences: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn:
            conn.close()


@router.post("/nitrous/solve")
def solve_nitrous_options(request: NitrousSolveRequest):
    """
    Direct endpoint: Ingests War Rig execution bounds (trigger, target, stop loss)
    and dynamically solves for Nitro Mode A (Bull Call Vertical Spread 3:1 - 5:1 asymmetry)
    and Nitro Mode B (Gamma Straddles & IV Crush Harvester).
    """
    from ..analytics.war_rig_nitrous import get_nitrous_bridge
    bridge = get_nitrous_bridge()
    try:
        result = bridge.generate_nitrous_deployment(request.model_dump())
        return result
    except Exception as e:
        logger.error(f"[CASINO_NITROUS] Error solving nitrous options for {request.ticker}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/nitrous/inject")
def inject_nitrous_pod(request: NitrousInjectRequest):
    """
    Hit the NITROUS Button: Deploys the selected options strategy into mimir_casino_strategies
    linked to the War Rig signal, marking it ready for execution.
    """
    _check_tables()
    conn = get_db_connection_dict()
    if not conn:
        raise HTTPException(status_code=500, detail="Database connection failed")

    schema = settings.mimir_schema
    strat = request.strategy_payload

    try:
        cursor = conn.cursor()
        legs_json = json.dumps(strat.get("legs", []))
        strat_name = strat.get("strategy_name", f"{request.ticker} Nitrous {request.mode}")
        cat_type = "bullish" if not strat.get("is_credit") else "income"
        spot = float(strat.get("underlying_price", 100.0))
        prem = float(strat.get("net_debit_or_credit", 0.0))
        if not strat.get("is_credit"):
            prem = -abs(prem)  # debit
        else:
            prem = abs(prem)   # credit

        mp = strat.get("max_profit")
        ml = strat.get("max_loss")
        if ml is not None and not strat.get("is_credit"):
            ml = -abs(ml)

        be_list = [strat.get("breakeven")] if strat.get("breakeven") else []
        pop = strat.get("probability_of_profit", 0.5)
        rr = strat.get("asymmetry_ratio", 3.0)
        conv = 0.85
        risk_grade = "DEFINED_RISK_CAPPED"
        snapshot = {
            "nitrous_mode": request.mode,
            "war_rig_signal_id": request.war_rig_signal_id,
            "target_price": strat.get("target_price"),
            "stop_loss": strat.get("stop_loss"),
            "iv_rank": strat.get("iv_rank")
        }
        reasoning = strat.get("thesis", "War Rig Nitrous Oxide Injector deployment.")
        exp_date = strat.get("expiration")

        cursor.execute(f"""
            INSERT INTO {schema}.mimir_casino_strategies
            (ticker, strategy_name, strategy_type, legs, underlying_price, net_premium,
             max_profit, max_loss, breakeven_points, probability_of_profit, risk_reward_ratio,
             conviction, risk_grade, signal_snapshot, reasoning, recommended_at, status, expiration_date)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (
            request.ticker, strat_name, cat_type, legs_json,
            spot, prem, mp, ml, be_list, pop, rr, conv,
            risk_grade, json.dumps(snapshot), reasoning, get_now(), "pending_nitrous", exp_date
        ))
        row = cursor.fetchone()
        strategy_id = row["id"]
        conn.commit()

        return {
            "status": "success",
            "strategy_id": strategy_id,
            "ticker": request.ticker,
            "mode": request.mode,
            "strategy_name": strat_name,
            "legs": strat.get("legs", []),
            "max_profit": mp,
            "max_loss": ml,
            "asymmetry_ratio": rr,
            "message": f"Nitrous Pod successfully deployed for {request.ticker}!"
        }
    except Exception as e:
        conn.rollback()
        logger.error(f"[CASINO_NITROUS] Error injecting nitrous pod: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

