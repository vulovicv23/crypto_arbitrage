# Configuration Reference

Source: `config.py`

All configuration uses frozen dataclasses. Secrets and tunable parameters are loaded from environment variables (`.env` file via `python-dotenv`). See `.env.example` for a complete template.

## AppConfig (Root)

Aggregates all sub-configurations:

```python
@dataclass(frozen=True)
class AppConfig:
    polymarket: PolymarketConfig
    discovery: DiscoveryConfig
    predictions: PredictionSourcesConfig
    strategy: StrategyConfig
    risk: RiskConfig
    execution: ExecutionConfig
    dry_run: DryRunConfig
    ml: MLConfig
    logging: LoggingConfig
```

## PolymarketConfig

Polymarket CLOB API connection and authentication.

| Parameter | Env Var | Default | Description |
|-----------|---------|---------|-------------|
| `rest_url` | `POLY_REST_URL` | `https://clob.polymarket.com` | REST API base URL |
| `ws_url` | `POLY_WS_URL` | `wss://ws-subscriptions-clob.polymarket.com/ws/market` | WebSocket URL |
| `api_key` | `POLY_API_KEY` | (required) | API key |
| `api_secret` | `POLY_API_SECRET` | "" | API secret for HMAC signing |
| `api_passphrase` | `POLY_API_PASSPHRASE` | "" | API passphrase |
| `chain_id` | `POLY_CHAIN_ID` | 137 (Polygon) | Blockchain chain ID |
| `private_key` | `POLY_PRIVATE_KEY` | (required) | Ethereum private key for order signing |
| `btc_condition_ids` | `POLY_BTC_CONDITION_IDS` | (required) | Comma-separated condition IDs to monitor |

**Validation:** `POLY_API_KEY`, `POLY_PRIVATE_KEY`, and at least one `POLY_BTC_CONDITION_IDS` are required. In `--dry-run` mode, missing keys are allowed.

## PredictionSourcesConfig

External price feed configuration.

| Parameter | Env Var | Default | Description |
|-----------|---------|---------|-------------|
| `binance_ws_url` | — | `wss://stream.binance.com:9443/ws/btcusdt@trade` | Binance WebSocket URL |
| `cryptocompare_api_key` | `CRYPTOCOMPARE_API_KEY` | "" | CryptoCompare API key (optional) |
| `cryptocompare_url` | — | CryptoCompare BTC/USD endpoint | REST API URL |
| `coingecko_url` | — | CoinGecko BTC/USD endpoint | REST API URL |
| `rest_poll_interval` | `REST_POLL_INTERVAL` | 1.0 | Polling interval for REST feeds (seconds) |

## StrategyConfig

Trading strategy parameters.

| Parameter | Env Var | Default | Description |
|-----------|---------|---------|-------------|
| `min_edge_threshold` | `MIN_EDGE_THRESHOLD` | 0.003 (0.3%) | Minimum edge to trigger a trade |
| `max_edge_threshold` | `MAX_EDGE_THRESHOLD` | 0.05 (5%) | Maximum edge (above = likely stale) |
| `prediction_horizon_s` | `PREDICTION_HORIZON_S` | 900 (15 min) | Prediction horizon |
| `ema_fast_span` | `EMA_FAST_SPAN` | 12 | Fast EMA span for trend detection |
| `ema_slow_span` | `EMA_SLOW_SPAN` | 26 | Slow EMA span for trend detection |
| `volatility_window` | `VOLATILITY_WINDOW` | 60 | Ticks for volatility calculation |
| `confidence_scale` | — | True | Scale position by signal confidence |

## RiskConfig

Risk management parameters.

| Parameter | Env Var | Default | Description |
|-----------|---------|---------|-------------|
| `max_position_pct` | `MAX_POSITION_PCT` | 0.005 (0.5%) | Max capital per single trade |
| `max_daily_loss_pct` | `MAX_DAILY_LOSS_PCT` | 0.02 (2%) | Daily drawdown halt trigger |
| `max_open_positions` | `MAX_OPEN_POSITIONS` | 20 | Max concurrent open positions |
| `max_total_exposure_pct` | `MAX_TOTAL_EXPOSURE_PCT` | 0.10 (10%) | Max total exposure as fraction of capital |
| `cooldown_after_losses` | `COOLDOWN_AFTER_LOSSES` | 5 | Consecutive losses before cooldown |
| `cooldown_duration_s` | `COOLDOWN_DURATION_S` | 30.0 | Cooldown duration (seconds) |
| `sideways_size_multiplier` | `SIDEWAYS_SIZE_MULTIPLIER` | 0.4 | Size reduction in sideways markets |
| `trend_size_multiplier` | `TREND_SIZE_MULTIPLIER` | 1.0 | Size multiplier in trending markets |

## ExecutionConfig

Latency and rate limiting.

| Parameter | Env Var | Default | Description |
|-----------|---------|---------|-------------|
| `max_latency_ms` | `MAX_LATENCY_MS` | 100 | Detection-to-trade latency budget (ms) |
| `max_orders_per_second` | `MAX_ORDERS_PER_SECOND` | 50 | Order rate cap |
| `max_retries` | — | 3 | Retry attempts for transient failures |
| `retry_backoff_base_ms` | — | 10 | Base backoff for retries (ms) |
| `http_pool_size` | `HTTP_POOL_SIZE` | 20 | aiohttp connection pool size |

## LoggingConfig

Logging setup.

| Parameter | Env Var | Default | Description |
|-----------|---------|---------|-------------|
| `level` | `LOG_LEVEL` | INFO | Log level |
| `log_dir` | — | `./logs` | Log directory |
| `log_file` | — | `bot.log` | Main log file (rotating) |
| `trade_log_file` | — | `trades.jsonl` | Trade log (JSON lines) |
| `max_bytes` | — | 50,000,000 (50MB) | Max log file size |
| `backup_count` | — | 10 | Number of rotated backups |

## MLConfig

ML prediction pipeline (optional, disabled by default).

| Parameter | Env Var | Default | Description |
|-----------|---------|---------|-------------|
| `enabled` | `ML_ENABLED` | false | Enable ML prediction pipeline |
| `model_path` | `ML_MODEL_PATH` | `models/btc_5m_v2.pkl` | Path to trained .pkl artifact |
| `feature_window` | `ML_FEATURE_WINDOW` | 4000 | Rolling buffer size (seconds) |
| `prediction_interval` | `ML_PREDICTION_INTERVAL` | 0.25 | Prediction emit interval (seconds) |
| `min_confidence` | `ML_MIN_CONFIDENCE` | 0.1 | Minimum confidence to emit signal |
| `max_predicted_return` | `ML_MAX_PREDICTED_RETURN` | 0.01 (1%) | Cap on predicted price magnitude |
| `horizon_s` | `ML_HORIZON_S` | 300 (5 min) | Prediction horizon matching training labels |
