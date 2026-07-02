# backend/app/routers/backtest.py
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any

from ..analytics.backtester import BacktestEngine
from ..database import get_db_connection

router = APIRouter(prefix="/api/v1/backtest", tags=["backtest"])

class BacktestRequest(BaseModel):
    formula: str = Field(..., example="neutralize(scale(sentiment)) - neutralize(scale(delay(returns(5), 1)))")
    start_date: str = Field("2025-06-01", description="YYYY-MM-DD")
    end_date: str = Field("2026-07-02", description="YYYY-MM-DD")
    holding_period: int = Field(1, ge=1, description="Decay holding period in days")
    slippage_bps: float = Field(5.0, ge=0.0, description="Transaction fee in basis points")
    universe: str = Field("core", description="core (benchmarks) or all (dynamic)")
    style: str = Field("long_short", description="long_short or long_only")
    portfolio_size: Optional[int] = Field(None, description="Trade top/bottom N assets. None to trade all.")
    markets: Optional[List[str]] = Field(None, description="Filter for specific markets: e.g. us, china, korea, japan, thailand, crypto, forex, commodity")

class MetricResponse(BaseModel):
    sharpe: float
    annualized_return: float
    max_drawdown: float
    turnover: float
    win_rate: float
    ic: float
    fitness: float

class ChartDataPoint(BaseModel):
    date: str
    strategy: float
    benchmark: float
    drawdown: float

class TradeLogItem(BaseModel):
    date: str
    ticker: str
    action: str
    weight: float
    price: float

class BacktestResponse(BaseModel):
    metrics: MetricResponse
    chart: List[ChartDataPoint]
    trades: List[TradeLogItem]

@router.post("/run", response_model=BacktestResponse)
def run_backtest(req: BacktestRequest):
    """
    Executes a vectorized quant strategy backtest using daily aggregates and formula parsing.
    Returns Sharpe Ratio, returns curves, Max Drawdown, and recent executed trades.
    Saves metrics results to database history table.
    """
    try:
        engine = BacktestEngine(
            start_date=req.start_date,
            end_date=req.end_date,
            universe=req.universe,
            markets=req.markets
        )
        result = engine.run(
            formula=req.formula,
            holding_period=req.holding_period,
            slippage_bps=req.slippage_bps,
            style=req.style,
            portfolio_size=req.portfolio_size
        )
        
        # Save run statistics to database
        conn = get_db_connection()
        cur = conn.cursor()
        try:
            # We map float("-inf") to -999.99 for database storage compatibility
            m = result["metrics"]
            def clean_db_val(v):
                import math
                if math.isinf(v) or math.isnan(v):
                    return -999.99
                return v

            cur.execute("""
                INSERT INTO yggdrasil.mimir_backtest_history (
                    formula, universe, style, start_date, end_date, holding_period, slippage_bps, portfolio_size, markets,
                    sharpe, annualized_return, max_drawdown, turnover, fitness, win_rate, ic
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                req.formula, req.universe, req.style, req.start_date, req.end_date, req.holding_period, req.slippage_bps, req.portfolio_size,
                req.markets or [],
                clean_db_val(m["sharpe"]), clean_db_val(m["annualized_return"]), clean_db_val(m["max_drawdown"]),
                clean_db_val(m["turnover"]), clean_db_val(m["fitness"]), clean_db_val(m["win_rate"]), clean_db_val(m["ic"])
            ))
            conn.commit()
        except Exception as db_err:
            conn.rollback()
            print(f"[BACKTEST] DB save failed: {db_err}")
        finally:
            cur.close()
            conn.close()

        return result
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=f"Simulation Error: {str(ve)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal Server Error: {str(e)}")

@router.get("/history")
def get_backtest_history():
    """Returns previous backtests ordered by execution date."""
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT id, formula, universe, style, start_date, end_date, holding_period, slippage_bps, portfolio_size, markets,
                   sharpe, annualized_return, max_drawdown, turnover, fitness, win_rate, ic, created_at
            FROM yggdrasil.mimir_backtest_history
            ORDER BY created_at DESC
            LIMIT 50
        """)
        rows = cur.fetchall()
        history = []
        for row in rows:
            history.append({
                "id": row[0],
                "formula": row[1],
                "universe": row[2],
                "style": row[3],
                "start_date": str(row[4]),
                "end_date": str(row[5]),
                "holding_period": row[6],
                "slippage_bps": float(row[7]),
                "portfolio_size": row[8],
                "markets": row[9],
                "sharpe": float(row[10]) if row[10] is not None else None,
                "annualized_return": float(row[11]) if row[11] is not None else None,
                "max_drawdown": float(row[12]) if row[12] is not None else None,
                "turnover": float(row[13]) if row[13] is not None else None,
                "fitness": float(row[14]) if row[14] is not None else None,
                "win_rate": float(row[15]) if row[15] is not None else None,
                "ic": float(row[16]) if row[16] is not None else None,
                "created_at": row[17].strftime("%Y-%m-%d %H:%M:%S")
            })
        return history
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"Database query failed: {e}")
    finally:
        cur.close()
        conn.close()
