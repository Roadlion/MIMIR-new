from fastapi import APIRouter
from pydantic import BaseModel
from typing import List, Optional
from ..analytics.guerilla_hybrid import get_hybrid_signals

router = APIRouter()

class Opportunity(BaseModel):
    pair: str
    z_score: float
    mean_spread: float
    current_spread: float
    signal: str
    status: str
    sentiment_t1: Optional[float] = 0.0
    sentiment_t2: Optional[float] = 0.0
    conviction: Optional[str] = "LOW"

class NicheResponse(BaseModel):
    opportunities: List[Opportunity]

@router.get("/niche/opportunities", response_model=NicheResponse)
def get_niche_opportunities():
    """
    Returns the latest Guerilla Quant Stat-Arb opportunities with Hybrid Sentiment.
    """
    results = get_hybrid_signals()
    return {"opportunities": results}
