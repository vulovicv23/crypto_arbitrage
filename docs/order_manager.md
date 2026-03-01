# Order Manager

Source: `src/order_manager.py`

Async order lifecycle manager. Consumes signals, gates through risk checks, builds orders, submits to Polymarket, and tracks fills.

## Class: OrderManager

### Constructor

```python
OrderManager(
    config: AppConfig,
    signal_queue: asyncio.Queue[Signal],
    poly_client: PolymarketClient,
    risk_manager: RiskManager,
)
```

### Pipeline

```
signal_queue → risk_check → build_order → submit → track → close
```

### Main Loop

`run()` — Blocking async loop that consumes signals from `signal_queue` and processes each through the full pipeline.

### Signal Processing

1. **Risk check** — `risk_manager.check_signal(signal)` → approved/rejected with size
2. **Build order** — Creates `Order` with:
   - BUY: price = `best_ask` (lift the ask)
   - SELL: price = `best_bid` (hit the bid)
   - Size from risk manager
3. **Submit** — Sends to Polymarket CLOB via `poly_client.place_order()`

### Order Tracking

After submission, spawns an async task (`_track_order`) that polls for fill status:

| Parameter | Value |
|-----------|-------|
| Initial poll interval | 0.5s |
| Backoff multiplier | 1.5x |
| Max poll interval | 5.0s |
| Timeout | 30s |

**Fill detection:** Checks if the order is no longer in `get_open_orders()`. If absent, assumes filled (optimistic — production would use WebSocket fill feed).

**On fill:**
1. Mark order as `FILLED`
2. Create `Position` and register with risk manager
3. Log trade to JSONL file
4. Move from pending to filled list

**On timeout:**
1. Cancel the stale order on exchange
2. Mark as `EXPIRED`
3. Remove from pending

### Position Exit

```python
await order_manager.close_position(token_id, current_price)
```

Submits the opposite side order. Returns realized P&L or None on failure.

### Trade Logging

Each filled trade is appended as a JSON line to `logs/trades.jsonl`:

```json
{
  "ts": 1234567890.123,
  "order_id": "uuid",
  "exchange_id": "exchange-uuid",
  "condition_id": "...",
  "token_id": "...",
  "side": "BUY",
  "price": 0.65,
  "size": 25.0,
  "fill_price": 0.65,
  "fill_size": 25.0,
  "latency_ms": 45.2,
  "edge": 0.0042,
  "strength": "MODERATE",
  "regime": "TRENDING_UP",
  "pred_return": 0.005,
  "pred_confidence": 0.82
}
```

### Emergency

`cancel_all_orders()` — Cancels all orders on exchange and clears local pending list.

### Stats

`stats()` returns:

| Key | Description |
|-----|-------------|
| `submitted` | Total orders submitted |
| `filled` | Total orders filled |
| `rejected` | Total orders rejected by exchange |
| `risk_blocked` | Total signals blocked by risk |
| `pending` | Current pending orders |
| `daily_pnl` | Today's realized P&L |
| `win_rate` | Today's win rate |
| `total_volume` | Today's total volume |

## Class: DryRunOrderManager

Subclass defined in `main.py`. Overrides `_submit_order` to simulate fills locally without hitting the exchange. Logs trades with `DRY-RUN ORDER:` prefix.
