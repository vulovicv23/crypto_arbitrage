# CLAUDE.md — Crypto Arbitrage Bot

## Project Identity

This is a **Python 3.13 application** implementing a BTC latency-arbitrage trading bot for Polymarket prediction markets. It detects divergences between real-time BTC price feeds (Binance, CryptoCompare, CoinGecko) and Polymarket's short-duration BTC Up/Down contract prices, then trades the edge within a strict latency budget.

The bot **auto-discovers** rotating 5-minute and 15-minute BTC Up/Down markets via the Gamma API — these markets are created and expire continuously, so static condition IDs don't work.

This is a **single-component project** (not a monorepo). All source code lives in `src/` with configuration in `config.py` and the main entry point in `main.py`.

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                         MAIN LOOP                                │
│                                                                   │
│  ┌──────────────────┐                                            │
│  │ Market Discovery  │  <-- Gamma API: discovers 5m/15m markets  │
│  │ (periodic scan)   │────┐                                      │
│  └──────────────────┘    │ new token_ids                        │
│                           v                                       │
│  ┌─────────────┐   price_queue   ┌──────────────┐               │
│  │ BinanceWS   │───────┐         │  Prediction  │               │
│  │ CryptoComp  │───────┼────────>│  Aggregator  │               │
│  │ CoinGecko   │───────┘         └──────┬───────┘               │
│                                         │ prediction_queue       │
│  ┌───────────────┐                      v                        │
│  │ Polymarket WS │──book──>  ┌──────────────────┐               │
│  │ (book stream) │           │  Strategy Engine  │               │
│  └───────────────┘           └────────┬─────────┘               │
│                                       │ signal_queue             │
│                                       v                          │
│                             ┌─────────────────┐                  │
│                             │  Risk Manager   │                  │
│                             └────────┬────────┘                  │
│                                      │                           │
│                                      v                           │
│                             ┌─────────────────┐                  │
│                             │  Order Manager  │──> Polymarket    │
│                             └─────────────────┘     CLOB REST    │
│                                                                   │
│  ┌──────────────────────────────────────────────────────────┐    │
│  │              Health Monitor (periodic stats)              │    │
│  └──────────────────────────────────────────────────────────┘    │
└──────────────────────────────────────────────────────────────────┘
```

**Data flow:** Market Discovery finds active 5m/15m BTC Up/Down markets via Gamma API and feeds token IDs to the strategy + WS subscriber. Price sources push `PriceTick` objects into a shared queue. When ML is enabled, a price splitter fans ticks to both the `PredictionAggregator` and the `MLPredictor`. The `PredictionAggregator` blends them into `Prediction` objects using linear regression; the `MLPredictor` computes 49 features and runs LightGBM inference to emit its own `Prediction` objects. Both feed into the `StrategyEngine`, which compares predictions against Polymarket order books and emits `Signal` objects. The `RiskManager` gates each signal, and the `OrderManager` submits approved trades to the Polymarket CLOB.

---

## Critical Rules

1. **Never commit secrets.** API keys, wallet private keys go in `.env` only. `.env` is in `.gitignore`.
2. **Dry-run mode is the default for development.** Use `--dry-run` to test without placing real orders.
3. **All timestamps are nanoseconds.** Use `time.time_ns()` for latency-critical paths. Convert to human-readable only at log boundaries.
4. **Async-first.** Use `async/await` throughout. All I/O goes through `aiohttp`.
5. **Frozen dataclasses for config.** All configuration is immutable after loading.
6. **Type hints everywhere.** All function signatures must have type hints.
7. **Queue-based pipeline.** Components communicate via `asyncio.Queue` — never call between pipeline stages directly.
8. **Auto-discovery for markets.** Never hardcode condition IDs for short-duration markets — they rotate every 5/15 minutes.

---

## Git Policy

**ABSOLUTE RULE — NO EXCEPTIONS:**

**Do NOT create git worktrees.** Never use `git worktree`, `EnterWorktree`, or any worktree-based isolation. All work must be done directly on the current branch in the main working tree.

Under **NO circumstance** should you commit and push changes directly. Do not use `git commit`, `git push`, or any combination of git commands to commit and push code changes.

The **ONLY** way to commit and push changes is by running the `/syncDocsAndCommit` command.

This ensures documentation is updated alongside code changes and maintains consistency across the codebase.

---

## Clarification Policy

**ALWAYS ask clarifying questions before planning or executing actions when:**

- Something is unclear or ambiguous
- A request could be interpreted multiple ways
- Implementation details are not specified
- You're uncertain about the intended behavior or scope

Keep asking questions until you have complete clarity. It's better to ask too many questions than to make incorrect assumptions.

---

## Project Structure

```
crypto_arbitrage/
├── CLAUDE.md                     # This file
├── main.py                       # Entry point, Bot orchestrator, DryRunOrderManager
├── config.py                     # Configuration (frozen dataclasses, .env loading)
├── requirements.txt              # Python dependencies
├── .env.example                  # Environment variable template
├── .env                          # Secrets and tunables (not committed)
├── .gitignore
│
├── src/                          # Source code
│   ├── __init__.py
│   ├── models.py                 # Domain models (PriceTick, Signal, Order, etc.)
│   ├── market_discovery.py       # Auto-discovers 5m/15m BTC markets from Gamma API
│   ├── polymarket_client.py      # Polymarket CLOB API client (REST + WebSocket)
│   ├── prediction_sources.py     # Price feeds (Binance, CryptoCompare, CoinGecko)
│   ├── strategy.py               # EMA-based strategy engine
│   ├── risk_manager.py           # Position sizing & risk controls
│   ├── order_manager.py          # Order lifecycle management
│   ├── logger_setup.py           # Structured logging (console + JSON file)
│   ├── synthetic_books.py        # Synthetic order book generation (paper mode)
│   ├── ws_client.py              # WebSocket client with auto-reconnect
│   ├── ws_pool.py                # WebSocket connection pool (500 tokens/conn)
│   └── ml/                       # Machine learning prediction module
│       ├── __init__.py
│       ├── features.py           # Feature engineering (49 features, batch + streaming)
│       └── predictor.py          # LightGBM inference wrapper (async)
│
├── docs/                         # Implementation documentation
│   ├── ARCHITECTURE.md           # System design and data flow
│   ├── STRATEGY.md               # Trading logic and edge computation
│   ├── RISK_MANAGEMENT.md        # Risk rules and position sizing
│   ├── API_INTEGRATION.md        # Polymarket + external API details
│   ├── CONFIGURATION.md          # All configurable parameters
│   └── DEVELOPMENT.md            # Dev workflow, testing, debugging
│
├── tests/                        # Test suite
│   ├── __init__.py
│   ├── test_strategy.py          # Strategy engine tests
│   └── test_features.py          # ML feature parity tests
│
├── tools/                        # Development & training tools
│   ├── test_matrix.py            # Parallel bot testing framework
│   ├── liquidity_scanner.py      # Market liquidity discovery
│   ├── collect_data.py           # Binance historical kline downloader
│   ├── train_model.py            # LightGBM training with walk-forward CV
│   └── backtest.py               # Historical backtesting framework
│
├── docker-compose.yml            # PostgreSQL for ML data storage
├── schema.sql                    # Database schema (klines, labels, model runs)
├── models/                       # Trained ML model artifacts (gitignored)
├── logs/                         # Runtime logs (gitignored)
│
└── .claude/                      # Claude Code configuration
    ├── settings.local.json       # Permissions, hooks, MCP servers
    └── commands/                 # Slash commands
