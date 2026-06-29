from fastapi import APIRouter, Query
from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime, timezone, timedelta
from ..database import get_db_connection, get_db_connection_dict
from ..config import get_settings
from ..analytics.guerilla_hybrid import get_hybrid_signals

router = APIRouter()
settings = get_settings()


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


class SignalRow(BaseModel):
    pair_id: int
    ticker1: str
    ticker2: str
    signal_date: datetime
    z_score: float
    mean_spread: Optional[float] = None
    current_spread: Optional[float] = None
    status: str
    conviction: Optional[str] = None


class SignalHistoryResponse(BaseModel):
    signals: List[SignalRow]


class NicheStats(BaseModel):
    active_pairs: int
    high_conviction_sigs: int
    last_scan: Optional[str] = None
    sources_count: int


class NicheArticle(BaseModel):
    id: int
    title: str
    summary: Optional[str] = None
    source_name: str
    published_ts: Optional[datetime] = None
    ticker: str
    sentiment_score: float
    direction: str


class NicheArticlesResponse(BaseModel):
    articles: List[NicheArticle]


@router.get("/niche/opportunities", response_model=NicheResponse)
def get_niche_opportunities():
    """
    Return cached pair signals from the last hour, or compute fresh if none exist.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
    conn = get_db_connection_dict()
    try:
        cur = conn.cursor()
        # Latest signal per pair in the last hour
        cur.execute(f"""
            SELECT DISTINCT ON (ticker1, ticker2)
                ticker1, ticker2, z_score, mean_spread, current_spread,
                status, conviction, signal_date
            FROM {settings.mimir_schema}.mimir_pair_signals
            WHERE signal_date >= %s
            ORDER BY ticker1, ticker2, signal_date DESC
        """, (cutoff,))
        rows = cur.fetchall()
        cur.close()

        if rows:
            opps = []
            for r in rows:
                t1, t2 = r["ticker1"], r["ticker2"]
                z = float(r["z_score"])
                signal = "WAIT"
                if z > 2.0:
                    signal = f"SHORT {t1}, LONG {t2}"
                elif z < -2.0:
                    signal = f"LONG {t1}, SHORT {t2}"

                opps.append(Opportunity(
                    pair=f"{t1} / {t2}",
                    z_score=z,
                    mean_spread=float(r["mean_spread"]) if r["mean_spread"] else 0,
                    current_spread=float(r["current_spread"]) if r["current_spread"] else 0,
                    signal=signal,
                    status=r["status"],
                    conviction=r["conviction"] or "LOW",
                ))
            return NicheResponse(opportunities=opps)
    except Exception as e:
        print(f"[niche] DB read failed, falling back to live scan: {e}")
    finally:
        conn.close()

    # Fallback: live scan
    results = get_hybrid_signals()
    return NicheResponse(opportunities=[Opportunity(**r) for r in results])


@router.get("/niche/signals", response_model=SignalHistoryResponse)
def get_niche_signal_history(
    days: int = Query(30, ge=1, le=365),
    limit: int = Query(50, ge=1, le=200),
):
    """Return historical pair signals from mimir_pair_signals."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    conn = get_db_connection_dict()
    try:
        cur = conn.cursor()
        cur.execute(f"""
            SELECT pair_id, ticker1, ticker2, signal_date, z_score,
                   mean_spread, current_spread, status, conviction
            FROM {settings.mimir_schema}.mimir_pair_signals
            WHERE signal_date >= %s
            ORDER BY signal_date DESC
            LIMIT %s
        """, (cutoff, limit))
        rows = cur.fetchall()
        cur.close()
        signals = []
        for r in rows:
            signals.append(SignalRow(
                pair_id=r["pair_id"],
                ticker1=r["ticker1"],
                ticker2=r["ticker2"],
                signal_date=r["signal_date"],
                z_score=float(r["z_score"]),
                mean_spread=float(r["mean_spread"]) if r["mean_spread"] else None,
                current_spread=float(r["current_spread"]) if r["current_spread"] else None,
                status=r["status"],
                conviction=r["conviction"],
            ))
        return SignalHistoryResponse(signals=signals)
    except Exception as e:
        print(f"[niche] signal history error: {e}")
        return SignalHistoryResponse(signals=[])
    finally:
        conn.close()


