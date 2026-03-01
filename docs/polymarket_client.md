# Polymarket CLOB API Client

Source: `src/polymarket_client.py`

Async client for the Polymarket Central Limit Order Book (CLOB) API. Provides REST methods for order management and WebSocket subscription for real-time book updates.

## Class: PolymarketClient

### Constructor

```python
PolymarketClient(config: PolymarketConfig, exec_config: ExecutionConfig)
```

Maintains a single `aiohttp.ClientSession` with a configurable connection pool for low-latency HTTP/1.1 keep-alive.

### Lifecycle

| Method | Description |
|--------|-------------|
| `connect()` | Open HTTP session with connection pooling (`http_pool_size` connections, DNS cache 60s) |
| `close()` | Close WebSocket and HTTP session |

### Authentication

HMAC-SHA256 signing per Polymarket's scheme:

```
message = timestamp + METHOD + path + body
signature = HMAC-SHA256(api_secret, message)
```

Headers sent: `POLY-API-KEY`, `POLY-SIGNATURE`, `POLY-TIMESTAMP`, `POLY-PASSPHRASE`.

### Rate Limiting

Token bucket algorithm capped at `max_orders_per_second` (default: 50). If tokens are exhausted, sleeps in 1ms increments until a token is available.

Handles HTTP 429 responses with `Retry-After` header backoff.

### REST Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `get_order_book(token_id)` | `GET /book` | Fetch full order book snapshot, returns `PolymarketBook` |
| `get_markets()` | `GET /markets` | List available markets |
| `place_order(token_id, side, price, size)` | `POST /order` | Place GTC limit order |
| `cancel_order(order_id)` | `DELETE /order/{id}` | Cancel single order |
| `cancel_all()` | `DELETE /order/all` | Cancel all open orders |
| `get_open_orders()` | `GET /orders?open=true` | Fetch current open orders |
| `get_positions()` | `GET /positions` | Fetch current positions |

### Retry Policy

All REST requests retry up to `max_retries` (default: 3) with exponential backoff starting at `retry_backoff_base_ms` (default: 10ms). Retries on `aiohttp.ClientError` and `asyncio.TimeoutError`. HTTP 429 uses server-provided `Retry-After` delay.

### WebSocket Book Stream

```python
await client.subscribe_books(token_ids, callback)
```

- Subscribes to `book` channel for each token ID
- Auto-reconnects on connection loss (1s delay)
- Heartbeat interval: 15s
- Receive timeout: 30s
- Handles `book_update` and `book_snapshot` message types
- Calls `callback(PolymarketBook)` for each update

### Connection Configuration

| Parameter | Source | Default |
|-----------|--------|---------|
| REST URL | `POLY_REST_URL` | `https://clob.polymarket.com` |
| WS URL | `POLY_WS_URL` | `wss://ws-subscriptions-clob.polymarket.com/ws/market` |
| Pool size | `HTTP_POOL_SIZE` | 20 |
| Request timeout | hardcoded | 5s |
| Heartbeat | hardcoded | 15s |
