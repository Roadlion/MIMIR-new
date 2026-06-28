-- Attempt to enable TimescaleDB extension if available
DO $$
BEGIN
    CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;
EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'TimescaleDB extension is not installed on this system. Falling back to standard PostgreSQL table.';
END $$;

-- Create the mimir_minute_ohlcv table if it doesn't exist
CREATE TABLE IF NOT EXISTS yggdrasil.mimir_minute_ohlcv (
    ticker VARCHAR(20) NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL,
    open NUMERIC NOT NULL,
    high NUMERIC NOT NULL,
    low NUMERIC NOT NULL,
    close NUMERIC NOT NULL,
    volume BIGINT NOT NULL,
    scraped_at TIMESTAMPTZ DEFAULT NOW()
);

-- Convert to a hypertable (ignore error if TimescaleDB is not installed or already a hypertable)
DO $$
BEGIN
    -- Check if TimescaleDB extension is actually loaded
    IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') THEN
        IF NOT EXISTS (
            SELECT 1 FROM _timescaledb_catalog.hypertable 
            WHERE table_name = 'mimir_minute_ohlcv' AND schema_name = 'yggdrasil'
        ) THEN
            PERFORM create_hypertable('yggdrasil.mimir_minute_ohlcv', 'timestamp');
            RAISE NOTICE 'Successfully created TimescaleDB hypertable for mimir_minute_ohlcv.';
        END IF;
    ELSE
        RAISE NOTICE 'TimescaleDB extension not loaded. Table remains a standard PostgreSQL table.';
    END IF;
EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'Could not create hypertable: %', SQLERRM;
END $$;

-- Create a unique index on (ticker, timestamp) to support ON CONFLICT
CREATE UNIQUE INDEX IF NOT EXISTS idx_mimir_minute_ohlcv_ticker_timestamp 
ON yggdrasil.mimir_minute_ohlcv (ticker, timestamp);

-- Set compression and retention policies only if TimescaleDB is active
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') THEN
        ALTER TABLE yggdrasil.mimir_minute_ohlcv SET (
            timescaledb.compress,
            timescaledb.compress_segmentby = 'ticker'
        );
        PERFORM add_compression_policy('yggdrasil.mimir_minute_ohlcv', INTERVAL '7 days', if_not_exists => true);
        PERFORM add_retention_policy('yggdrasil.mimir_minute_ohlcv', INTERVAL '14 days', if_not_exists => true);
        RAISE NOTICE 'Successfully configured compression and retention policies.';
    END IF;
EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'Could not set TimescaleDB policies: %', SQLERRM;
END $$;
