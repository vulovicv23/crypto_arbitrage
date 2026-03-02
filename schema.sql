-- =========================================================================
-- Crypto Arbitrage Bot — PostgreSQL Schema
-- =========================================================================
-- Run automatically on first container start via docker-entrypoint-initdb.d.
-- Safe to re-run: all statements use IF NOT EXISTS.
-- =========================================================================


-- -------------------------------------------------------------------------
-- 1. btc_klines: Historical 1-second OHLCV candles from Binance
-- -------------------------------------------------------------------------
-- Binance uses millisecond timestamps. Each row is one 1-second candle.
-- 12 months ≈ 31.5 M rows ≈ 2–3 GB with indexes.

CREATE TABLE IF NOT EXISTS btc_klines (
    open_time_ms    BIGINT       NOT NULL,
    interval        TEXT         NOT NULL DEFAULT '1s',
    open            DOUBLE PRECISION NOT NULL,
    high            DOUBLE PRECISION NOT NULL,
    low             DOUBLE PRECISION NOT NULL,
    close           DOUBLE PRECISION NOT NULL,
    volume          DOUBLE PRECISION NOT NULL,
    trades_count    INTEGER      NOT NULL,
    close_time_ms   BIGINT       NOT NULL,
    PRIMARY KEY (open_time_ms, interval)
);

-- Covering index for time-range scans during feature computation.
CREATE INDEX IF NOT EXISTS idx_btc_klines_time
    ON btc_klines (open_time_ms);


-- -------------------------------------------------------------------------
-- 2. ml_labels: Pre-computed labels for training (optional cache)
-- -------------------------------------------------------------------------
-- Labels can also be computed on-the-fly via numpy array shifting.
-- This table is a convenience for SQL-based exploration / ad-hoc queries.

CREATE TABLE IF NOT EXISTS ml_labels (
    open_time_ms    BIGINT       PRIMARY KEY,
    price_at_t      DOUBLE PRECISION NOT NULL,
    price_5m        DOUBLE PRECISION,          -- close at t + 300 s
    price_15m       DOUBLE PRECISION,          -- close at t + 900 s
    label_5m        SMALLINT,                  -- 1 = up, 0 = down, NULL = no data
    label_15m       SMALLINT,
    return_5m       DOUBLE PRECISION,
    return_15m      DOUBLE PRECISION
);


-- -------------------------------------------------------------------------
-- 3. ml_model_runs: Track training experiments
-- -------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS ml_model_runs (
    id                  SERIAL       PRIMARY KEY,
    model_version       TEXT         NOT NULL,
    horizon_s           INTEGER      NOT NULL,
    train_start_ms      BIGINT       NOT NULL,
    train_end_ms        BIGINT       NOT NULL,
    test_start_ms       BIGINT       NOT NULL,
    test_end_ms         BIGINT       NOT NULL,
    brier_score         DOUBLE PRECISION,
    log_loss            DOUBLE PRECISION,
    accuracy            DOUBLE PRECISION,
    auc_roc             DOUBLE PRECISION,
    n_train             INTEGER,
    n_test              INTEGER,
    hyperparams         JSONB,
    feature_importance  JSONB,
    created_at          TIMESTAMPTZ  DEFAULT NOW()
);
