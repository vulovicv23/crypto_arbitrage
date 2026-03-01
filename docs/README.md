# Documentation Index

Implementation documentation for the Crypto Arbitrage Bot.

## Module Documentation

| Document | Source File | Topic |
|----------|-------------|-------|
| [models.md](models.md) | `src/models.py` | Domain models — PriceTick, Prediction, Signal, Order, Position, DailyPnL |
| [polymarket_client.md](polymarket_client.md) | `src/polymarket_client.py` | Polymarket CLOB API client — REST endpoints, WebSocket book stream, HMAC auth |
| [prediction_sources.md](prediction_sources.md) | `src/prediction_sources.py` | Price feed sources — Binance WS, CryptoCompare REST, CoinGecko REST, PredictionAggregator |
| [strategy.md](strategy.md) | `src/strategy.py` | Strategy engine — EMA regime detection, edge computation, signal generation |
| [risk.md](risk.md) | `src/risk_manager.py` | Risk management — position sizing, daily loss limits, exposure caps, cooldown |
| [order_manager.md](order_manager.md) | `src/order_manager.py` | Order lifecycle — signal processing, order submission, fill tracking, trade logging |
| [config.md](config.md) | `config.py` | Configuration reference — all parameters with defaults and environment variables |

## Quick Reference

| Working On | Read First |
|---|---|
| Strategy logic | [strategy.md](strategy.md) + [models.md](models.md) |
| Risk controls | [risk.md](risk.md) + [models.md](models.md) |
| Order execution | [order_manager.md](order_manager.md) + [polymarket_client.md](polymarket_client.md) |
| Price feeds | [prediction_sources.md](prediction_sources.md) + [models.md](models.md) |
| Configuration | [config.md](config.md) |
| API integration | [polymarket_client.md](polymarket_client.md) |

## Cross-Cutting Concerns

- **Logging**: `src/logger_setup.py` — Colored console + JSON file output, configured via `LoggingConfig`
- **Entry point**: `main.py` — `Bot` class orchestrates all components, `DryRunOrderManager` for paper trading
