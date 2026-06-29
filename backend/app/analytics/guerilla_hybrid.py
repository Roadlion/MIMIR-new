from .cointegration import calculate_z_score, NICHE_PAIRS
from ..database import get_db_connection
from ..config import get_settings

settings = get_settings()


def _fetch_niche_sentiment():
    """Fetch avg sentiment for niche tickers from the last 24h of scored articles."""
    sql = f"""
        SELECT si.ticker, AVG(si.sentiment_score) AS avg_score
        FROM {settings.mimir_schema}.mimir_sentiment_impacts si
        JOIN {settings.mimir_schema}.mimir_raw_articles a ON a.id = si.article_id
        WHERE si.ticker IN (
            SELECT ticker FROM {settings.mimir_schema}.mimir_niche_assets
        )
        AND a.published_ts > NOW() - INTERVAL '24 hours'
        GROUP BY si.ticker
    """
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(sql)
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return {row[0]: float(row[1]) for row in rows} if rows else {}
    except Exception as e:
        print(f"[guerilla_hybrid] sentiment fetch failed: {e}")
        return {}


def _upsert_signal(conn, t1, t2, z_score, mean_spread, current_spread, status, conviction):
    """Insert or update a row in mimir_pair_signals."""
    cur = conn.cursor()
    cur.execute(f"""
        INSERT INTO {settings.mimir_schema}.mimir_pair_signals
            (ticker1, ticker2, signal_date, z_score, p_value, status, mean_spread, current_spread, conviction)
        VALUES (%s, %s, NOW(), %s, 0, %s, %s, %s, %s)
    """, (t1, t2, z_score, status, mean_spread, current_spread, conviction))
    cur.close()


def get_hybrid_signals():
    """
    Scan NICHE_PAIRS for stat-arb opportunities, overlay DB-sourced sentiment,
    persist results to mimir_pair_signals, and return the opportunity list.
    """
    sentiment = _fetch_niche_sentiment()

    opportunities = []
    conn = get_db_connection()
    try:
        for t1, t2 in NICHE_PAIRS:
            res = calculate_z_score(t1, t2)
            if not res:
                continue

            s1 = sentiment.get(t1, 0.0)
            s2 = sentiment.get(t2, 0.0)
            sentiment_delta = s2 - s1
            z_score = res["z_score"]
            signal = res["signal"]

            conviction = "LOW"
            if signal.startswith("SHORT"):
                if sentiment_delta > 0.2:
                    conviction = "HIGH (Math + Sentiment Aligned)"
                elif sentiment_delta < -0.2:
                    conviction = "WARNING (Sentiment Contradicts Math)"
                else:
                    conviction = "MEDIUM (Math Only)"
            elif signal.startswith("LONG"):
                if sentiment_delta < -0.2:
                    conviction = "HIGH (Math + Sentiment Aligned)"
                elif sentiment_delta > 0.2:
                    conviction = "WARNING (Sentiment Contradicts Math)"
                else:
                    conviction = "MEDIUM (Math Only)"

            res["sentiment_t1"] = round(s1, 2)
            res["sentiment_t2"] = round(s2, 2)
            res["conviction"] = conviction

            _upsert_signal(
                conn, t1, t2,
                z_score=res["z_score"],
                mean_spread=res["mean_spread"],
                current_spread=res["current_spread"],
                status=res["status"],
                conviction=conviction,
            )

            opportunities.append(res)

        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"[guerilla_hybrid] error persisting signals: {e}")
    finally:
        conn.close()

    return opportunities