```

---

## Tech Stack

| Component | Package | Version |
|-----------|---------|---------|
| Python | - | 3.13+ |
| HTTP client | aiohttp | >=3.9,<4 |
| Numerics | numpy | >=1.26,<2 |
| JSON | orjson | >=3.9,<4 |
| Env loading | python-dotenv | >=1.0,<2 |
| ML model | lightgbm | >=4.0,<5 |
| ML utilities | scikit-learn | >=1.4,<2 |
| PostgreSQL | asyncpg | >=0.29,<1 |
| Serialization | joblib | >=1.3,<2 |
| HPO | optuna | >=3.5,<4 |

---

## Environment Variables (.env)

```
# Polymarket CLOB API
POLY_API_KEY=<your-api-key>
POLY_API_SECRET=<your-api-secret>
POLY_API_PASSPHRASE=<your-passphrase>
POLY_PRIVATE_KEY=<ethereum-private-key-hex>
POLY_CHAIN_ID=137
POLY_BTC_CONDITION_IDS=                 # Leave empty (auto-discovery preferred)

# Market Discovery (auto-discovers rotating 5m/15m BTC Up/Down markets)
DISCOVERY_ENABLED=true
DISCOVERY_ASSETS=BTC
DISCOVERY_TIMEFRAMES=5m,15m
DISCOVERY_INTERVAL_S=20
DISCOVERY_MIN_SECONDS=60

