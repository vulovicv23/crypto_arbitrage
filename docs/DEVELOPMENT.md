# Development Guide

> **Entry point:** `main.py`
> **Config:** `config.py`
> **Source modules:** `src/`

This guide covers how to set up, run, extend, and debug the Polymarket BTC Latency-Arbitrage Bot.

---

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [Setup](#setup)
3. [Running the Bot](#running-the-bot)
4. [Project Structure](#project-structure)
5. [How to Add a New Price Source](#how-to-add-a-new-price-source)
6. [How to Add a New Asset](#how-to-add-a-new-asset)
7. [How to Adjust Timeframes](#how-to-adjust-timeframes)
8. [How to Change Risk Parameters](#how-to-change-risk-parameters)
9. [Logging](#logging)
10. [Parallel Test Matrix](#parallel-test-matrix)
11. [Debugging Tips](#debugging-tips)

---

## Prerequisites

- **Python 3.13+** (uses `type[X] | None` syntax, `slots=True` dataclasses)
- **Polymarket account** with:
  - API key, secret, and passphrase from the CLOB developer dashboard
  - Ethereum private key funded with USDC on Polygon (chain ID 137)
- **Optional:** CryptoCompare API key (for higher rate limits on the secondary price feed)

### Python Dependencies

The project uses the following key packages:

| Package       | Purpose                              |
|---------------|--------------------------------------|
| `aiohttp`     | Async HTTP client and WebSocket      |
| `orjson`      | Fast JSON serialization              |
| `numpy`       | Linear regression, statistics        |
| `python-dotenv`| Load `.env` file                    |
| `lightgbm`    | ML model training and inference      |
| `scikit-learn` | Calibration, metrics                |
| `asyncpg`     | PostgreSQL async client              |
| `joblib`      | Model serialization                  |
| `optuna`      | Hyperparameter optimization          |

---

## Setup

### 1. Clone the repository

```bash
git clone <repo-url>
cd crypto_arbitrage
```

### 2. Create a virtual environment

```bash
python3.13 -m venv .venv
source .venv/bin/activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Create the `.env` file

```bash
cp .env.example .env
# Edit .env with your credentials
```

Required variables (see `docs/CONFIGURATION.md` for all options):

```env
POLY_API_KEY=your-api-key
POLY_API_SECRET=your-api-secret
POLY_API_PASSPHRASE=your-passphrase
POLY_PRIVATE_KEY=0xYourEthereumPrivateKey

# Optional
CRYPTOCOMPARE_API_KEY=your-cc-key
DISCOVERY_ENABLED=true
DISCOVERY_TIMEFRAMES=5m,15m
LOG_LEVEL=INFO
```

---

## Running the Bot

### Dry-Run Mode (no real orders)

```bash
python main.py --dry-run
```

In dry-run mode:
- Config validation errors for missing API keys are skipped with a warning.
- The `DryRunOrderManager` is used instead of `OrderManager`.
- Orders are logged but never submitted to Polymarket.
- Fills are simulated immediately at the order price.
- All other components (discovery, prediction, strategy, risk) run normally.

### Live Mode

```bash
python main.py
```

### With Custom Capital

```bash
python main.py --capital 5000           # $5,000 starting capital
python main.py --dry-run --capital 50000 # dry-run with $50k
```

Default starting capital: `$10,000`.

### Command-Line Arguments

| Argument      | Type    | Default   | Description                           |
|---------------|---------|-----------|---------------------------------------|
| `--dry-run`   | flag    | `False`   | Log trades without placing orders     |
| `--capital`   | float   | `10000.0` | Starting capital in USDC              |
| `--duration`  | int     | `0`       | Run for N seconds then shut down (0 = forever) |

### Shutdown

The bot handles `SIGINT` (Ctrl+C) and `SIGTERM` gracefully:

1. Cancels all pending orders on Polymarket.
2. Stops all price sources.
3. Stops market discovery.
4. Cancels all async tasks.
5. Closes WebSocket and HTTP connections.

---

## Project Structure

```
crypto_arbitrage/
+-- main.py                     # Entry point, Bot orchestrator, DryRunOrderManager
+-- config.py                   # All configuration (frozen dataclasses, .env loading)
+-- .env                        # Secrets and tunable parameters (not committed)
+-- requirements.txt            # Python dependencies
+-- docker-compose.yml          # PostgreSQL for ML data storage
+-- schema.sql                  # Database schema (klines, labels, model runs)
+-- src/
|   +-- models.py               # Core domain models (PriceTick, Prediction, Signal, Order, etc.)
|   +-- market_discovery.py     # Gamma API market discovery (DiscoveredMarket, MarketDiscovery)
|   +-- prediction_sources.py   # Price feeds (Binance, CryptoCompare, CoinGecko, Aggregator)
|   +-- strategy.py             # Strategy engine (CDF probability model, edge computation, regime detection)
|   +-- risk_manager.py         # Risk management (position sizing, daily limits, cooldowns)
|   +-- order_manager.py        # Order lifecycle (build, submit, track, close, log)
|   +-- polymarket_client.py    # Polymarket CLOB client (REST + WebSocket + auth)
|   +-- synthetic_books.py      # Synthetic order book generation (paper mode)
|   +-- ws_client.py            # WebSocket client with auto-reconnect
|   +-- ws_pool.py              # WebSocket connection pool (500 tokens/conn)
|   +-- logger_setup.py         # Logging configuration (console + JSON file + rotation)
|   +-- ml/                     # Machine learning prediction module
|       +-- features.py         # Feature engineering (58 features, batch + streaming)
|       +-- predictor.py        # LightGBM inference wrapper (async)
+-- tools/
|   +-- test_matrix.py          # Parallel bot testing framework
|   +-- liquidity_scanner.py    # Market liquidity discovery
|   +-- collect_data.py         # Binance historical kline downloader
|   +-- train_model.py          # LightGBM training with walk-forward CV
|   +-- backtest.py             # Historical backtesting framework
+-- models/                     # Trained ML artifacts (gitignored)
+-- logs/                       # Log output directory (auto-created)
|   +-- bot.log                 # Main log file (JSON lines, rotated at 50MB)
|   +-- trades.jsonl            # Trade log (one JSON object per trade)
+-- docs/                       # Documentation
```

### Data Pipeline

```
MarketDiscovery ──> token_ids ──> Bot._on_markets_changed()
                                       |
BinanceWS ──┐                          |
CryptoComp ──> price_queue ──> Aggregator ──> prediction_queue
CoinGecko ──┘                                       |
                                                     v
Polymarket WS ──> book updates ──> StrategyEngine._evaluate()
                                          |
                                     signal_queue
                                          |
                                          v
                                    RiskManager.check_signal()
                                          |
                                          v
                                    OrderManager._submit_order()
                                          |
                                          v
                                    Polymarket CLOB REST
```

---

## How to Add a New Price Source

### Step 1: Create the source class

In `src/prediction_sources.py`, create a new class that extends `PriceSource`:

```python
class MyNewSource(PriceSource):
    """Polls MyExchange API for BTC/USD price."""

    def __init__(
        self,
        queue: asyncio.Queue[PriceTick],
        api_url: str = "https://api.myexchange.com/v1/ticker/btcusd",
        poll_interval: float = 1.0,
    ):
        super().__init__("myexchange", queue)  # unique source name
        self._url = api_url
        self._poll_interval = poll_interval

    async def _run(self) -> None:
        async with aiohttp.ClientSession() as session:
            while self._running:
                try:
                    async with session.get(
                        self._url, timeout=aiohttp.ClientTimeout(total=5)
                    ) as resp:
                        data = await resp.json()
                        price = float(data["last_price"])
                        if price > 0:
                            await self._emit(
                                PriceTick(source="myexchange", price=price)
                            )
                except Exception as exc:
                    logger.warning("MyExchange error: %s", exc)
                await asyncio.sleep(self._poll_interval)
```

### Step 2: Register the source in `main.py`

In `Bot.start()`, add the new source to `self._sources`:

```python
self._sources = [
    BinanceSource(...),
    CryptoCompareSource(...),
    CoinGeckoSource(...),
    MyNewSource(self._price_queue),  # add here
]
```

### Step 3: (Optional) Add configuration

If the source needs config, add fields to `PredictionSourcesConfig` in `config.py`:

```python
myexchange_url: str = os.getenv("MYEXCHANGE_URL", "https://api.myexchange.com/v1/ticker/btcusd")
```

### Step 4: Update current price priority

In `PredictionAggregator._best_current_price()`, add the new source to the priority list if it should be preferred:

```python
for source in ("binance", "myexchange", "cryptocompare", "coingecko"):
    if source in self._latest:
        return self._latest[source]
```

---

## How to Add a New Asset

To add support for a new asset (e.g., ETH):

### Step 1: Update discovery configuration

In `.env`:

```env
DISCOVERY_ASSETS=BTC,ETH
```

### Step 2: Add price feed sources for the new asset

The current Binance source tracks `btcusdt@trade`. To add ETH, create a parallel source:

```python
# In main.py Bot.start():
self._sources.append(
    BinanceSource(
        self._price_queue,
        ws_url="wss://stream.binance.com:9443/ws/ethusdt@trade",
    )
)
```

You would need to modify `BinanceSource` (or create a variant) to tag ticks with the asset name, and modify the `PredictionAggregator` to maintain separate price windows per asset.

### Step 3: Update market discovery filters

The discovery module already supports multi-asset discovery. It extracts the asset from the slug pattern `{asset}-updown-{timeframe}-{timestamp}`. Setting `DISCOVERY_ASSETS=BTC,ETH` will match slugs like `eth-updown-5m-1771942800`.

### Step 4: Update the strategy

The current strategy treats all book updates uniformly. For multi-asset support, you would need to:

1. Maintain separate price histories per asset.
2. Compute separate regime classifications per asset.
3. Match predictions to the correct asset's books.

---

## How to Adjust Timeframes

### Include longer timeframes

In `.env`:

```env
DISCOVERY_TIMEFRAMES=5m,15m,1h,4h
```

Supported timeframe values: `5m`, `15m`, `1h`, `4h`.

### Timeframe-specific sizing

The `Timeframe` enum in `src/market_discovery.py` has built-in sizing multipliers:

| Timeframe | `sizing_multiplier` | `resolution_seconds` |
|-----------|--------------------:|---------------------:|
| 5m        | 0.50                | 300                  |
| 15m       | 1.00                | 900                  |
| 1h        | 1.25                | 3600                 |
| 4h        | 1.50                | 14400                |

These multipliers are available on the `DiscoveredMarket.timeframe` but are **not currently used** by the risk manager. To use them:

```python
# In RiskManager._compute_size(), access the market's timeframe:
# (requires passing timeframe info through the Signal)
size *= timeframe.sizing_multiplier
```

### Adjust prediction horizon

The prediction horizon should match your primary timeframe. For 1h markets:

```env
PREDICTION_HORIZON_S=3600
```

---

## How to Change Risk Parameters

All risk parameters are configurable via environment variables. Common adjustments:

### More aggressive sizing

```env
MAX_POSITION_PCT=0.01         # 1% per trade (was 0.5%)
MAX_TOTAL_EXPOSURE_PCT=0.20   # 20% total exposure (was 10%)
MAX_OPEN_POSITIONS=50         # more concurrent positions
```

### More conservative

```env
MAX_POSITION_PCT=0.002        # 0.2% per trade
MAX_DAILY_LOSS_PCT=0.01       # 1% daily loss limit (was 2%)
SIDEWAYS_SIZE_MULTIPLIER=0.2  # smaller in sideways (was 0.4)
```

### Tighter latency

```env
MAX_LATENCY_MS=50             # reject signals older than 50ms (was 100ms)
```

### Longer cooldown

```env
COOLDOWN_AFTER_LOSSES=3       # cooldown after 3 losses (was 5)
COOLDOWN_DURATION_S=60        # 60 second pause (was 30)
```

---

## Logging

### Console Output

Colored, human-readable format:

```
14:30:15 INFO     [src.strategy] SIGNAL: BUY YES 0xabc123def4 edge=0.1200 p_up=0.6700 mid=0.5500 ttl=180s regime=TRENDING_UP strength=STRONG
14:30:15 INFO     [src.order_manager] ORDER SUBMITTED: BUY 0xabc123def4 price=0.5600 size=25.00 latency=12.3ms edge=0.1200
```

Signal log fields include `p_up` (probability estimate), `mid` (market-implied probability), `ttl` (seconds to expiry), and `outcome` (YES/NO).

Color scheme:
- DEBUG: grey
- INFO: cyan
- WARNING: yellow
- ERROR: red
- CRITICAL: red background

### File Logs (`logs/bot.log`)

JSON-lines format (machine-parseable), with rotating file handler:

```json
{"ts": 1709123456.789, "level": "INFO", "logger": "src.strategy", "msg": "SIGNAL: BUY YES 0xabc123def4 edge=0.1200 p_up=0.67..."}
```

- Max size: 50 MB per file
- Backup count: 10 files (total ~500 MB max)

### Trade Log (`logs/trades.jsonl`)

One JSON object per filled trade:

```json
{
  "ts": 1709123456.789,
  "order_id": "uuid-here",
  "exchange_id": "poly-order-id",
  "condition_id": "0xcondition...",
  "token_id": "0xtoken...",
  "side": "BUY",
  "price": 0.56,
  "size": 25.0,
  "fill_price": 0.56,
  "fill_size": 25.0,
  "latency_ms": 12.3,
  "edge": 0.12,
  "strength": "STRONG",
  "regime": "TRENDING_UP",
  "pred_return": 0.001,
  "pred_confidence": 0.85,
  "p_up": 0.67,
  "outcome": "YES",
  "seconds_to_expiry": 180.0,
  "btc_volatility": 0.00015
}
```

**Probability model analytics** (added fields):
- `p_up`: CDF probability estimate that BTC goes up (`0.0–1.0`)
- `outcome`: Which outcome this token represents (`"YES"` or `"NO"`)
- `seconds_to_expiry`: Time remaining until market resolution
- `btc_volatility`: Standard deviation of BTC tick-to-tick returns used in the CDF model

### Third-Party Logger Quieting

The following loggers are set to WARNING to reduce noise:

- `aiohttp`
- `asyncio`
- `websockets`

### Log Level Control

Set via environment variable:

```env
LOG_LEVEL=DEBUG    # most verbose
LOG_LEVEL=INFO     # default -- signals, orders, health
LOG_LEVEL=WARNING  # only warnings and errors
```

### Timed Runs

Use `--duration` for automated or time-bounded testing:

```bash
python main.py --dry-run --duration 300           # run 5 minutes then exit
python main.py --dry-run --capital 50000 --duration 3600  # 1 hour test with $50k
```

The bot shuts down gracefully when the duration expires — cancelling pending orders, closing WebSocket connections, and flushing trade logs. `SIGINT`/`SIGTERM` still works during timed runs (immediately triggers shutdown).

---

## Parallel Test Matrix

The test matrix tool (`tools/test_matrix.py`) runs multiple bot instances simultaneously with different configurations, then compares results to find optimal settings.

### Quick Start

```bash
# List all available profiles
python tools/test_matrix.py --list-profiles

# Run all 7 built-in profiles for 1 hour
python tools/test_matrix.py --duration 3600

# Quick 5-minute comparison of two profiles
python tools/test_matrix.py --profiles moderate,aggressive --duration 300

# Custom capital and parallel limit
python tools/test_matrix.py --duration 3600 --capital 50000 --max-parallel 3
```

### Built-in Profiles

| Profile | MIN_EDGE | POS_PCT | Description |
|---------|----------|---------|-------------|
| `conservative` | 0.03 | 0.003 | Tight stops, small positions |
| `moderate` | 0.02 | 0.005 | Baseline (matches defaults) |
| `aggressive` | 0.01 | 0.01 | Low bar, large positions |
| `near_expiry` | 0.005 | 0.008 | Targets near-expiry edges |
| `edge_sweep_low` | 0.005 | — | Very permissive threshold |
| `edge_sweep_mid` | 0.015 | — | Moderate selectivity |
| `edge_sweep_high` | 0.04 | — | Highly selective |

### Custom Profiles

Create a JSON file with custom parameter sets:

```json
{
  "my_strategy": {
    "description": "Optimized for sideways markets",
    "env_overrides": {
      "MIN_EDGE_THRESHOLD": "0.025",
      "SIDEWAYS_SIZE_MULTIPLIER": "0.8"
    }
  }
}
```

```bash
python tools/test_matrix.py --custom-profiles my_profiles.json --profiles my_strategy --duration 600
```

### CLI Options

| Argument | Type | Default | Description |
|----------|------|---------|-------------|
| `--profiles` | str | `all` | Comma-separated profile names, or `all` |
| `-d`, `--duration` | int | `3600` | Duration per bot in seconds |
| `--capital` | float | `10000` | Starting capital per instance (USDC) |
| `--max-parallel` | int | `0` | Max concurrent processes (0 = all at once) |
| `--output-dir` | str | `matrix_runs/` | Base directory for run artifacts |
| `--custom-profiles` | str | — | Path to custom profiles JSON file |
| `--run-id` | str | timestamp | Custom run identifier |
| `--list-profiles` | flag | — | List all profiles and exit |

### Output Structure

Each run creates a timestamped directory:

```
matrix_runs/
└── 20260227_143000/
    ├── run_meta.json          # Run config for reproducibility
    ├── conservative/
    │   ├── bot.log            # Bot log output
    │   ├── trades.jsonl       # Trade records
    │   ├── stdout.log         # Process stdout
    │   └── stderr.log         # Process stderr
    ├── moderate/
    │   └── ...
    └── report/
        ├── summary.json       # Structured results
        └── summary.md         # Human-readable report
```

### Metrics and Scoring

The report ranks profiles by a composite score (0–1) based on:

| Metric | Weight | Notes |
|--------|--------|-------|
| Sharpe ratio | 0.25 | Risk-adjusted return |
| Total expected PnL | 0.25 | `sum(edge × fill_size)` |
| Profit factor | 0.15 | `sum(gains) / abs(sum(losses))` |
| Max drawdown (inverted) | 0.15 | Lower drawdown is better |
| Trades per hour | 0.10 | Activity level |
| Average edge | 0.10 | Signal quality |

In dry-run mode, "expected PnL" uses `edge × fill_size` as a proxy since actual market outcomes aren't observed. This is valid for relative comparison since all profiles see the same market conditions simultaneously.

---

## ML Tools

### Database Setup

The ML pipeline requires PostgreSQL for storing historical klines and training artifacts.

```bash
# Start PostgreSQL (port 6501)
docker compose up -d

# Verify it's running
docker compose ps
```

PostgreSQL runs on `localhost:6501` with credentials `postgres:postgres` and database `crypto_arbitrage`. The `schema.sql` file auto-runs on first start, creating tables: `btc_klines`, `ml_labels`, `ml_model_runs`.

### Data Collection

Download historical 1-second BTCUSDT klines from Binance:

```bash
# Download 6 months of data
python tools/collect_data.py --start 2025-09-01 --end 2026-02-28

# Resume from last stored row
python tools/collect_data.py --resume --end 2026-02-28
```

Data is stored in the `btc_klines` table (~31.5M rows/year). The collector uses 10 parallel workers with rate limiting.

### Model Training

Train a LightGBM classifier with walk-forward cross-validation:

```bash
# Basic training (5m horizon)
python tools/train_model.py --horizon 300 --output models/btc_5m_v2.pkl

# With Optuna HPO
python tools/train_model.py --horizon 300 --optuna --optuna-trials 50

# Custom date range and dead zone
python tools/train_model.py --horizon 300 --dead-zone 0.001 --start 2025-09-01 --end 2026-02-28
```

Output: `.pkl` artifact containing `model`, `calibrator`, `feature_names`, `num_features`, `horizon_s`, `metrics`. Metrics include accuracy, AUC-ROC, Brier score, log loss.

### Backtesting

Replay historical klines through the full ML pipeline:

```bash
# Run backtest with ML model
python tools/backtest.py --model models/btc_5m_v2.pkl --start 2026-01-01 --end 2026-02-28

# Control group (no ML)
python tools/backtest.py --no-ml --start 2026-01-01 --end 2026-02-28

# Custom capital
python tools/backtest.py --model models/btc_5m_v2.pkl --capital 10000
```

The backtester replays klines, feeds them through the feature engine and ML inference, generates synthetic order books, runs the strategy/risk pipeline, and resolves positions at 5m boundaries. Reports total PnL, win rate, Sharpe ratio, max drawdown.

---

## Debugging Tips

### 1. Start with dry-run

Always test changes with `--dry-run` first:

```bash
python main.py --dry-run --capital 10000
```

### 2. Enable DEBUG logging

```env
LOG_LEVEL=DEBUG
```

This adds:
- Queue depth logging every 30 seconds.
- Edge computations that fall below the threshold.
- Order placement payloads.
- All REST request attempts and retries.

### 3. Monitor the health output

Every 30 seconds, the health monitor logs:

```
HEALTH | submitted=5 filled=3 rejected=1 risk_blocked=12 pending=1 pnl=0.0045 win_rate=66.7% volume=150.00 regime=TRENDING_UP markets_active=4 discovered=8 expired=4
```

Key metrics to watch:
- **risk_blocked** -- If this is very high, your edge thresholds or risk limits may be too tight.
- **rejected** -- API errors or rate limiting issues.
- **pending** -- Orders waiting for fills. If this grows, fills may be timing out.
- **win_rate** -- Below 50% consistently suggests the prediction model needs tuning.
- **pnl** -- Running daily P&L.

### 4. Check queue depths

At DEBUG level, queue depths are logged:

```
Queues | price=0 prediction=0 signal=0 subscribed_tokens=8
```

- **High price queue** -- Price sources are producing faster than the aggregator consumes. Usually fine.
- **High prediction queue** -- Strategy is not consuming predictions fast enough.
- **High signal queue** -- Order manager is not processing signals fast enough.

### 5. Analyze trade logs

```bash
# Count trades by outcome
cat logs/trades.jsonl | python -c "
import sys, json
trades = [json.loads(l) for l in sys.stdin]
yes = sum(1 for t in trades if t.get('outcome') == 'YES')
no = sum(1 for t in trades if t.get('outcome') == 'NO')
print(f'YES (up): {yes}, NO (down): {no}, Total: {len(trades)}')
"

# Average latency
cat logs/trades.jsonl | python -c "
import sys, json
trades = [json.loads(l) for l in sys.stdin]
if trades:
    avg = sum(t['latency_ms'] for t in trades) / len(trades)
    print(f'Avg latency: {avg:.1f}ms over {len(trades)} trades')
"

# Average edge and P(up)
cat logs/trades.jsonl | python -c "
import sys, json
trades = [json.loads(l) for l in sys.stdin]
if trades:
    avg_edge = sum(t['edge'] for t in trades) / len(trades)
    avg_pup = sum(t.get('p_up', 0) for t in trades) / len(trades)
    print(f'Avg edge: {avg_edge:.4f}, Avg P(up): {avg_pup:.4f}')
"
```

### 6. Common Issues

| Symptom                               | Likely Cause                                    | Fix                                           |
|---------------------------------------|-------------------------------------------------|-----------------------------------------------|
| No signals emitted                    | Edge thresholds too tight, no book data, or no `MarketContext` | Lower `MIN_EDGE_THRESHOLD` (probability space, default 0.02) or check WS/discovery |
| All signals blocked by risk           | Too many positions or daily loss hit            | Increase limits or check P&L                  |
| "Signal too stale" rejections         | Slow processing pipeline                        | Increase `MAX_LATENCY_MS` or optimize code    |
| "Binance WS error -- reconnecting"    | Network issues or Binance downtime              | Normal -- auto-reconnects in 1 second         |
| "Config validation error"             | Missing API keys in `.env`                      | Set `POLY_API_KEY`, `POLY_PRIVATE_KEY`, etc.  |
| "All retries exhausted"               | Polymarket API down or rate-limited             | Check API status, reduce `MAX_ORDERS_PER_SECOND`|
| Discovery finds 0 markets             | No active BTC Up/Down markets at this moment    | Wait for new markets to open, or expand `DISCOVERY_TIMEFRAMES` |
| DRY-RUN orders but no live orders     | Running with `--dry-run` flag                   | Remove `--dry-run` for live trading           |
| Orders submitted but never filled     | Price moved away before fill                    | Adjust pricing logic in `OrderManager._build_order()` |
