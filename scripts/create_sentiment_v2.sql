-- ============================================================================
-- MIMIR Sentiment V2 Migration
-- Adds: weighted sentiment function, asset relationships table,
--       spillover columns on sentiment_impacts
-- ============================================================================

-- ----------------------------------------------------------------------------
-- 1. New columns on mimir_sentiment_impacts (safe to run multiple times)
-- ----------------------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'yggdrasil'
          AND table_name = 'mimir_sentiment_impacts'
          AND column_name = 'is_spillover'
    ) THEN
        ALTER TABLE yggdrasil.mimir_sentiment_impacts
        ADD COLUMN is_spillover BOOLEAN NOT NULL DEFAULT FALSE;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'yggdrasil'
          AND table_name = 'mimir_sentiment_impacts'
          AND column_name = 'spillover_source_article_id'
    ) THEN
        ALTER TABLE yggdrasil.mimir_sentiment_impacts
        ADD COLUMN spillover_source_article_id INTEGER;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'yggdrasil'
          AND table_name = 'mimir_sentiment_impacts'
          AND column_name = 'spillover_source_asset'
    ) THEN
        ALTER TABLE yggdrasil.mimir_sentiment_impacts
        ADD COLUMN spillover_source_asset VARCHAR(255);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_impacts_spillover
ON yggdrasil.mimir_sentiment_impacts (is_spillover);

CREATE INDEX IF NOT EXISTS idx_impacts_spill_source
ON yggdrasil.mimir_sentiment_impacts (spillover_source_article_id)
WHERE spillover_source_article_id IS NOT NULL;

-- ----------------------------------------------------------------------------
-- 2. Asset relationships table
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS yggdrasil.mimir_asset_relationships (
    id              SERIAL PRIMARY KEY,
    source_type     VARCHAR(32)  NOT NULL,
    source_key      VARCHAR(128) NOT NULL,
    target_type     VARCHAR(32)  NOT NULL,
    target_key      VARCHAR(128) NOT NULL,
    decay_factor    NUMERIC(4,3) NOT NULL DEFAULT 0.50,
    is_active       BOOLEAN      NOT NULL DEFAULT TRUE,
    metadata        JSONB        DEFAULT '{}',
    created_at      TIMESTAMPTZ  DEFAULT NOW(),
    updated_at      TIMESTAMPTZ  DEFAULT NOW(),

    UNIQUE (source_type, source_key, target_type, target_key)
);

CREATE INDEX IF NOT EXISTS idx_rel_source
ON yggdrasil.mimir_asset_relationships (source_type, source_key)
WHERE is_active = TRUE;

CREATE INDEX IF NOT EXISTS idx_rel_target
ON yggdrasil.mimir_asset_relationships (target_type, target_key)
WHERE is_active = TRUE;

-- ----------------------------------------------------------------------------
-- 3. Weighted sentiment function
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION yggdrasil.mimir_weighted_sentiment(
    p_ticker          TEXT DEFAULT NULL,
    p_asset_name      TEXT DEFAULT NULL,
    p_hours_window    INTEGER DEFAULT 24,
    p_half_life_hours NUMERIC DEFAULT 12,
    p_include_spillover BOOLEAN DEFAULT TRUE
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
        SELECT
            si.ticker,
            si.asset_name,
            si.sentiment_score,
            si.confidence,
            si.is_spillover,
            a.published_ts,
            -- Exponential time decay: confidence * 2^(-hours_ago / half_life)
            si.confidence * POW(0.5,
                EXTRACT(EPOCH FROM (now_ts - a.published_ts)) / 3600.0 / p_half_life_hours
            ) AS time_weight
        FROM yggdrasil.mimir_sentiment_impacts si
        JOIN yggdrasil.mimir_raw_articles a ON a.id = si.article_id
        WHERE (p_ticker IS NULL OR UPPER(si.ticker) = UPPER(p_ticker))
          AND (p_asset_name IS NULL OR si.asset_name ILIKE p_asset_name)
          AND a.published_ts > now_ts - (p_hours_window || ' hours')::INTERVAL
          AND (p_include_spillover OR si.is_spillover = FALSE)
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
