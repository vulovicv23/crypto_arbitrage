# API Integration

> **Source files:** `src/polymarket_client.py`, `src/prediction_sources.py`, `src/market_discovery.py`
> **Config:** `PolymarketConfig`, `PredictionSourcesConfig`, `DiscoveryConfig`, `ExecutionConfig` in `config.py`

This document covers all external API integrations: Polymarket (Gamma API, CLOB REST, CLOB WebSocket), Binance WebSocket, CryptoCompare REST, and CoinGecko REST.

---

## Table of Contents

1. [Gamma API (Market Discovery)](#gamma-api-market-discovery)
2. [CLOB REST API](#clob-rest-api)
3. [CLOB WebSocket](#clob-websocket)
4. [HMAC-SHA256 Auth Scheme](#hmac-sha256-auth-scheme)
5. [Rate Limiting](#rate-limiting)
6. [Binance WebSocket](#binance-websocket)
7. [CryptoCompare REST](#cryptocompare-rest)
8. [CoinGecko REST](#coingecko-rest)
9. [Error Handling and Retry Logic](#error-handling-and-retry-logic)

---

## Gamma API (Market Discovery)

> **Source:** `src/market_discovery.py`
> **Base URL:** `https://gamma-api.polymarket.com`
> **Authentication:** None required (public API)

The Gamma API is used exclusively for discovering active BTC Up/Down markets. It is queried periodically by the `MarketDiscovery` class.

### Endpoint

```
GET /markets
```

### Parameters

| Parameter       | Value                                         | Description                                |
|-----------------|-----------------------------------------------|--------------------------------------------|
| `active`        | `"true"`                                      | Only active (unresolved) markets           |
| `closed`        | `"false"`                                     | Exclude closed markets                     |
| `end_date_min`  | Current UTC time (ISO 8601)                   | Markets ending after now                   |
| `end_date_max`  | Current UTC time + 1 day (ISO 8601)           | Markets ending within 24 hours             |
| `order`         | `"volume24hr"`                                | Sort by 24h trading volume                 |
| `ascending`     | `"false"`                                     | Highest volume first                       |
| `limit`         | `"500"`                                       | Page size (max per request)                |
| `offset`        | `"0"`, `"500"`, etc.                          | Pagination offset                          |

### Pagination

The discovery module paginates through results with up to 20 pages (safety limit). Each page fetches up to 500 markets. Pagination stops when a response returns fewer than 500 results.

### Response Format (per market object)

```json
{
  "conditionId": "0x1234...",
  "question": "Will Bitcoin go up or down in the next 5 minutes?",
  "slug": "btc-updown-5m-1771942800",
  "endDate": "2026-02-27T14:05:00Z",
  "clobTokenIds": "[\"token_yes_id\", \"token_no_id\"]",
  "outcomes": "[\"Up\", \"Down\"]",
  "volume24hr": 50000.0
}
```

Note: `clobTokenIds` and `outcomes` can be either JSON strings or arrays depending on the API version. The parser handles both.

### Slug Patterns

The discovery module extracts asset and timeframe from the market slug:

| Pattern Regex               | Timeframe |
|-----------------------------|-----------|
| `-updown-5m-`              | 5m        |
| `-updown-15m-`             | 15m       |
| `-updown-1h-`              | 1h        |
| `-updown-4h-`              | 4h        |

Asset is extracted via: `^([a-z]+)-updown-\d+[mh]-\d+$` (group 1, uppercased).

### Filtering Pipeline

Each market goes through 5 filters:

1. **Question contains "up or down"** (case-insensitive)
2. **Asset matches configured assets** (default: BTC). Checks slug first, then question text for "bitcoin"/"btc".
3. **Timeframe matches configured timeframes** (default: 5m, 15m)
4. **Enough time remaining** (>= `DISCOVERY_MIN_SECONDS`, default: 60 seconds)
5. **Has both YES (Up) and NO (Down) token IDs**

### Discovery Cycle Timing

- Interval: `DISCOVERY_INTERVAL_S` (default: 20 seconds)
- HTTP timeout: 15 seconds per request
- Markets are tracked by `condition_id` in an internal dict
- Expired markets (past their `end_date`) are automatically removed

---

## CLOB REST API

> **Source:** `src/polymarket_client.py`
> **Base URL:** `https://clob.polymarket.com` (configurable via `POLY_REST_URL`)
> **Authentication:** HMAC-SHA256 (see below)

### Endpoints

| Method   | Path                     | Description                        | Used By             |
|----------|--------------------------|------------------------------------|---------------------|
| `GET`    | `/book?token_id={id}`    | Fetch order book snapshot          | `get_order_book()`  |
| `GET`    | `/markets`               | List available markets             | `get_markets()`     |
| `POST`   | `/order`                 | Place a limit order                | `place_order()`     |
| `DELETE` | `/order/{order_id}`      | Cancel a specific order            | `cancel_order()`    |
| `DELETE` | `/order/all`             | Cancel all open orders             | `cancel_all()`      |
| `GET`    | `/orders?open=true`      | Fetch current open orders          | `get_open_orders()` |
| `GET`    | `/positions`             | Fetch current positions/balances   | `get_positions()`   |

### Order Placement Payload

```json
{
  "tokenID": "0xabc123...",
  "side": "BUY",
  "price": "0.5500",
  "size": "25.00",
  "type": "GTC"
}
```

All orders are placed as **GTC (Good-Til-Cancelled)** limit orders. Price is rounded to 4 decimal places, size to 2 decimal places.

### Order Book Response

```json
{
  "bids": [
    {"price": "0.5400", "size": "100.00"},
    {"price": "0.5300", "size": "200.00"}
  ],
  "asks": [
    {"price": "0.5600", "size": "150.00"},
    {"price": "0.5700", "size": "250.00"}
  ]
}
```

Parsed into a `PolymarketBook` dataclass:
- `best_bid` = first bid price
- `best_ask` = first ask price
- `mid_price` = `(best_bid + best_ask) / 2`
- `spread` = `best_ask - best_bid`

### Order Response

```json
{
  "orderID": "exchange-order-id-here",
  "id": "alternate-id-format"
}
```

The client checks both `orderID` and `id` keys for compatibility.

---

## CLOB WebSocket

> **Source:** `src/polymarket_client.py`, method `subscribe_books()`
> **URL:** `wss://ws-subscriptions-clob.polymarket.com/ws/market` (configurable via `POLY_WS_URL`)
> **Authentication:** None (public book data)

### Subscription Format

For each token ID, a subscription message is sent:

```json
{
  "type": "subscribe",
  "channel": "book",
  "assets_id": "token_id_here"
}
```

### Message Types

The client processes two message types:

| Type             | Description                                   |
|------------------|-----------------------------------------------|
| `book_update`    | Incremental update to the order book          |
| `book_snapshot`  | Full snapshot of the order book               |

Both types share the same response structure:

```json
{
  "type": "book_update",
  "asset_id": "token_id_here",
  "bids": [{"price": "0.54", "size": "100"}],
  "asks": [{"price": "0.56", "size": "150"}]
}
```

### Connection Management

- **Heartbeat:** 15 seconds
- **Receive timeout:** 30 seconds
- **Reconnect:** Automatic on `ClientError` or `TimeoutError`, with 1-second delay
- **Callback:** Each parsed `PolymarketBook` is passed to all registered callbacks via `await cb(book)`

---

## HMAC-SHA256 Auth Scheme

Defined in `PolymarketClient._sign()` (`src/polymarket_client.py`).

### Signature Construction

```python
timestamp = str(int(time.time()))             # Unix epoch seconds
message = timestamp + METHOD + path + body    # e.g., "1709123456POST/order{...}"
signature = HMAC-SHA256(api_secret, message)  # hex digest
```

### Required Headers

| Header             | Value                                       |
|--------------------|---------------------------------------------|
| `POLY-API-KEY`     | `POLY_API_KEY` from config                  |
| `POLY-SIGNATURE`   | HMAC-SHA256 hex digest                      |
| `POLY-TIMESTAMP`   | Unix epoch seconds (string)                 |
| `POLY-PASSPHRASE`  | `POLY_API_PASSPHRASE` from config           |
| `Content-Type`     | `application/json`                          |

### Credentials Required

| Environment Variable   | Description                                     |
|------------------------|-------------------------------------------------|
| `POLY_API_KEY`         | API key from Polymarket developer dashboard     |
| `POLY_API_SECRET`      | API secret for HMAC signing                     |
| `POLY_API_PASSPHRASE`  | Additional passphrase for authentication        |
| `POLY_PRIVATE_KEY`     | Ethereum private key for order signing (Polygon)|

---

## Rate Limiting

Defined in `PolymarketClient._acquire_rate_token()` (`src/polymarket_client.py`).

### Token Bucket Algorithm

```python
max_tokens = MAX_ORDERS_PER_SECOND  # default: 50
refill_rate = max_tokens per second

# On each request:
elapsed = now - last_refill_time
tokens = min(max_tokens, tokens + elapsed * max_tokens)
if tokens >= 1.0:
    tokens -= 1.0
    # proceed with request
else:
    await asyncio.sleep(0.001)  # spin-wait with yielding
```

Key properties:
- Burst capacity: up to `MAX_ORDERS_PER_SECOND` requests instantly.
- Sustained rate: `MAX_ORDERS_PER_SECOND` requests per second.
- If the bucket is empty, the client spin-waits with 1ms sleeps until a token is available.

### HTTP 429 Handling

If the server returns a `429 Too Many Requests` response:

```python
if resp.status == 429:
    wait = float(resp.headers.get("Retry-After", "1"))
    await asyncio.sleep(wait)
    continue  # retry the same request
```

---

## Binance WebSocket

> **Source:** `src/prediction_sources.py`, class `BinanceSource`
> **URL:** `wss://stream.binance.com:9443/ws/btcusdt@trade`
> **Authentication:** None required (public stream)

### Stream Format

The `btcusdt@trade` stream delivers individual trade events:

```json
{
  "e": "trade",
  "E": 1709123456789,
  "s": "BTCUSDT",
  "t": 123456789,
  "p": "65432.10",
  "q": "0.001",
  "T": 1709123456789,
  "m": true
}
```

### Fields Used

| JSON Key | Meaning              | Mapped To                    |
|----------|----------------------|------------------------------|
| `p`      | Trade price (string) | `PriceTick.price` (float)    |
| `q`      | Trade quantity        | `PriceTick.volume` (float)   |
| `T`      | Trade time (ms)      | `PriceTick.timestamp_ns` (converted: `T * 1_000_000`) |

### Connection Management

- **Heartbeat:** 20 seconds
- **Receive timeout:** 30 seconds
- **Reconnect:** On `ClientError` or `TimeoutError`, waits 1 second then reconnects
- **Source name:** `"binance"`

This is the **lowest-latency** price source and is preferred for the "current price" reference.

---

## CryptoCompare REST

> **Source:** `src/prediction_sources.py`, class `CryptoCompareSource`
> **URL:** `https://min-api.cryptocompare.com/data/price?fsym=BTC&tsyms=USD`
> **Authentication:** Optional API key via `Apikey` header

### Request

```
GET /data/price?fsym=BTC&tsyms=USD
Authorization: Apikey {CRYPTOCOMPARE_API_KEY}
```

### Response

```json
{
  "USD": 65432.10
}
```

### Polling

- **Interval:** `REST_POLL_INTERVAL` (default: 1.0 seconds)
- **Timeout:** 5 seconds per request
- **Source name:** `"cryptocompare"`

### Authentication

Set `CRYPTOCOMPARE_API_KEY` in the environment. If empty, the header is omitted (free tier works without a key but has lower rate limits).

---

## CoinGecko REST

> **Source:** `src/prediction_sources.py`, class `CoinGeckoSource`
> **URL:** `https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd&include_24hr_change=true`
> **Authentication:** None required (free tier)

### Request

```
GET /api/v3/simple/price?ids=bitcoin&vs_currencies=usd&include_24hr_change=true
```

### Response

```json
{
  "bitcoin": {
    "usd": 65432.10,
    "usd_24h_change": 1.234
  }
}
```

### Polling

- **Interval:** `REST_POLL_INTERVAL * 2` (default: 2.0 seconds -- doubled to respect CoinGecko rate limits)
- **Timeout:** 5 seconds per request
- **Source name:** `"coingecko"`

---

## Error Handling and Retry Logic

### REST Retry Policy (Polymarket CLOB)

Defined in `PolymarketClient._request()`:

```python
for attempt in range(max_retries):  # default: 3 attempts
    try:
        # ... make request ...
        if resp.status == 429:
            await asyncio.sleep(Retry-After header or 1s)
            continue
        resp.raise_for_status()
        return parsed_response
    except (ClientError, TimeoutError):
        if attempt < max_retries - 1:
            backoff = retry_backoff_base_ms * (2 ** attempt) / 1000
            await asyncio.sleep(backoff)

raise ConnectionError("All retries exhausted")
```

**Retry backoff schedule** (with default `retry_backoff_base_ms=10`):

| Attempt | Backoff (ms) | Backoff (s) |
|---------|-------------|-------------|
| 1       | 10          | 0.01        |
| 2       | 20          | 0.02        |
| 3       | _(fail)_    | _(raises)_  |

### WebSocket Reconnection

Both Binance and Polymarket WebSocket connections use the same pattern:

```python
while True:
    try:
        async with session.ws_connect(url, heartbeat=N, receive_timeout=M) as ws:
            # process messages...
    except (ClientError, TimeoutError):
        await asyncio.sleep(1)  # wait 1 second, then reconnect
```

### Price Source Error Handling

All price sources (`BinanceSource`, `CryptoCompareSource`, `CoinGeckoSource`) catch exceptions broadly and log warnings without crashing:

```python
except Exception as exc:
    logger.warning("Source error: %s", exc)
await asyncio.sleep(poll_interval)
```

### Queue Back-Pressure

All queues in the pipeline use a drop-oldest strategy when full:

```python
try:
    queue.put_nowait(item)
except asyncio.QueueFull:
    queue.get_nowait()   # drop oldest
    queue.put_nowait(item)
```

### HTTP Connection Pool

The Polymarket client uses a shared `aiohttp.TCPConnector` with:

| Setting                | Value                             |
|------------------------|-----------------------------------|
| `limit`                | `HTTP_POOL_SIZE` (default: 20)    |
| `enable_cleanup_closed`| `True`                            |
| `ttl_dns_cache`        | `60` seconds                      |

### Timeouts

| Context                | Timeout    |
|------------------------|------------|
| Polymarket REST        | 5 seconds  |
| Gamma API discovery    | 15 seconds |
| CryptoCompare REST     | 5 seconds  |
| CoinGecko REST         | 5 seconds  |
| Binance WS receive     | 30 seconds |
| Polymarket WS receive  | 30 seconds |
| Order fill tracking    | 30 seconds |
