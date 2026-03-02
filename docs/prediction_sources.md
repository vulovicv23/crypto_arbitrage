# Prediction Sources

Source: `src/prediction_sources.py`

Price feed integrations that produce `PriceTick` objects feeding the strategy. Each source runs as an independent async task and pushes into a shared `asyncio.Queue`.

## Architecture

```
┌──────────────┐
│ BinanceWS    │──┐
├──────────────┤  │
│ CryptoCompRE │──┤──>  price_queue (asyncio.Queue[PriceTick])
├──────────────┤  │
│ CoinGeckoRE  │──┘
└──────────────┘

PredictionAggregator consumes ticks → emits Prediction objects
```

## Base Class: PriceSource (ABC)

All sources extend `PriceSource`. Provides:
- `start()` — Launch the source with automatic retry on crash (exponential backoff: 1s → 2s → 4s → ... → 60s max)
- `stop()` — Set `_running = False`
- `_emit(tick)` — Push to queue with back-pressure (drops oldest on full)
- `_run()` — Abstract method implemented by each source

### Crash Recovery

If `_run()` raises a non-`CancelledError` exception, the source logs the error and restarts automatically after an exponential backoff delay. This prevents permanent loss of a price feed due to transient errors (network timeouts, API blips, etc.). Clean cancellation (`CancelledError`) exits immediately without retry.

## Sources

### BinanceSource (WebSocket)

Lowest-latency source. Streams individual BTC/USDT trades from Binance.

| Parameter | Default |
|-----------|---------|
| WebSocket URL | `wss://stream.binance.com:9443/ws/btcusdt@trade` |
| Heartbeat | 20s |
| Receive timeout | 30s |
| Reconnect delay | 1s |

Parses Binance trade messages: `price = data["p"]`, `volume = data["q"]`, `timestamp = data["T"] * 1_000_000` (ms to ns).

### CryptoCompareSource (REST Polling)

Secondary price feed. Polls CryptoCompare REST API.

| Parameter | Source | Default |
|-----------|--------|---------|
| API URL | hardcoded | `https://min-api.cryptocompare.com/data/price?fsym=BTC&tsyms=USD` |
| API key | `CRYPTOCOMPARE_API_KEY` | (optional) |
| Poll interval | `REST_POLL_INTERVAL` | 1.0s |
| Request timeout | hardcoded | 5s |

### CoinGeckoSource (REST Polling)

Free-tier price feed. Polls CoinGecko API.

| Parameter | Source | Default |
|-----------|--------|---------|
| API URL | hardcoded | CoinGecko simple price endpoint |
| Poll interval | `REST_POLL_INTERVAL * 2` | 2.0s |
| Request timeout | hardcoded | 5s |

## PredictionAggregator

Consumes `PriceTick` objects from multiple sources, maintains rolling windows, and produces `Prediction` objects.

### Constructor

```python
PredictionAggregator(
    price_queue,           # Input: PriceTick queue
    prediction_queue,      # Output: Prediction queue
    horizon_s=900,         # Prediction horizon (15 min)
    window_size=300,       # Rolling window size per source
    emit_interval=0.25,    # Prediction emit rate (4/sec)
)
```

### Two Concurrent Loops

1. **Ingest loop** — Reads ticks from `price_queue`, appends `(timestamp_ns, price)` to per-source rolling windows
2. **Emit loop** — Every `emit_interval` seconds, generates a blended prediction

### Prediction Model

1. **Per-source extrapolation**: Linear regression on recent ticks, extrapolated to the prediction horizon. Confidence = R-squared of the fit.
2. **Confidence-weighted blend**: `blended_price = sum(price * confidence) / sum(confidence)` across sources.
3. **Cross-source agreement**: If 2+ sources available, compute coefficient of variation. High divergence penalizes confidence.
4. **Price priority**: Current reference price prefers Binance > CryptoCompare > CoinGecko.

### Requirements

- Minimum 10 ticks per source before extrapolation
- Minimum 1 second of data span for valid regression
- At least one source producing predictions