# External Data Sources
CRYPTOCOMPARE_API_KEY=<your-key>

# Strategy
MIN_EDGE_THRESHOLD=0.003
MAX_EDGE_THRESHOLD=0.05
PREDICTION_HORIZON_S=900

# Risk
MAX_POSITION_PCT=0.005
MAX_DAILY_LOSS_PCT=0.02
MAX_OPEN_POSITIONS=20

# Execution
MAX_LATENCY_MS=100
MAX_ORDERS_PER_SECOND=50
HTTP_POOL_SIZE=20

# Logging
LOG_LEVEL=INFO

# ML Prediction (optional, disabled by default)
ML_ENABLED=false
ML_MODEL_PATH=models/btc_5m_v2.pkl
ML_FEATURE_WINDOW=4000
ML_PREDICTION_INTERVAL=0.25
ML_MIN_CONFIDENCE=0.1
ML_MAX_PREDICTED_RETURN=0.01
ML_HORIZON_S=300
```

---

## Commands

### Run Commands

| Command | Description |
|---------|-------------|
| `/runInfra` | Start PostgreSQL via Docker Compose |
| `/runArbBot` | Run the arbitrage bot locally |

### Documentation Commands

| Command | Description |
|---------|-------------|
| `/syncDocsAndCommit` | Format code, update docs, commit, and push |
| `/updateProjectDocs` | Update docs for a specific module (no commit) |
| `/documentChangedProjects` | Update docs for all changed modules (no commit) |
| `/auditProjectDocs` | Deep audit and fix documentation |
| `/auditProjectTests` | Audit test coverage |

### Development Commands

| Command | Description |
|---------|-------------|
| `/createFeatureTasks` | Create executable task specs for a feature |
| `/executeTask` | Execute a task spec file with multi-agent exploration |
| `/debug` | Debug an issue using code analysis |
| `/generateTestsForChanges` | Analyze code changes and generate tests |

---

## Code Style

- **Imports:** Absolute (`from src.models import Signal`, `from config import AppConfig`)
- **Docstrings:** Google style
- **Naming:** snake_case functions/variables, PascalCase classes, UPPER_SNAKE constants
- **Errors:** Standard exceptions with clear messages
- **Logging:** stdlib `logging` with colored console + JSON file output
- **Line length:** No strict formatter configured — keep lines reasonable (~100 chars)
- **Dataclasses:** Use `@dataclass(frozen=True)` for config, `@dataclass(slots=True)` for domain models

---

## Key Patterns

### Auto-Discovery of Rotating Markets

Polymarket creates new 5m/15m BTC Up/Down markets on a rolling basis. The `MarketDiscovery` class in `src/market_discovery.py` periodically queries the Gamma API to find active markets by:
1. Fetching active markets with `end_date_min=now` and `end_date_max=now+1day`
2. Filtering by question containing "up or down" and slug matching `-updown-5m-` or `-updown-15m-`
3. Extracting YES/NO token IDs from `clobTokenIds` field
4. Feeding new token IDs to the strategy engine and WS book subscriber

### Queue-Based Pipeline

All pipeline stages communicate through bounded `asyncio.Queue` objects with back-pressure handling:

```python
# Producer: drop oldest if queue is full
try:
    queue.put_nowait(item)
except asyncio.QueueFull:
    try:
        queue.get_nowait()
    except asyncio.QueueEmpty:
        pass
    queue.put_nowait(item)