@router.get("/niche/stats", response_model=NicheStats)
def get_niche_stats():
    """Return real-time stats for the Guerilla Quant page."""
    conn = get_db_connection_dict()
    try:
        cur = conn.cursor()

        # Active pairs
        cur.execute(f"SELECT COUNT(*) AS c FROM {settings.mimir_schema}.mimir_niche_assets")
        pair_count = cur.fetchone()["c"] // 2  # approximate pair count

        # High conviction signals in last 24h
        cur.execute(f"""
            SELECT COUNT(*) AS c FROM {settings.mimir_schema}.mimir_pair_signals
            WHERE signal_date > NOW() - INTERVAL '24 hours'
              AND conviction ILIKE 'HIGH%'
        """)
        high_conv = cur.fetchone()["c"]

        # Last scan time
        cur.execute(f"""
            SELECT MAX(signal_date) AS last_scan
            FROM {settings.mimir_schema}.mimir_pair_signals
        """)
        last_scan = cur.fetchone()["last_scan"]

        # Sources count (count distinct source_name for niche articles)
        cur.execute(f"""
            SELECT COUNT(DISTINCT source_name) AS c
            FROM {settings.mimir_schema}.mimir_raw_articles
            WHERE source_name LIKE 'niche-%'
              AND scraped_at > NOW() - INTERVAL '7 days'
        """)
        sources_count = cur.fetchone()["c"]

        cur.close()
        return NicheStats(
            active_pairs=pair_count,
            high_conviction_sigs=high_conv,
            last_scan=str(last_scan) if last_scan else None,
            sources_count=sources_count,
        )
    except Exception as e:
        print(f"[niche] stats error: {e}")
        return NicheStats(active_pairs=0, high_conviction_sigs=0, sources_count=0)
    finally:
        conn.close()


@router.get("/niche/articles", response_model=NicheArticlesResponse)
def get_niche_articles(
    ticker1: str = Query(None),
    ticker2: str = Query(None),
    days: int = Query(7, ge=1, le=90),
    limit: int = Query(20, ge=1, le=100),
):
    """Return articles that mention or impact the given tickers."""
    tickers = []
    if ticker1:
        tickers.append(ticker1.upper())
    if ticker2:
        tickers.append(ticker2.upper())

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    conn = get_db_connection_dict()
    try:
        cur = conn.cursor()

        if tickers:
            cur.execute(f"""
                SELECT a.id, a.title, a.summary, a.source_name, a.published_ts,
                       si.ticker, si.sentiment_score, si.direction
                FROM {settings.mimir_schema}.mimir_raw_articles a
                JOIN {settings.mimir_schema}.mimir_sentiment_impacts si ON si.article_id = a.id
                WHERE si.ticker = ANY(%s)
                  AND a.published_ts > %s
                ORDER BY a.published_ts DESC
                LIMIT %s
            """, (tickers, cutoff, limit))
        else:
            # Return all niche-sourced articles
            cur.execute(f"""
                SELECT a.id, a.title, a.summary, a.source_name, a.published_ts,
                       'N/A' AS ticker, 0.0 AS sentiment_score, 'neutral' AS direction
                FROM {settings.mimir_schema}.mimir_raw_articles a
                WHERE a.source_name LIKE 'niche-%'
                  AND a.published_ts > %s
                ORDER BY a.published_ts DESC
                LIMIT %s
            """, (cutoff, limit))

        rows = cur.fetchall()
        cur.close()

        articles = []
        for r in rows:
            articles.append(NicheArticle(
                id=r["id"],
                title=r["title"] or "",
                summary=r["summary"],
                source_name=r["source_name"],
                published_ts=r["published_ts"],
                ticker=r["ticker"],
                sentiment_score=float(r["sentiment_score"]) if r["sentiment_score"] else 0.0,
                direction=r["direction"] or "neutral",
            ))
        return NicheArticlesResponse(articles=articles)
    except Exception as e:
        print(f"[niche] articles error: {e}")
        return NicheArticlesResponse(articles=[])
    finally:
        conn.close()
