---
allowed-tools: Bash(python main.py:*), Bash(python:*)
description: Run Arbitrage Bot
---

# Run Arbitrage Bot

Start the BTC latency-arbitrage bot for local development.

## Prerequisites

- `.env` file with required environment variables (see `.env.example`)
- Virtual environment activated with dependencies installed (`pip install -r requirements.txt`)

## Run Command

Run the bot in dry-run mode (no real orders):

```
python main.py --dry-run
```

Run the bot in live mode (requires valid API keys):

```
python main.py
```

Run with custom starting capital:

```
python main.py --dry-run --capital 5000
```

## What Happens

The bot will:
1. Load configuration from `.env` and `config.py`
2. Start price sources (Binance WS, CryptoCompare REST, CoinGecko REST)
3. Start the PredictionAggregator
4. Start the StrategyEngine
5. Start the OrderManager (or DryRunOrderManager in dry-run mode)
6. Connect to Polymarket WebSocket for book updates (live mode only)
7. Launch the health monitor (stats every 30s)

## Stopping

Press Ctrl+C for graceful shutdown. The bot will:
- Stop price sources
- Cancel pending orders (live mode)
- Cancel all async tasks
- Close Polymarket connection
