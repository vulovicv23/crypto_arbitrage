# Domain Models

Source: `src/models.py`

All domain models are Python dataclasses with `slots=True` for memory efficiency. Timestamps use Unix epoch **nanoseconds** (`int`) for latency-critical paths.

## Enums

### Side
Trade direction: `BUY` or `SELL`.

### OrderStatus
Order lifecycle states: `PENDING` → `SUBMITTED` → `FILLED` / `PARTIALLY_FILLED` / `CANCELLED` / `REJECTED` / `EXPIRED`.

### MarketRegime
Detected via dual-EMA crossover + volatility in the strategy engine:
- `TRENDING_UP` — Fast EMA above slow EMA with sufficient volatility
- `TRENDING_DOWN` — Fast EMA below slow EMA with sufficient volatility
- `SIDEWAYS` — Low volatility or EMAs too close

### SignalStrength
Edge relative to `min_edge_threshold`:
- `WEAK` — Edge < 1.5x threshold
- `MODERATE` — Edge 1.5x–2.5x threshold
- `STRONG` — Edge > 2.5x threshold

## Data Models

### PriceTick
A single price observation from any source.

| Field | Type | Description |
|-------|------|-------------|
| `source` | `str` | Source name ("binance", "cryptocompare", "coingecko") |
| `price` | `float` | BTC/USD price |
| `timestamp_ns` | `int` | Unix epoch nanoseconds |
| `volume` | `float` | Trade volume (0 for REST sources) |

### Prediction
A directional prediction for BTC price at a given horizon.

| Field | Type | Description |
|-------|------|-------------|
| `source` | `str` | Source name ("aggregator") |
| `predicted_price` | `float` | Predicted BTC price at horizon |
| `current_price` | `float` | Current BTC price |
| `horizon_s` | `int` | Prediction horizon in seconds (default: 900) |
| `confidence` | `float` | 0.0–1.0 confidence score |

Properties:
- `predicted_return` — Signed fractional return: `(predicted - current) / current`
- `direction` — `Side.BUY` if positive return, else `Side.SELL`

### PolymarketBook
Snapshot of a Polymarket order book for one outcome.

| Field | Type | Description |
|-------|------|-------------|
| `condition_id` | `str` | Market condition ID |
| `token_id` | `str` | Token ID for this outcome |
| `best_bid` | `float` | Best bid price |
| `best_ask` | `float` | Best ask price |
| `mid_price` | `float` | Midpoint of bid/ask |
| `spread` | `float` | Ask minus bid |

Property: `is_valid` — True if bid > 0 and ask > bid.

### Signal
An actionable trade signal produced by the strategy.

| Field | Type | Description |
|-------|------|-------------|
| `condition_id` | `str` | Market condition ID |
| `token_id` | `str` | Token to trade |
| `side` | `Side` | BUY or SELL |
| `edge` | `float` | Expected profit fraction |
| `strength` | `SignalStrength` | WEAK / MODERATE / STRONG |
| `regime` | `MarketRegime` | Current market regime |
| `prediction` | `Prediction` | The prediction that generated this signal |
| `book` | `PolymarketBook` | Book snapshot at signal time |

### Order
An order to be submitted to the Polymarket CLOB.

| Field | Type | Description |
|-------|------|-------------|
| `order_id` | `str` | Local UUID |
| `condition_id` | `str` | Market condition ID |
| `token_id` | `str` | Token to trade |
| `side` | `Side` | BUY or SELL |
| `price` | `float` | Limit price |
| `size` | `float` | Size in USDC |
| `status` | `OrderStatus` | Current lifecycle state |
| `latency_ms` | `float` | Detection-to-submit latency |

Methods:
- `mark_submitted(exchange_id)` — Transition to SUBMITTED, record latency
- `mark_filled(fill_price, fill_size)` — Transition to FILLED

### Position
Tracks an open position.

| Field | Type | Description |
|-------|------|-------------|
| `condition_id` | `str` | Market condition ID |
| `token_id` | `str` | Token ID |
| `side` | `Side` | BUY or SELL |
| `entry_price` | `float` | Entry price |
| `size` | `float` | Position size |
| `unrealized_pnl` | `float` | Current unrealized P&L |

Method: `update_pnl(current_price)` — Recalculate unrealized P&L.

### DailyPnL
Running daily P&L tracker.

| Field | Type | Description |
|-------|------|-------------|
| `date` | `str` | Trading date |
| `realized_pnl` | `float` | Total realized P&L |
| `total_trades` | `int` | Number of trades |
| `winning_trades` | `int` | Winning trades count |
| `max_drawdown` | `float` | Maximum peak-to-trough drawdown |

Property: `win_rate` — `winning_trades / total_trades`.
Method: `record_trade(pnl, volume)` — Update all running stats including drawdown tracking.
