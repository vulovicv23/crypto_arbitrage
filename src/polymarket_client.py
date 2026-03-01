"""
Polymarket CLOB API client.

Provides:
  - REST methods for order placement / cancellation / book snapshots.
  - WebSocket pool for real-time book updates (auto-scales across
    multiple connections to handle the 500-token-per-connection limit).
  - HMAC signing compliant with Polymarket's auth scheme.

All public methods are async.  The client maintains a single aiohttp session
with a configurable connection pool for low-latency HTTP/1.1 keep-alive.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import time
from collections.abc import Awaitable, Callable

import aiohttp
import orjson

from config import ExecutionConfig, PolymarketConfig
from src.models import PolymarketBook, Side
from src.ws_pool import WebSocketPool

logger = logging.getLogger(__name__)


class PolymarketClient:
    """Async client for the Polymarket CLOB API.

    Combines:
      - REST: order placement, cancellation, book snapshots, positions.
      - WebSocket pool: scalable real-time book streaming with dynamic
        subscribe/unsubscribe and auto-reconnect.
    """

    def __init__(
        self,
        config: PolymarketConfig,
        exec_config: ExecutionConfig,
    ):
        self._cfg = config
        self._exec = exec_config
        self._session: aiohttp.ClientSession | None = None

        # Rate limiter: token bucket
        self._rate_tokens = float(exec_config.max_orders_per_second)
        self._rate_max = float(exec_config.max_orders_per_second)
        self._rate_last_refill = time.monotonic()

        # WebSocket pool for book streaming
        self._ws_pool = WebSocketPool(
            url=config.ws_url,
            max_tokens_per_connection=config.ws_max_tokens_per_connection,
            max_reconnect_delay=config.ws_max_reconnect_delay,
            initial_reconnect_delay=config.ws_initial_reconnect_delay,
        )

        # Condition ID mapping: token_id → condition_id
        # (set by the bot so WS updates carry the condition_id)
        self._token_condition_map: dict[str, str] = {}

    # ── lifecycle ─────────────────────────────────────────────────────

    async def connect(self) -> None:
        """Open the REST session and connect the WebSocket pool."""
        connector = aiohttp.TCPConnector(
            limit=self._exec.http_pool_size,
            enable_cleanup_closed=True,
            ttl_dns_cache=60,
        )
        self._session = aiohttp.ClientSession(
            connector=connector,
            json_serialize=lambda obj: orjson.dumps(obj).decode(),
        )
        await self._ws_pool.connect()
        logger.info(
            "Polymarket client connected (REST pool=%d, WS pool ready)",
            self._exec.http_pool_size,
        )

    async def close(self) -> None:
        """Disconnect the WebSocket pool and close the REST session."""
        await self._ws_pool.disconnect()
        if self._session and not self._session.closed:
            await self._session.close()
        logger.info("Polymarket client closed")

    # ── auth ──────────────────────────────────────────────────────────

    def _sign(self, method: str, path: str, body: str = "") -> dict[str, str]:
        """Generate HMAC-SHA256 auth headers for the CLOB API."""
        timestamp = str(int(time.time()))
        message = timestamp + method.upper() + path + body
        signature = hmac.new(
            self._cfg.api_secret.encode(),
            message.encode(),
            hashlib.sha256,
        ).hexdigest()
        return {
            "POLY-API-KEY": self._cfg.api_key,
            "POLY-SIGNATURE": signature,
            "POLY-TIMESTAMP": timestamp,
            "POLY-PASSPHRASE": self._cfg.api_passphrase,
        }

    # ── rate limiter ──────────────────────────────────────────────────

    async def _acquire_rate_token(self) -> None:
        while True:
            now = time.monotonic()
            elapsed = now - self._rate_last_refill
            self._rate_tokens = min(
                self._rate_max,
                self._rate_tokens + elapsed * self._rate_max,
            )
            self._rate_last_refill = now
            if self._rate_tokens >= 1.0:
                self._rate_tokens -= 1.0
                return
            await asyncio.sleep(0.001)

    # ── REST helpers ──────────────────────────────────────────────────

    async def _request(
        self,
        method: str,
        path: str,
        json_body: dict | None = None,
        retry: bool = True,
    ) -> dict:
        assert self._session is not None, "Client not connected"

        body_str = orjson.dumps(json_body).decode() if json_body else ""
        headers = self._sign(method, path, body_str)
        headers["Content-Type"] = "application/json"

        url = self._cfg.rest_url + path

        for attempt in range(self._exec.max_retries if retry else 1):
            await self._acquire_rate_token()
            try:
                async with self._session.request(
                    method,
                    url,
                    headers=headers,
                    data=body_str.encode() if body_str else None,
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    if resp.status == 429:
                        wait = float(resp.headers.get("Retry-After", "1"))
                        logger.warning("Rate-limited — waiting %.1fs", wait)
                        await asyncio.sleep(wait)
                        continue
                    resp.raise_for_status()
                    raw = await resp.read()
                    return orjson.loads(raw) if raw else {}
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                logger.warning(
                    "Request %s %s attempt %d failed: %s",
                    method,
                    path,
                    attempt + 1,
                    exc,
                )
                if attempt < self._exec.max_retries - 1:
                    backoff = self._exec.retry_backoff_base_ms * (2**attempt) / 1000
                    await asyncio.sleep(backoff)

        raise ConnectionError(f"All retries exhausted for {method} {path}")

    # ── public REST endpoints ─────────────────────────────────────────

    async def get_order_book(self, token_id: str) -> PolymarketBook:
        """Fetch a full order-book snapshot for one token.

        NOTE: The CLOB REST API returns bids in ascending order (lowest first)
        and asks in descending order (highest first). We must use max(bids)
        and min(asks) to find the best prices, NOT bids[0]/asks[0].
        """
        data = await self._request("GET", f"/book?token_id={token_id}")
        bids = data.get("bids", [])
        asks = data.get("asks", [])
        best_bid = max((float(b["price"]) for b in bids), default=0.0)
        best_ask = min((float(a["price"]) for a in asks), default=0.0) if asks else 0.0
        mid = (best_bid + best_ask) / 2 if best_bid and best_ask else 0.0
        spread = best_ask - best_bid if best_bid and best_ask else 0.0
        return PolymarketBook(
            condition_id=self._token_condition_map.get(token_id, ""),
            token_id=token_id,
            best_bid=best_bid,
            best_ask=best_ask,
            mid_price=mid,
            spread=spread,
        )

    async def get_markets(self) -> list[dict]:
        """List available markets (for discovering BTC condition IDs)."""
        return await self._request("GET", "/markets")

    async def place_order(
        self,
        token_id: str,
        side: Side,
        price: float,
        size: float,
    ) -> dict:
        """Place a limit order on the CLOB.

        Returns the exchange response with order ID.
        """
        payload = {
            "tokenID": token_id,
            "side": side.value,
            "price": str(round(price, 4)),
            "size": str(round(size, 2)),
            "type": "GTC",  # Good-til-cancelled
        }
        logger.debug("Placing order: %s", payload)
        result = await self._request("POST", "/order", payload)
        logger.info(
            "Order placed: id=%s side=%s price=%s size=%s",
            result.get("orderID", "?"),
            side.value,
            price,
            size,
        )
        return result

    async def cancel_order(self, order_id: str) -> dict:
        """Cancel an open order."""
        return await self._request("DELETE", f"/order/{order_id}")

    async def cancel_all(self) -> dict:
        """Cancel all open orders."""
        return await self._request("DELETE", "/order/all")

    async def get_open_orders(self) -> list[dict]:
        """Fetch current open orders."""
        return await self._request("GET", "/orders?open=true")

    async def get_positions(self) -> list[dict]:
        """Fetch current positions (balances)."""
        return await self._request("GET", "/positions")

    # ── WebSocket book streaming (via pool) ────────────────────────────

    def set_token_condition_map(self, mapping: dict[str, str]) -> None:
        """Update the token_id → condition_id mapping.

        Called by the bot when market discovery updates active markets,
        so that WS book updates carry the correct ``condition_id``.
        """
        self._token_condition_map = dict(mapping)

    async def subscribe_books(
        self,
        token_ids: list[str],
        callback: Callable[[PolymarketBook], Awaitable[None]],
    ) -> None:
        """Subscribe to real-time order-book updates for the given tokens.

        Uses the WebSocket pool to scale across multiple connections
        (500 tokens per connection).  Subscriptions are additive — call
        this multiple times as new markets are discovered.

        Args:
            token_ids: Token IDs to subscribe to.
            callback: ``async def handler(book: PolymarketBook) -> None``
        """

        # Wrap the user-facing callback to parse raw WS messages
        async def _on_ws_message(asset_id: str, raw_msg: dict) -> None:
            book = self._parse_book_message(asset_id, raw_msg)
            if book is not None:
                try:
                    await callback(book)
                except Exception:
                    logger.exception("Book callback error for %s", asset_id[:12])

        await self._ws_pool.subscribe(token_ids, _on_ws_message)

    async def unsubscribe_books(self, token_ids: list[str]) -> None:
        """Unsubscribe from order-book updates for the given tokens.

        Called when markets expire and we no longer need updates.
        Empty WS connections are cleaned up automatically by the pool.
        """
        await self._ws_pool.unsubscribe(token_ids)

        # Clean up condition map entries
        for tid in token_ids:
            self._token_condition_map.pop(tid, None)

    def _parse_book_message(
        self, asset_id: str, raw_msg: dict
    ) -> PolymarketBook | None:
        """Parse a raw WebSocket message into a PolymarketBook.

        Returns None for non-book messages (e.g. heartbeats, trades).
        """
        # Polymarket sends various event types — we only want book data
        event_type = raw_msg.get("event_type", raw_msg.get("type", ""))
        if event_type and event_type not in ("book", "book_update", "book_snapshot"):
            return None

        bids = raw_msg.get("bids", [])
        asks = raw_msg.get("asks", [])

        if not bids and not asks:
            return None

        # Bids/asks come as [[price, size], ...] or [{"price": ..., "size": ...}, ...]
        # Best bid = highest bid, best ask = lowest ask
        best_bid = self._extract_best_bid(bids)
        best_ask = self._extract_best_ask(asks)
        mid = (best_bid + best_ask) / 2 if best_bid and best_ask else 0.0
        spread = best_ask - best_bid if best_bid and best_ask else 0.0

        return PolymarketBook(
            condition_id=self._token_condition_map.get(asset_id, ""),
            token_id=asset_id,
            best_bid=best_bid,
            best_ask=best_ask,
            mid_price=mid,
            spread=spread,
        )

    @staticmethod
    def _extract_price(level: dict | list | tuple | float) -> float:
        """Extract price from a single book level entry."""
        if isinstance(level, dict):
            return float(level.get("price", 0))
        if isinstance(level, (list, tuple)):
            return float(level[0])
        return float(level)

    @classmethod
    def _extract_best_bid(cls, levels: list) -> float:
        """Extract the best (highest) bid price from book levels.

        Handles both list format [[price, size], ...] and dict format
        [{"price": ..., "size": ...}, ...].

        NOTE: Polymarket may return levels in any order (REST returns
        ascending, WS may vary). Always use max() for safety.
        """
        if not levels:
            return 0.0
        return max(cls._extract_price(lvl) for lvl in levels)

    @classmethod
    def _extract_best_ask(cls, levels: list) -> float:
        """Extract the best (lowest) ask price from book levels.

        NOTE: Polymarket may return levels in any order. Always use
        min() for safety.
        """
        if not levels:
            return 0.0
        return min(cls._extract_price(lvl) for lvl in levels)

    # ── pool stats ─────────────────────────────────────────────────────

    def ws_stats(self) -> dict:
        """WebSocket pool statistics for health monitoring."""
        return self._ws_pool.stats()
