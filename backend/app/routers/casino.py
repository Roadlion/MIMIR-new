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
        try:
            exp_date = datetime.fromisoformat(leg.expiration.replace('Z', '+00:00'))
        except ValueError:
            # Fallback format parsing
            exp_date = datetime.strptime(leg.expiration.split('T')[0], "%Y-%m-%d").replace(tzinfo=timezone.utc)
            
        legs.append(StrategyLeg(
            contract_type=leg.contract_type,
            direction=leg.direction,
            strike=leg.strike,
            expiration=exp_date,
            quantity=leg.quantity,
            premium=leg.premium,
            implied_volatility=leg.implied_volatility or 0.0,
            delta=leg.delta,
            gamma=leg.gamma,
            theta=leg.theta,
            vega=leg.vega
        ))
    return legs

# --- Endpoints ---

@router.get("/chain/{ticker}")
def get_options_chain(ticker: str):
    _check_tables()
    try:
        service = get_options_service()
        chain_data = service.fetch_chain(ticker)
        if not chain_data:
            raise HTTPException(status_code=404, detail=f"Chain data not found for {ticker}")
        
        # Limit expirations to first 3 to keep response manageable
        limited_expirations = chain_data.get('expirations', [])[:3]
        filtered_calls = {exp: chain_data.get('calls', {}).get(exp, []) for exp in limited_expirations}
        filtered_puts = {exp: chain_data.get('puts', {}).get(exp, []) for exp in limited_expirations}
        
        return {
            "ticker": ticker,
            "underlying_price": round(chain_data.get('underlying_price', 0.0), 2),
            "expirations": limited_expirations,
            "calls": filtered_calls,
            "puts": filtered_puts,
            "liquidity_warning": "Warning: Options with low Open Interest (OI) may have wide bid-ask spreads."
        }
    except Exception as e:
        logger.error(f"Error fetching chain for {ticker}: {e}")
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
        strategy = Strategy(
            name="Custom Payoff",
            category=StrategyCategory.CUSTOM,
            legs=legs,
            underlying_price=request.underlying_price
        )
        surface = compute_payoff_surface(strategy)
        
        # Format the surface data nicely
        curves = {}
        for date, date_surface in surface.items():
            curves[date.isoformat()] = {
                "prices": [round(p, 2) for p in date_surface.get("prices", [])],
                "pnl": [round(p, 2) for p in date_surface.get("pnl", [])],
                "breakevens": [round(b, 2) for b in date_surface.get("breakevens", [])],
                "max_profit": round(date_surface.get("max_profit"), 2) if date_surface.get("max_profit") is not None else None,
                "max_loss": round(date_surface.get("max_loss"), 2) if date_surface.get("max_loss") is not None else None
            }
            
        return {"curves": curves}
    except Exception as e:
        logger.error(f"Error computing payoff: {e}")
        raise HTTPException(status_code=400, detail=str(e))

@router.post("/greeks")
def get_aggregate_greeks(request: GreeksRequest):
    _check_tables()
    try:
        legs = _parse_legs_to_objects(request.legs)
        greeks = compute_aggregate_greeks(legs)
        return {
            "delta": round(greeks.delta, 4) if greeks.delta else 0,
            "gamma": round(greeks.gamma, 4) if greeks.gamma else 0,
            "theta": round(greeks.theta, 4) if greeks.theta else 0,
            "vega": round(greeks.vega, 4) if greeks.vega else 0,
            "rho": round(greeks.rho, 4) if greeks.rho else 0
        }
    except Exception as e:
        logger.error(f"Error computing greeks: {e}")
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
            legs_json = json.dumps([
                {
                    "contract_type": leg.contract_type,
                    "direction": leg.direction,
                    "strike": leg.strike,
                    "expiration": leg.expiration.isoformat(),
                    "quantity": leg.quantity,
                    "premium": leg.premium
                }
                for leg in strat.legs
            ])
            
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
                round(strat.max_profit, 2) if strat.max_profit is not None else None,
                round(strat.max_loss, 2) if strat.max_loss is not None else None,
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
                "reasoning": rec.reasoning
            })
            
        conn.commit()
        return {"recommendations": saved_recs}
        
    except Exception as e:
        conn.rollback()
        logger.error(f"Error recommending strategies for {ticker}: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if 'cursor' in locals() and cursor:
            cursor.close()
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
