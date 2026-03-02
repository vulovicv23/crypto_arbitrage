# Architecture

## System Overview

The bot is an async Python 3.13 application built on `asyncio` with `aiohttp` for all I/O. Components communicate through bounded `asyncio.Queue` objects, forming a unidirectional data pipeline.

## Component Diagram

```
┌──────────────────────────────────────────────────────────────────┐
│                         Bot (main.py)                            │
│                                                                   │
│  ┌──────────────────┐                                            │
│  │ MarketDiscovery   │ ← Gamma API (every 20s)                   │
│  │ market_discovery  │──── discovers 5m/15m token_ids             │
│  └──────────────────┘──── notifies strategy + subscribes WS      │
│                                                                   │
│  ┌─────────────┐   price_queue (5000)                            │
│  │ BinanceWS   │──────┐                                          │
│  │ CryptoComp  │──────┼──┬── PredictionAggregator ───┐           │
│  │ CoinGecko   │──────┘  │   prediction_sources.py   │           │
│  └─────────────┘         │                            │           │
│                          │  [ML enabled: splitter]    │           │
│                          └── MLPredictor ─────────────┤           │
│                              src/ml/predictor.py      │           │
│                             prediction_queue (500)    │           │
│  ┌───────────────┐                                    v           │
│  │ Polymarket WS │──book──> StrategyEngine ──> signal_queue (200)│
│  │ polymarket_    │          strategy.py                          │
│  │ client.py      │                │                              │
│  └───────────────┘                v                              │
│                           RiskManager ──> approved?               │
│                           risk_manager.py                         │
│                                  │                                │
│                                  v                                │
│                           OrderManager ──> Polymarket CLOB REST   │
│                           order_manager.py                        │
│                                                                   │
│  ┌──────────────────────────────────────────────────────────┐    │
│  │ Health Monitor (30s interval) — logs stats + queue depths │    │
│  └──────────────────────────────────────────────────────────┘    │
└──────────────────────────────────────────────────────────────────┘
```

## Data Flow

### 1. Market Discovery (`src/market_discovery.py`)

Every 20 seconds, `MarketDiscovery` queries the Gamma API:

```
GET https://gamma-api.polymarket.com/markets
  ?active=true
  &closed=false
  &end_date_min={now}
  &end_date_max={now+1day}
  &order=volume24hr
  &ascending=false
  &limit=500
```

Filters applied:
- Question contains "up or down" (case-insensitive)
- Slug matches `-updown-5m-` or `-updown-15m-` (regex)
- Asset from slug is in configured assets (default: BTC)
- Market has >= 60 seconds until resolution

When new markets are found:
1. Strategy engine receives `MarketContext` objects (YES/NO token mapping + expiry + timeframe) via `set_market_contexts()`
2. Initial order book snapshots are fetched via REST
3. New WebSocket subscriptions are created for real-time book updates

### 2. Price Sources (`src/prediction_sources.py`)

Three independent async tasks produce `PriceTick` objects:

| Source | Type | Latency | Interval |
|--------|------|---------|----------|
| Binance | WebSocket stream | ~50ms | Real-time per trade |
| CryptoCompare | REST polling | ~500ms | 1.0s |
| CoinGecko | REST polling | ~1000ms | 2.0s |

All emit into `price_queue` (capacity: 5000). Back-pressure: drop oldest on overflow.

### 3. Prediction Aggregator (`src/prediction_sources.py`)

Runs two concurrent loops:
- **Ingest loop**: Consumes `PriceTick` objects, maintains per-source rolling windows (300 ticks)
- **Emit loop**: Every 0.25s, generates a `Prediction` using:
  1. Per-source linear regression on recent price history
  2. Extrapolation to the prediction horizon (default: 900s)
  3. Confidence = R-squared of the regression fit
  4. Cross-source blending: confidence-weighted average
  5. Agreement factor: penalizes divergence between sources

### 3b. ML Predictor (`src/ml/predictor.py`) — Optional

When `ML_ENABLED=true`, the bot adds a parallel prediction path:

1. A **price splitter** task fans `PriceTick` objects from `price_queue` to both:
   - `aggregator_queue` → PredictionAggregator (existing path)
   - `ml_price_queue` → MLPredictor (new path)

2. **MLPredictor** runs two concurrent loops:
   - **Ingest loop**: Consumes `PriceTick` objects, feeds `FeatureEngine` which aggregates ticks into 1-second OHLCV bars (matching Binance 1s kline training data)
   - **Predict loop**: Every 0.25s, computes 58 features from the bar buffer, runs LightGBM inference (~<1ms), emits `Prediction` objects

3. **Feature catalogue** (58 frozen features):
   - Returns (6), volatility (4), momentum (3), acceleration (2), volume (4)
   - VWAP deviation (2), Bollinger z-score (2), EMA/MACD (2), range/intensity (2)
   - Multi-timeframe 1m/5m bars (10), v2 features (8), candlestick microstructure (6)
   - Orderbook pass-through (5), time features (2)

4. **Warmup**: Requires 3661 1-second bars (~61 minutes) before first prediction. The `FeatureEngine` returns `None` until sufficient data is accumulated.

5. **Confidence**: `|p_up - 0.5| × 2.0`, filtered by `ML_MIN_CONFIDENCE` threshold.

When ML is disabled (default), the price splitter is not created and the pipeline works as before.

### 4. Strategy Engine (`src/strategy.py`)

Consumes `Prediction` objects and evaluates against all monitored order books using a **CDF probability model** for binary contracts.

**Core model:** Polymarket BTC Up/Down markets are binary bets — YES pays $1 if BTC goes up, NO pays $1 if down. The token mid-price IS the market-implied probability. The strategy computes an independent P(up) estimate and trades the discrepancy:

```
P(up) = Φ(z)   where z = (predicted_return / (vol × √T)) × confidence
edge  = P(outcome) - mid_price
```

Where:
- `Φ` = standard normal CDF (via `math.erf`, no scipy dependency)
- `vol` = standard deviation of BTC tick-to-tick returns
- `T` = seconds remaining until market expiry
- `confidence` = prediction model confidence (dampens z-score toward 0 → P toward 0.5)

**Signal generation (BUY-only):**
- For each tracked order book, look up `MarketContext` to determine if the token is YES or NO
- Compute `P(up)` once per prediction + market (cached per `condition_id`)
- YES token: `edge = P(up) - mid_price`; NO token: `edge = (1 - P(up)) - mid_price`
- Only emit **BUY** signals with positive edge (buy the underpriced outcome)
- Skip fully-priced-in books (mid < 0.01 or > 0.99), near-expiry (< 5s remaining)
- Edge must satisfy: `min_edge_threshold (0.02) ≤ edge ≤ max_edge_threshold (0.30)`

**Strength classification** (threshold-relative, auto-calibrates):
- WEAK: 1.0–1.5× threshold (0.02–0.03)
- MODERATE: 1.5–2.5× threshold (0.03–0.05)
- STRONG: > 2.5× threshold (0.05–0.30)

**Market context:** `MarketContext` objects (built from `DiscoveredMarket` in `main.py`) provide YES/NO token mapping and expiry time. The strategy receives these via `set_market_contexts()` when markets are discovered.

**Regime detection** uses dual-EMA crossover on contract mid-prices:
- Fast EMA (span=12) vs Slow EMA (span=26)
- If fast > slow by > 0.05%: TRENDING_UP
- If fast < slow by > 0.05%: TRENDING_DOWN
- If volatility < 0.1%: SIDEWAYS (regardless of EMA)

### 5. Risk Manager (`src/risk_manager.py`)

Gates every signal through sequential checks:

1. **Halted?** — Trading stopped for the day
2. **Cooldown?** — After 5 consecutive losses, 30s pause
3. **Daily loss limit** — 2% of capital → halt
4. **Max open positions** — 20 concurrent
5. **Total exposure** — 10% of capital
6. **Latency budget** — Signal older than 100ms is stale
7. **Position sizing**:
   ```
   size = capital × max_position_pct × regime_mult × strength_mult × confidence
   ```

### 6. Order Manager (`src/order_manager.py`)

Approved signals become orders:

1. **Build order**: Set price at best ask (BUY) or best bid (SELL)
2. **Submit**: POST to Polymarket CLOB API
3. **Track**: Poll for fill status with exponential backoff (0.5s → 5s, 30s timeout)
4. **Record**: On fill, register position with risk manager, write JSONL trade log (includes probability model analytics: `p_up`, `outcome`, `seconds_to_expiry`, `btc_volatility`)
5. **Cancel**: On timeout, cancel the stale order

## Queue Sizes and Back-Pressure

| Queue | Capacity | Overflow Policy |
|-------|----------|-----------------|
| `price_queue` | 5000 | Drop oldest tick |
| `prediction_queue` | 500 | Drop oldest prediction |
| `signal_queue` | 200 | Drop oldest signal |
| `aggregator_queue` | 5000 | Drop oldest tick (ML mode only) |
| `ml_price_queue` | 5000 | Drop oldest tick (ML mode only) |

## Async Task Summary

| Task | Source | Description |
|------|--------|-------------|
| `source-binance` | `BinanceSource.start()` | Binance WS trade stream |
| `source-cryptocompare` | `CryptoCompareSource.start()` | REST polling |
| `source-coingecko` | `CoinGeckoSource.start()` | REST polling |
| `aggregator` | `PredictionAggregator.start()` | Ingest + emit loops |
| `strategy` | `StrategyEngine.run()` | Consume predictions, emit signals |
| `order-manager` | `OrderManager.run()` | Consume signals, manage orders |
| `health-monitor` | `Bot._health_monitor()` | Stats dump every 30s |
| `market-discovery` | `MarketDiscovery.start()` | Discover new markets every 20s |
| `poly-ws-*` | `PolymarketClient.subscribe_books()` | WS book subscriptions (dynamic) |
| `price-splitter` | `Bot._price_splitter()` | Fan ticks to aggregator + ML (ML mode) |
| `ml-predictor` | `MLPredictor.start()` | Ingest + predict loops (ML mode) |

## Shutdown Sequence

1. Signal handler (`SIGINT`/`SIGTERM`) sets `shutdown_event`
2. `Bot.stop()` called:
   - Stop all price sources
   - Stop market discovery
   - Cancel all pending orders on exchange (live mode only)
   - Cancel all async tasks
   - Close Polymarket REST+WS sessions
3. Event loop drains remaining tasks and closes

## File Dependencies

```
config.py          ← .env
main.py            ← config, all src/* modules, src/ml/predictor
src/models.py      ← (standalone, no internal deps)
src/market_discovery.py  ← aiohttp (Gamma API)
src/prediction_sources.py ← models, aiohttp, numpy
src/strategy.py    ← config, models, numpy, math (CDF)
src/risk_manager.py ← config, models
src/order_manager.py ← config, models, polymarket_client, risk_manager
src/polymarket_client.py ← config, models, aiohttp, orjson
src/synthetic_books.py ← config, models
src/ws_client.py   ← aiohttp, orjson
src/ws_pool.py     ← ws_client
src/ml/features.py ← numpy (standalone feature engine)
src/ml/predictor.py ← config, models, ml/features, joblib, lightgbm
src/logger_setup.py ← config
```
