# Deprecated & Non-Essential Scripts

The scripts in this directory have been deprecated and archived. They are not essential to the active, live operations of MIMIR, but are preserved here for historical reference, schema setup history, or offline research.

## Directory Contents

### Database Setup & Migrations (Historical)
These scripts were used during development to initialize and alter the database tables, indices, and functions:
* `add_indexes.py` - Script to add indexes to databases.
* `alter_articles_scoring_status_default.py` - Migration for article default scoring status.
* `alter_fundamentals_ticker_type.py` - Migration for ticker type of corporate fundamentals.
* `alter_niche_tables.py` - Database schema adjustment for niche tables.
* `create_fundamentals_and_cost_tables.py` - Initializes cost tracking and fundamentals schema.
* `create_niche_tables.py` - Setup schema for niche asset tracking.
* `create_portfolio_table.py` - Setup database structure for portfolio records.
* `create_prices_table.py` - Creates prices and OHLCV database tables.
* `create_sentiment_v2.sql` - SQL script updating database functions and view triggers for sentiment v2.
* `create_social_chatter_table.sql` - SQL schema setup for social chatter scraping storage.
* `create_ticker_params_table.py` - Database parameters storage setup.
* `create_timescale_tables.sql` - Setup TimescaleDB hypertable definitions.
* `create_trade_signals_table.py` - Initializes structure for generated quant signals.
* `drop_old_sentiment_func.py` - Drops legacy postgres functions to avoid conflicts.
* `migrate_pair_signals.py` - Migration utility for statistical arbitrage cointegration pairs.
* `migrate_portfolio_fees.py` - Migration for portfolio slippage and broker fees.
* `migrate_portfolio_sales.py` - Migration for portfolio sales bookkeeping.
* `migrate_social.py` - Data migration script for social media record tables.
* `seed_asset_relationships.py` - Initial seed data for asset correlations and industry relationships.
* `seed_niche_assets.py` - Initial assets data seeding.

### Backfill & Offline Analysis
These scripts were used for one-off operations, backfilling historical data, or running exploratory statistical queries:
* `analyze_sentiment_predictive_power.py` - Offline Jupyter-style statistical evaluation of sentiment indicators vs price.
* `backfill_dynamic_tickers_hourly.py` - Utility to batch pull historical hourly candles for newer tickers.
* `backfill_hourly_ohlcv.py` - Batch historical hourly price backfill.
* `backfill_minute_prices.py` - Batch historical minute-level price backfill.
* `backfill_spillovers.py` - Calculates and writes historical spillover weights.
* `backfill_tickers.py` - Ingests initial ticker database listings.
* `backtest_trade_alerts.py` - Command-line test utility for manual strategy prototyping.
* `remove_async.py` - Scratch utility script that was used to strip async/await syntax from backend routers.
* `verify_data_preparation.py` - Inspection utility to test inputs before backtests are run.

### Legacy / Development Test Scripts
These are localized, non-automated test files used to verify individual modules or API connections during early implementation phases:
* `test_anomalous_volume.py`
* `test_cost_ledger.py`
* `test_dihh.py`
* `test_filter.py`
* `test_niche.py`
* `test_niche_api.py`
* `test_niche_scraper_sentiment.py`
* `test_portfolio_api.py`
* `test_portfolio_fees_and_edit.py`
* `test_rsi_debug.py`
* `test_rsi_live.py`
* `test_sentiment.py`
* `test_sentiment_overhaul.py`
* `test_sentiment_pipeline.py`
* `test_signal_fusion.py`
* `test_technical_analysis.py`
* `test_trade_alerts_api.py`
* `test_yf.py`