# Consumer: blocking get
item = await queue.get()
```

### Nanosecond Timestamps

All latency-critical models use `time.time_ns()`:

```python
@dataclass(slots=True)
class PriceTick:
    source: str
    price: float
    timestamp_ns: int = field(default_factory=lambda: time.time_ns())
```

### HMAC Authentication

Polymarket CLOB API uses HMAC-SHA256 signing:

```python
timestamp = str(int(time.time()))
message = timestamp + method.upper() + path + body
signature = hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()
```

### Rate Limiting

Token bucket algorithm in `PolymarketClient` caps requests to `max_orders_per_second`.

### Market Regime Detection

Dual-EMA crossover (fast=12, slow=26) + volatility classifies market as TRENDING_UP, TRENDING_DOWN, or SIDEWAYS. Position sizing adapts to regime (sideways=0.4x, trending=1.0x).

---

## Common Modification Tasks

### Adding a new prediction source
1. Subclass `PriceSource` in `src/prediction_sources.py`
2. Implement `_run()` async method that emits `PriceTick` objects
3. Add to the sources list in `main.py` `Bot.start()`

### Adjusting market timeframes
Edit `DISCOVERY_TIMEFRAMES` in `.env`. Supported: `5m`, `15m`, `1h`, `4h`.

### Adding a new asset (e.g., ETH)
1. Add to `DISCOVERY_ASSETS=BTC,ETH` in `.env`
2. The discovery module already supports any asset with slug format `{asset}-updown-{tf}-{ts}`
3. Add a new price source for ETH trades in `prediction_sources.py`

### Changing risk parameters
Edit values in `.env` — no code changes needed. See `docs/RISK_MANAGEMENT.md`.

---

## Testing

### Conventions

- Test files: `tests/test_{module}.py`
- Run tests: `pytest tests/ -v --tb=short`
- Mock HTTP: Use `aiohttp` test utilities or `unittest.mock.AsyncMock`
- Assertions: Plain `assert` statements

---

## Documentation Structure

Implementation documentation lives in `docs/`:

| Document | Topic |
|----------|-------|
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | System design, data flow, component interaction |
| [STRATEGY.md](docs/STRATEGY.md) | Edge computation, regime detection, signal generation |
| [RISK_MANAGEMENT.md](docs/RISK_MANAGEMENT.md) | Position sizing, loss limits, halt logic, cooldowns |
| [API_INTEGRATION.md](docs/API_INTEGRATION.md) | Polymarket CLOB/Gamma + external price sources |
| [CONFIGURATION.md](docs/CONFIGURATION.md) | All environment variables and tunables |
| [DEVELOPMENT.md](docs/DEVELOPMENT.md) | Dev workflow, debugging, deployment |

**Before modifying code, read the relevant docs first.**

| Working On | Read First |
|---|---|
| Strategy logic | `docs/STRATEGY.md` + `src/models.py` |
| Risk controls | `docs/RISK_MANAGEMENT.md` + `src/models.py` |
| Order execution | `docs/API_INTEGRATION.md` + `src/order_manager.py` |
| Price feeds | `docs/API_INTEGRATION.md` + `src/prediction_sources.py` |
| Market discovery | `docs/API_INTEGRATION.md` + `src/market_discovery.py` |
| Configuration | `docs/CONFIGURATION.md` |

---

## MCP Tool Usage Guidelines

Four MCP tool servers are available: Serena (semantic code tools), Context7 (library docs), Sequential Thinking, and Postgres.

### Serena (Semantic Code Tools)

Use Serena when you need to:

- **Understand code structure**: `get_symbols_overview`, `find_symbol`
- **Trace dependencies**: `find_referencing_symbols`
- **Precise edits**: `replace_symbol_body`, `insert_after_symbol`, `rename_symbol`

Do NOT use Serena for: simple file reads (use Read), simple text search (use Grep), file listing (use Glob).

### Context7 (Library Documentation)

Use Context7 for current documentation on: aiohttp, numpy, orjson, pytest.

### Sequential Thinking

Use for architectural decisions, complex debugging, or algorithm design where multiple approaches exist.

### Postgres MCP

Connects to the local PostgreSQL instance (`localhost:6501`) for inspecting database state when the database is running.
