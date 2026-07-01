-- scripts/create_social_chatter_table.sql
-- Migration script to add mimir_social_chatter table and update the weighted sentiment function.

-- 1. Create Social Chatter Table (Option B)
CREATE TABLE IF NOT EXISTS yggdrasil.mimir_social_chatter (
    id SERIAL PRIMARY KEY,
    platform VARCHAR(20) NOT NULL,               -- 'reddit', 'twitter'
    channel VARCHAR(100) NOT NULL,              -- subreddit name or user handle
    ticker VARCHAR(20) NOT NULL,                 -- resolved asset ticker
    asset_name VARCHAR(255) NOT NULL,           -- resolved asset name
    bucket_ts TIMESTAMPTZ NOT NULL,             -- hourly bucket timestamp
    sentiment_score NUMERIC NOT NULL,           -- aggregated/average sentiment score
    confidence NUMERIC NOT NULL,                 -- confidence score
    post_count INTEGER DEFAULT 1,                -- number of posts aggregated in this bucket
    engagement_score INTEGER DEFAULT 0,          -- sum of upvotes/retweets/likes
    summary_text TEXT,                           -- consolidated snippet of posts
    scraped_at TIMESTAMPTZ DEFAULT NOW(),
    
    UNIQUE (platform, channel, ticker, bucket_ts)
);

CREATE INDEX IF NOT EXISTS idx_social_chatter_ts ON yggdrasil.mimir_social_chatter (bucket_ts DESC);
CREATE INDEX IF NOT EXISTS idx_social_chatter_ticker ON yggdrasil.mimir_social_chatter (ticker);

-- 2. Update Weighted Sentiment Function (Nullable & Unified)
CREATE OR REPLACE FUNCTION yggdrasil.mimir_weighted_sentiment(
    p_ticker          TEXT DEFAULT NULL,
    p_asset_name      TEXT DEFAULT NULL,
    p_hours_window    INTEGER DEFAULT 24,
    p_half_life_hours NUMERIC DEFAULT 12,
    p_include_spillover BOOLEAN DEFAULT TRUE,
    p_social_half_life_hours NUMERIC DEFAULT 6,
    p_social_weight_multiplier NUMERIC DEFAULT 0.25
)
RETURNS TABLE(
    ticker                  TEXT,
    asset_name              TEXT,
    weighted_score          NUMERIC,
    direct_score            NUMERIC,
    article_count           BIGINT,
    spillover_count         BIGINT,
    avg_confidence          NUMERIC,
    effective_age_hours     NUMERIC
) AS $$
DECLARE
    now_ts TIMESTAMPTZ := NOW();
BEGIN
    RETURN QUERY
    WITH scored AS (
        -- Category A: News Articles
        SELECT
            si.ticker,
            si.asset_name,
            si.sentiment_score,
            si.confidence,
            si.is_spillover,
            a.published_ts,
            -- Exponential time decay for news
            si.confidence * POW(0.5,
                EXTRACT(EPOCH FROM (now_ts - a.published_ts)) / 3600.0 / p_half_life_hours
            ) AS time_weight
        FROM yggdrasil.mimir_sentiment_impacts si
        JOIN yggdrasil.mimir_raw_articles a ON a.id = si.article_id
        WHERE (p_ticker IS NULL OR UPPER(si.ticker) = UPPER(p_ticker))
          AND (p_asset_name IS NULL OR si.asset_name ILIKE p_asset_name)
          AND a.published_ts > now_ts - (p_hours_window || ' hours')::INTERVAL
          AND (p_include_spillover OR si.is_spillover = FALSE)

        UNION ALL

        -- Category B: Social Chatter (Decoupled, scaled down and faster decay)
        SELECT
            sc.ticker,
            sc.asset_name,
            sc.sentiment_score,
            sc.confidence * p_social_weight_multiplier AS confidence,
            FALSE AS is_spillover,
            sc.bucket_ts AS published_ts,
            -- Exponential time decay for social
            (sc.confidence * p_social_weight_multiplier) * POW(0.5,
                EXTRACT(EPOCH FROM (now_ts - sc.bucket_ts)) / 3600.0 / p_social_half_life_hours
            ) AS time_weight
        FROM yggdrasil.mimir_social_chatter sc
        WHERE (p_ticker IS NULL OR UPPER(sc.ticker) = UPPER(p_ticker))
          AND (p_asset_name IS NULL OR sc.asset_name ILIKE p_asset_name)
          AND sc.bucket_ts > now_ts - (p_hours_window || ' hours')::INTERVAL
    )
    SELECT
        s.ticker::TEXT,
        s.asset_name::TEXT,
        -- Weighted average: Σ(score * weight) / Σ(weight)
        CASE WHEN SUM(s.time_weight) > 0
            THEN ROUND((SUM(s.sentiment_score * s.time_weight) / SUM(s.time_weight))::NUMERIC, 4)
            ELSE 0.0
        END AS weighted_score,
        -- Raw average of direct-only impacts (backward compat)
        ROUND((AVG(s.sentiment_score) FILTER (WHERE NOT s.is_spillover))::NUMERIC, 4) AS direct_score,
        COUNT(*) FILTER (WHERE NOT s.is_spillover) AS article_count,
        COUNT(*) FILTER (WHERE s.is_spillover) AS spillover_count,
        ROUND((AVG(s.confidence) FILTER (WHERE NOT s.is_spillover))::NUMERIC, 4) AS avg_confidence,
        -- Effective age: weighted average of hours ago
        CASE WHEN SUM(s.time_weight) > 0
            THEN ROUND((
                SUM(EXTRACT(EPOCH FROM (now_ts - s.published_ts)) / 3600.0 * s.time_weight)
                / SUM(s.time_weight)
            )::NUMERIC, 2)
            ELSE 0.0
        END AS effective_age_hours
    FROM scored s
    GROUP BY s.ticker, s.asset_name;
END;
$$ LANGUAGE plpgsql STABLE;
