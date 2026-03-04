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
    fees: FeeConfig
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

**Validation:** `POLY_API_KEY` and `POLY_PRIVATE_KEY` are required. `POLY_BTC_CONDITION_IDS` is required only when `DISCOVERY_ENABLED=false`.

## PredictionSourcesConfig

External price feed configuration.

| Parameter | Env Var | Default | Description |
|-----------|---------|---------|-------------|
| `binance_ws_url` | — | `wss://stream.binance.com:9443/ws/btcusdt@trade` | Binance WebSocket URL |
| `cryptocompare_api_key` | `CRYPTOCOMPARE_API_KEY` | "" | CryptoCompare API key (enables WS mode) |
| `cryptocompare_ws_url` | `CRYPTOCOMPARE_WS_URL` | `wss://streamer.cryptocompare.com/v2` | CryptoCompare WebSocket URL (CCCAGG stream) |
| `cryptocompare_url` | — | CryptoCompare BTC/USD endpoint | REST API URL (fallback when no API key) |
| `coingecko_url` | — | CoinGecko BTC/USD endpoint | REST API URL |
| `rest_poll_interval` | `REST_POLL_INTERVAL` | 1.0 | Polling interval for REST feeds (seconds) |

**Note:** CoinGecko poll interval is enforced to `max(rest_poll_interval * 2, 30s)` to respect free-tier rate limits.

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
| `expiry_buckets_enabled` | `EXPIRY_BUCKETS_ENABLED` | false | Enable time-dependent edge thresholds |
| `near_expiry_s` | `NEAR_EXPIRY_S` | 120 | Near-expiry bucket boundary (seconds) |
| `far_expiry_s` | `FAR_EXPIRY_S` | 600 | Far-expiry bucket boundary (seconds) |
| `near_min_edge` | `NEAR_MIN_EDGE` | 0.01 | Min edge for near-expiry bucket |
| `near_max_edge` | `NEAR_MAX_EDGE` | 0.50 | Max edge for near-expiry bucket |
| `near_size_mult` | `NEAR_SIZE_MULT` | 1.2 | Size multiplier for near-expiry bucket |
| `far_min_edge` | `FAR_MIN_EDGE` | 0.03 | Min edge for far-expiry bucket |
| `far_max_edge` | `FAR_MAX_EDGE` | 0.25 | Max edge for far-expiry bucket |
| `far_size_mult` | `FAR_SIZE_MULT` | 0.7 | Size multiplier for far-expiry bucket |
| `max_ttl_multiplier` | `MAX_TTL_MULTIPLIER` | 4 | Max TTL as multiple of timeframe (5m×4=20min, 15m×4=60min) |

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
| `trend_size_multiplier` | `TREND_SIZE_MULTIPLIER` | 1.0 | Size multiplier in trending-down markets |
| `trending_up_size_multiplier` | `TRENDING_UP_SIZE_MULTIPLIER` | 0.5 | Size multiplier in trending-up markets (reduced — ML underperforms in uptrends) |
| `moderate_strength_multiplier` | `MODERATE_STRENGTH_MULTIPLIER` | 0.4 | Size multiplier for MODERATE strength signals (data shows worst-performing class) |
| `weak_strength_multiplier` | `WEAK_STRENGTH_MULTIPLIER` | 0.5 | Size multiplier for WEAK strength signals (0 = skip WEAK trades entirely) |

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

## FeeConfig

Polymarket fee modeling. Fees are charged only on the profit portion of trades (losing trades pay no fee).

| Parameter | Env Var | Default | Description |
|-----------|---------|---------|-------------|
| `taker_fee_pct` | `TAKER_FEE_PCT` | 0.02 (2%) | Taker fee rate on profit |
| `maker_fee_pct` | `MAKER_FEE_PCT` | 0.01 (1%) | Maker fee rate on profit |

## MLConfig

ML prediction pipeline (optional, disabled by default).

| Parameter | Env Var | Default | Description |
|-----------|---------|---------|-------------|
| `enabled` | `ML_ENABLED` | false | Enable ML prediction pipeline |
| `model_path` | `ML_MODEL_PATH` | `models/btc_5m_v3.pkl` | Path to trained .pkl artifact |
| `model_type` | `ML_MODEL_TYPE` | `"regression"` | Model type: `"regression"` (v4+) or `"classification"` (v3) |
| `feature_window` | `ML_FEATURE_WINDOW` | 4000 | Rolling buffer size (seconds) |
| `prediction_interval` | `ML_PREDICTION_INTERVAL` | 0.25 | Prediction emit interval (seconds) |
| `min_confidence` | `ML_MIN_CONFIDENCE` | 0.1 | Minimum confidence to emit signal (classification only) |
| `min_predicted_return` | `ML_MIN_PREDICTED_RETURN` | 0.0001 | Minimum absolute predicted return to emit signal (regression noise gate) |
| `max_predicted_return` | `ML_MAX_PREDICTED_RETURN` | 0.01 (1%) | Cap on predicted price magnitude |
| `horizon_s` | `ML_HORIZON_S` | 300 (5 min) | Prediction horizon matching training labels |

## Startup Validation

`load_config()` calls `_validate()` which performs comprehensive semantic checks. The bot fails fast with clear error messages if any constraint is violated.

| Category | Checks |
|----------|--------|
| **Auth** | `POLY_API_KEY` and `POLY_PRIVATE_KEY` required; condition IDs required when discovery disabled |
| **Strategy** | `min_edge > 0`, `max_edge > min_edge`, `max_spread ∈ (0, 1)`, `near_expiry_s < far_expiry_s` |
| **Risk** | All percentage params in `(0, 1)`, `max_open_positions ≥ 1`, `cooldown_after_losses ≥ 1`, `cooldown_duration_s > 0` |
| **Execution** | `max_latency_ms > 0`, `max_orders_per_second ≥ 1` |
| **Fees** | `taker_fee_pct ∈ [0, 1)`, `maker_fee_pct ∈ [0, 1)` |
| **ML** | When enabled: `feature_window ≥ 100`, `prediction_interval > 0`, `min_confidence ∈ [0, 1]`, `model_type ∈ {classification, regression}`, `min_predicted_return ≥ 0` |
