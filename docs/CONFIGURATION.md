# Configuration Reference

> **Source file:** `config.py`
> **Loading:** `load_config()` returns an `AppConfig` instance
> **Mechanism:** Frozen dataclasses with `os.getenv()` defaults, `.env` file via `python-dotenv`

All configuration is defined as frozen (immutable) dataclasses in `config.py`. Values are loaded from environment variables at import time. A `.env` file in the project root is automatically loaded via `python-dotenv`.

---

## Table of Contents

1. [How Config Loading Works](#how-config-loading-works)
2. [AppConfig Structure](#appconfig-structure)
3. [PolymarketConfig](#polymarketconfig)
4. [DiscoveryConfig](#discoveryconfig)
5. [PredictionSourcesConfig](#predictionsourcesconfig)
6. [StrategyConfig](#strategyconfig)
7. [RiskConfig](#riskconfig)
8. [ExecutionConfig](#executionconfig)
9. [LoggingConfig](#loggingconfig)
10. [Validation Rules](#validation-rules)
11. [Complete Environment Variable Reference](#complete-environment-variable-reference)

---

## How Config Loading Works

1. On import of `config.py`, `python-dotenv` loads `.env` from the project root (`Path(__file__).parent / ".env"`).
2. Each dataclass field calls `os.getenv("VAR_NAME", "default")` at class definition time.
3. `load_config()` instantiates `AppConfig()` (which cascades to all sub-configs) and calls `_validate()`.
4. All dataclasses are `frozen=True`, meaning fields cannot be modified after creation.

```python
def load_config() -> AppConfig:
    cfg = AppConfig()
    _validate(cfg)
    return cfg
```

**In dry-run mode**, if validation fails (e.g., missing API keys), `main.py` catches the `ValueError` and creates an unvalidated `AppConfig()` with a warning.

---

## AppConfig Structure

```python
@dataclass(frozen=True)
class AppConfig:
    polymarket: PolymarketConfig
    discovery: DiscoveryConfig
    predictions: PredictionSourcesConfig
    strategy: StrategyConfig
    risk: RiskConfig
    execution: ExecutionConfig
    logging: LoggingConfig
```

---

## PolymarketConfig

Controls the Polymarket CLOB API connection (REST + WebSocket) and market discovery API.

| Field                | Env Variable            | Type        | Default                                                    | Description                                        |
|----------------------|-------------------------|-------------|------------------------------------------------------------|----------------------------------------------------|
| `rest_url`           | `POLY_REST_URL`         | `str`       | `https://clob.polymarket.com`                              | CLOB REST API base URL                             |
| `ws_url`             | `POLY_WS_URL`           | `str`       | `wss://ws-subscriptions-clob.polymarket.com/ws/market`     | CLOB WebSocket URL for book updates                |
| `gamma_url`          | `POLY_GAMMA_URL`        | `str`       | `https://gamma-api.polymarket.com`                         | Gamma API URL for market discovery                 |
| `api_key`            | `POLY_API_KEY`          | `str`       | `""`                                                       | API key for CLOB authentication                    |
| `api_secret`         | `POLY_API_SECRET`       | `str`       | `""`                                                       | API secret for HMAC signing                        |
| `api_passphrase`     | `POLY_API_PASSPHRASE`   | `str`       | `""`                                                       | Passphrase for CLOB authentication                 |
| `chain_id`           | `POLY_CHAIN_ID`         | `int`       | `137`                                                      | Polygon mainnet chain ID                           |
| `private_key`        | `POLY_PRIVATE_KEY`      | `str`       | `""`                                                       | Ethereum private key for order signing             |
| `btc_condition_ids`  | `POLY_BTC_CONDITION_IDS`| `list[str]` | `[]`                                                       | Static condition IDs (comma-separated, optional)   |

**`btc_condition_ids`** is parsed from a comma-separated string: `"id1,id2,id3"` becomes `["id1", "id2", "id3"]`. Empty strings are filtered out.

---

## DiscoveryConfig

Controls automatic market discovery via the Gamma API. Uses **predictive scheduling** — polls aggressively around expected market creation boundaries and slowly in between.

| Field                       | Env Variable              | Type        | Default   | Description                                               |
|-----------------------------|---------------------------|-------------|-----------|-----------------------------------------------------------|
| `enabled`                   | `DISCOVERY_ENABLED`       | `bool`      | `true`    | Enable/disable auto-discovery                             |
| `assets`                    | `DISCOVERY_ASSETS`        | `list[str]` | `["BTC"]` | Assets to discover (comma-separated)                      |
| `timeframes`                | `DISCOVERY_TIMEFRAMES`    | `list[str]` | `["5m", "15m"]` | Timeframes to track (comma-separated: 5m,15m,1h,4h)|
| `interval_s`                | `DISCOVERY_INTERVAL_S`    | `float`     | `15`      | Background poll interval between burst windows (seconds)  |
| `burst_poll_interval`       | `DISCOVERY_BURST_INTERVAL_S`| `float`   | `2.0`     | Fast poll interval during burst window (seconds)          |
| `burst_window`              | `DISCOVERY_BURST_WINDOW_S`| `float`     | `15`      | Duration of burst polling around each boundary (seconds)  |
| `lead_time`                 | `DISCOVERY_LEAD_TIME_S`   | `float`     | `5`       | Start burst this many seconds BEFORE the boundary         |
| `min_seconds_to_resolution` | `DISCOVERY_MIN_SECONDS`   | `int`       | `60`      | Min seconds remaining to consider market tradeable        |

**`enabled`** is parsed as truthy: `"true"`, `"1"`, `"yes"` (case-insensitive) all evaluate to `True`.

**`assets`** and **`timeframes`** are parsed from comma-separated strings.

### Predictive Scheduling

Instead of blind polling every N seconds, the discovery loop:
1. Computes the next 5m/15m boundary time (e.g., next :00, :05, :10, :15, :30, :45).
2. Sleeps until `lead_time` seconds before that boundary.
3. Enters **burst mode**: polls every `burst_poll_interval` seconds for `burst_window` seconds.
4. Falls back to slower `interval_s` polling between bursts as a safety net.

This means new markets are typically discovered within **2–5 seconds** of creation, vs 15–20s with blind polling.

---

## PredictionSourcesConfig

Controls external price feed connections.

| Field                  | Env Variable            | Type    | Default                                                                      | Description                          |
|------------------------|-------------------------|---------|------------------------------------------------------------------------------|--------------------------------------|
| `binance_ws_url`       | _(hardcoded)_           | `str`   | `wss://stream.binance.com:9443/ws/btcusdt@trade`                            | Binance trade stream WebSocket URL   |
| `cryptocompare_api_key`| `CRYPTOCOMPARE_API_KEY` | `str`   | `""`                                                                         | API key for CryptoCompare            |
| `cryptocompare_url`    | _(hardcoded)_           | `str`   | `https://min-api.cryptocompare.com/data/price?fsym=BTC&tsyms=USD`           | CryptoCompare REST endpoint          |
| `coingecko_url`        | _(hardcoded)_           | `str`   | `https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd&include_24hr_change=true` | CoinGecko REST endpoint |
| `rest_poll_interval`   | `REST_POLL_INTERVAL`    | `float` | `1.0`                                                                        | Polling interval for REST feeds (s)  |

Note: CoinGecko is polled at `rest_poll_interval * 2` (doubled in `main.py` to respect its rate limits).

---

## StrategyConfig

Controls probability-based edge detection, regime classification, and signal generation.

The strategy uses a **CDF probability model** for binary contracts. Edge is computed in probability space:

```
edge = P(outcome) - market_mid_price
```

Where `P(outcome)` is estimated via the normal CDF: `P(up) = Φ(z)`, with `z = (predicted_return / (vol × √T)) × confidence`.

| Field                 | Env Variable           | Type    | Default | Description                                                                 |
|-----------------------|------------------------|---------|---------|-----------------------------------------------------------------------------|
| `min_edge_threshold`  | `MIN_EDGE_THRESHOLD`   | `float` | `0.02`  | Minimum probability-space edge to trigger a trade (2% probability advantage)|
| `max_edge_threshold`  | `MAX_EDGE_THRESHOLD`   | `float` | `0.30`  | Maximum edge — above is suspect (near-expiry can be 10–25% legitimately)    |
| `prediction_horizon_s`| `PREDICTION_HORIZON_S` | `int`   | `900`   | Prediction horizon in seconds (15 minutes)                                  |
| `ema_fast_span`       | `EMA_FAST_SPAN`        | `int`   | `12`    | Fast EMA span for regime detection                                          |
| `ema_slow_span`       | `EMA_SLOW_SPAN`        | `int`   | `26`    | Slow EMA span for regime detection                                          |
| `volatility_window`   | `VOLATILITY_WINDOW`    | `int`   | `60`    | Number of ticks for volatility calculation                                  |
| `confidence_scale`    | _(hardcoded)_          | `bool`  | `True`  | Whether to scale position by signal strength                                |

**Edge thresholds are in probability space** (not return space). A `min_edge_threshold` of `0.02` means the strategy requires at least a 2% probability advantage over the market-implied probability. The `max_edge_threshold` of `0.30` is generous because near-expiry markets can legitimately show large edges (10–25%) as probability sharpens.

---

## RiskConfig

Controls position sizing, daily limits, and cooldowns.

| Field                     | Env Variable               | Type    | Default | Description                                          |
|---------------------------|----------------------------|---------|---------|------------------------------------------------------|
| `max_position_pct`        | `MAX_POSITION_PCT`         | `float` | `0.005` | Max capital fraction per trade (0.5%)                |
| `max_daily_loss_pct`      | `MAX_DAILY_LOSS_PCT`       | `float` | `0.02`  | Daily loss limit before halt (2%)                    |
| `max_open_positions`      | `MAX_OPEN_POSITIONS`       | `int`   | `20`    | Max concurrent open positions                        |
| `max_total_exposure_pct`  | `MAX_TOTAL_EXPOSURE_PCT`   | `float` | `0.10`  | Max total exposure as fraction of capital (10%)      |
| `cooldown_after_losses`   | `COOLDOWN_AFTER_LOSSES`    | `int`   | `5`     | Consecutive losses before cooldown                   |
| `cooldown_duration_s`     | `COOLDOWN_DURATION_S`      | `float` | `30.0`  | Cooldown pause duration (seconds)                    |
| `sideways_size_multiplier`| `SIDEWAYS_SIZE_MULTIPLIER` | `float` | `0.4`   | Position size multiplier in sideways regime          |
| `trend_size_multiplier`   | `TREND_SIZE_MULTIPLIER`    | `float` | `1.0`   | Position size multiplier in trending regime          |

---

## ExecutionConfig

Controls order submission latency and retry behavior.

| Field                   | Env Variable           | Type  | Default | Description                                    |
|-------------------------|------------------------|-------|---------|------------------------------------------------|
| `max_latency_ms`        | `MAX_LATENCY_MS`       | `int` | `100`   | Max signal age in ms (stale signals rejected)  |
| `max_orders_per_second` | `MAX_ORDERS_PER_SECOND`| `int` | `50`    | Rate limit cap (token bucket)                  |
| `max_retries`           | _(hardcoded)_          | `int` | `3`     | Max retry attempts for failed REST requests    |
| `retry_backoff_base_ms` | _(hardcoded)_          | `int` | `10`    | Base backoff for exponential retry (ms)        |
| `http_pool_size`        | `HTTP_POOL_SIZE`       | `int` | `20`    | Connection pool size for REST requests         |

---

## LoggingConfig

Controls log output destinations and rotation.

| Field             | Env Variable | Type  | Default                       | Description                               |
|-------------------|-------------|-------|-------------------------------|-------------------------------------------|
| `level`           | `LOG_LEVEL` | `str` | `"INFO"`                      | Log level (DEBUG, INFO, WARNING, etc.)    |
| `log_dir`         | `LOG_DIR`   | `str` | `{project_root}/logs`         | Directory for log files                   |
| `log_file`        | _(hardcoded)_| `str`| `"bot.log"`                   | Main log file name                        |
| `trade_log_file`  | _(hardcoded)_| `str`| `"trades.jsonl"`              | Trade log file name (JSON lines)          |
| `max_bytes`       | _(hardcoded)_| `int`| `50_000_000` (50 MB)          | Max log file size before rotation         |
| `backup_count`    | _(hardcoded)_| `int`| `10`                          | Number of rotated log backups to keep     |

---

## Validation Rules

`_validate()` is called by `load_config()` and enforces:

1. **`POLY_API_KEY` is required.** Raises `ValueError` if empty.
2. **`POLY_PRIVATE_KEY` is required.** Raises `ValueError` if empty.
3. **`POLY_BTC_CONDITION_IDS` is required when discovery is disabled.** If `DISCOVERY_ENABLED=false` and `btc_condition_ids` is empty, raises `ValueError`.

In dry-run mode (`--dry-run`), `main.py` catches these validation errors and creates an unvalidated config with a warning printed to stdout.

---

## Complete Environment Variable Reference

All configurable environment variables in one table:

| Variable                    | Config Class            | Type        | Default    | Required | Description                                    |
|-----------------------------|-------------------------|-------------|------------|----------|------------------------------------------------|
| `POLY_REST_URL`             | `PolymarketConfig`      | `str`       | see above  | No       | CLOB REST base URL                             |
| `POLY_WS_URL`               | `PolymarketConfig`      | `str`       | see above  | No       | CLOB WebSocket URL                             |
| `POLY_GAMMA_URL`            | `PolymarketConfig`      | `str`       | see above  | No       | Gamma API URL                                  |
| `POLY_API_KEY`              | `PolymarketConfig`      | `str`       | `""`       | **Yes**  | API key for CLOB                               |
| `POLY_API_SECRET`           | `PolymarketConfig`      | `str`       | `""`       | **Yes**  | API secret for HMAC                            |
| `POLY_API_PASSPHRASE`       | `PolymarketConfig`      | `str`       | `""`       | **Yes**  | Passphrase for CLOB                            |
| `POLY_CHAIN_ID`             | `PolymarketConfig`      | `int`       | `137`      | No       | Polygon chain ID                               |
| `POLY_PRIVATE_KEY`          | `PolymarketConfig`      | `str`       | `""`       | **Yes**  | Private key for order signing                  |
| `POLY_BTC_CONDITION_IDS`    | `PolymarketConfig`      | `str`       | `""`       | Cond.    | Comma-separated condition IDs (if no discovery)|
| `DISCOVERY_ENABLED`         | `DiscoveryConfig`       | `bool`      | `true`     | No       | Enable auto-discovery                          |
| `DISCOVERY_ASSETS`          | `DiscoveryConfig`       | `str`       | `"BTC"`    | No       | Comma-separated asset tickers                  |
| `DISCOVERY_TIMEFRAMES`      | `DiscoveryConfig`       | `str`       | `"5m,15m"` | No       | Comma-separated timeframes                     |
| `DISCOVERY_INTERVAL_S`      | `DiscoveryConfig`       | `float`     | `15`       | No       | Background poll interval between bursts (s)    |
| `DISCOVERY_BURST_INTERVAL_S`| `DiscoveryConfig`       | `float`     | `2.0`      | No       | Fast poll interval during burst window (s)     |
| `DISCOVERY_BURST_WINDOW_S`  | `DiscoveryConfig`       | `float`     | `15`       | No       | Burst polling duration around boundary (s)     |
| `DISCOVERY_LEAD_TIME_S`     | `DiscoveryConfig`       | `float`     | `5`        | No       | Start burst N seconds before boundary          |
| `DISCOVERY_MIN_SECONDS`     | `DiscoveryConfig`       | `int`       | `60`       | No       | Min seconds to resolution                      |
| `CRYPTOCOMPARE_API_KEY`     | `PredictionSourcesConfig`| `str`      | `""`       | No       | CryptoCompare API key                          |
| `REST_POLL_INTERVAL`        | `PredictionSourcesConfig`| `float`    | `1.0`      | No       | REST feed polling interval (s)                 |
| `MIN_EDGE_THRESHOLD`        | `StrategyConfig`        | `float`     | `0.02`     | No       | Minimum probability-space edge to trigger trade|
| `MAX_EDGE_THRESHOLD`        | `StrategyConfig`        | `float`     | `0.30`     | No       | Maximum probability-space edge (stale filter)  |
| `PREDICTION_HORIZON_S`      | `StrategyConfig`        | `int`       | `900`      | No       | Prediction horizon in seconds                  |
| `EMA_FAST_SPAN`             | `StrategyConfig`        | `int`       | `12`       | No       | Fast EMA span                                  |
| `EMA_SLOW_SPAN`             | `StrategyConfig`        | `int`       | `26`       | No       | Slow EMA span                                  |
| `VOLATILITY_WINDOW`         | `StrategyConfig`        | `int`       | `60`       | No       | Volatility lookback window (ticks)             |
| `MAX_POSITION_PCT`          | `RiskConfig`            | `float`     | `0.005`    | No       | Max capital fraction per trade                 |
| `MAX_DAILY_LOSS_PCT`        | `RiskConfig`            | `float`     | `0.02`     | No       | Daily loss limit before halt                   |
| `MAX_OPEN_POSITIONS`        | `RiskConfig`            | `int`       | `20`       | No       | Max concurrent positions                       |
| `MAX_TOTAL_EXPOSURE_PCT`    | `RiskConfig`            | `float`     | `0.10`     | No       | Max total exposure fraction                    |
| `COOLDOWN_AFTER_LOSSES`     | `RiskConfig`            | `int`       | `5`        | No       | Consecutive losses before cooldown             |
| `COOLDOWN_DURATION_S`       | `RiskConfig`            | `float`     | `30.0`     | No       | Cooldown duration (seconds)                    |
| `SIDEWAYS_SIZE_MULTIPLIER`  | `RiskConfig`            | `float`     | `0.4`      | No       | Sizing multiplier for sideways regime          |
| `TREND_SIZE_MULTIPLIER`     | `RiskConfig`            | `float`     | `1.0`      | No       | Sizing multiplier for trending regime          |
| `MAX_LATENCY_MS`            | `ExecutionConfig`       | `int`       | `100`      | No       | Max signal age (ms)                            |
| `MAX_ORDERS_PER_SECOND`     | `ExecutionConfig`       | `int`       | `50`       | No       | Rate limit cap                                 |
| `HTTP_POOL_SIZE`            | `ExecutionConfig`       | `int`       | `20`       | No       | HTTP connection pool size                      |
| `LOG_LEVEL`                 | `LoggingConfig`         | `str`       | `"INFO"`   | No       | Log level                                      |
| `LOG_DIR`                   | `LoggingConfig`         | `str`       | `{project}/logs` | No  | Directory for log files (enables per-instance isolation) |
