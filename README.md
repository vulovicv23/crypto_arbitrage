# Crypto Arbitrage Bot

BTC latency-arbitrage trading bot for Polymarket prediction markets.

Detects divergences between real-time BTC price feeds and Polymarket contract prices, then trades the edge within a strict latency budget (<100ms detection-to-trade).

## How It Works

1. **Price Sources** — Streams real-time BTC prices from Binance (WebSocket), CryptoCompare (REST), and CoinGecko (REST)
2. **Prediction Aggregator** — Blends multi-source prices using confidence-weighted linear regression to predict 15-minute BTC returns
3. **Strategy Engine** — Compares predictions against Polymarket order book mid-prices, detects edges, classifies market regime (trending/sideways)
4. **Risk Manager** — Gates trades through position limits, daily loss limits, exposure caps, latency budget, and regime-adaptive sizing
5. **Order Manager** — Submits approved trades to Polymarket CLOB with fill tracking and trade logging

## Quickstart

```bash
# 1. Clone and set up
cp .env.example .env
# Edit .env with your Polymarket API credentials

# 2. Create virtual environment
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 3. Run in dry-run mode (no real orders)
python main.py --dry-run

# 4. Run with custom capital
python main.py --dry-run --capital 5000
```

## Usage

```bash
python main.py                   # Live trading (requires valid API keys)
python main.py --dry-run         # Paper trading — logs trades without submitting
python main.py --capital 10000   # Override starting capital (default: $10,000 USDC)
```

## Configuration

All configuration is via environment variables (`.env` file) or command-line flags. See `.env.example` for all available settings.

### Key Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `MIN_EDGE_THRESHOLD` | 0.003 (0.3%) | Minimum edge to trigger a trade |
| `MAX_EDGE_THRESHOLD` | 0.05 (5%) | Maximum edge (above = likely stale) |
| `MAX_POSITION_PCT` | 0.005 (0.5%) | Max capital per single trade |
| `MAX_DAILY_LOSS_PCT` | 0.02 (2%) | Daily drawdown halt trigger |
| `MAX_LATENCY_MS` | 100 | Detection-to-trade latency budget (ms) |

## Project Structure

```
crypto_arbitrage/
├── main.py                  # Entry point — Bot orchestrator
├── config.py                # Configuration (frozen dataclasses)
├── src/
│   ├── models.py            # Domain models
│   ├── polymarket_client.py # Polymarket CLOB API client
│   ├── prediction_sources.py # BTC price feeds
│   ├── strategy.py          # EMA-based strategy engine
│   ├── risk_manager.py      # Risk controls
│   ├── order_manager.py     # Order lifecycle
│   └── logger_setup.py      # Logging setup
├── docs/                    # Implementation documentation
├── tests/                   # Test suite
└── logs/                    # Runtime logs
```

## Documentation

See `docs/README.md` for the full documentation index. Key documents:

- [Strategy Engine](docs/strategy.md) — EMA regime detection, edge computation, signal generation
- [Risk Management](docs/risk.md) — Position sizing, daily limits, halt logic
- [Polymarket Client](docs/polymarket_client.md) — REST + WebSocket API integration
- [Configuration Reference](docs/config.md) — All configurable parameters

## Dependencies

- **aiohttp** — Async HTTP/WebSocket client
- **numpy** — Numerical computing (regression, volatility)
- **orjson** — Fast JSON serialization
- **python-dotenv** — Environment variable loading
