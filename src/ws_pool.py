"""
WebSocket connection pool for Polymarket CLOB order book streaming.

Manages multiple ``WebSocketClient`` instances to work around the
500-instrument-per-connection limit enforced by Polymarket.  Tokens are
distributed across connections using a fill-first strategy: the first
client with spare capacity receives new subscriptions, and a new client
is created only when all existing clients are full.

Exposes the same interface as ``WebSocketClient`` (connect, disconnect,
subscribe, unsubscribe) so it is a drop-in replacement.

Dynamic lifecycle:
  - ``subscribe()`` creates new inner clients on demand.
  - ``unsubscribe()`` tears down empty clients automatically.
  - On pool ``connect()`` / ``disconnect()`` all inner clients follow suit.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from src.ws_client import WebSocketClient

logger = logging.getLogger(__name__)

# Polymarket enforces a limit on tokens per WebSocket connection.
DEFAULT_MAX_TOKENS_PER_CONNECTION = 500

BookCallback = Callable[[str, dict[str, Any]], Awaitable[None]]


class WebSocketPool:
    """Pool of ``WebSocketClient`` instances, each limited to N tokens.

    Automatically partitions token subscriptions across multiple
    connections and manages the lifecycle of inner clients.

    Parameters
    ----------
    url : str
        WebSocket URL.
    max_tokens_per_connection : int
        Upper limit on tokens per single WS connection (default: 500).
    max_reconnect_delay : float
        Passed to each inner ``WebSocketClient``.
    initial_reconnect_delay : float
        Passed to each inner ``WebSocketClient``.
    backoff_factor : float
        Passed to each inner ``WebSocketClient``.
    """

    def __init__(
        self,
        url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market",
        max_tokens_per_connection: int = DEFAULT_MAX_TOKENS_PER_CONNECTION,
        max_reconnect_delay: float = 30.0,
        initial_reconnect_delay: float = 2.0,
        backoff_factor: float = 2.0,
    ) -> None:
        self._url = url
        self._max_tokens = max_tokens_per_connection
        self._max_reconnect_delay = max_reconnect_delay
        self._initial_reconnect_delay = initial_reconnect_delay
        self._backoff_factor = backoff_factor

        self._clients: list[WebSocketClient] = []
        self._token_to_client: dict[str, WebSocketClient] = {}
        self._is_pool_connected: bool = False

    # ── public interface (mirrors WebSocketClient) ─────────────────────

    async def connect(self) -> None:
        """Mark pool as connected and connect all existing inner clients."""
        self._is_pool_connected = True
        for client in self._clients:
            await client.connect()
        logger.info("WS pool connected (%d clients)", len(self._clients))

    async def disconnect(self) -> None:
        """Disconnect all inner clients and reset pool state."""
        self._is_pool_connected = False
        for client in self._clients:
            await client.disconnect()
        logger.info("WS pool disconnected (%d clients)", len(self._clients))

    async def subscribe(
        self,
        token_ids: list[str],
        callback: BookCallback,
    ) -> None:
        """Subscribe to order book updates, distributing tokens across clients.

        New tokens are assigned to the first client with spare capacity.
        If all clients are full a new ``WebSocketClient`` is created (and
        auto-connected if the pool is in connected state).

        Args:
            token_ids: Token IDs to subscribe to.
            callback: ``async def handler(token_id, raw_msg_dict)``
        """
        # Filter out tokens we're already subscribed to
        new_tokens = [t for t in token_ids if t not in self._token_to_client]
        if not new_tokens:
            return

        remaining = list(new_tokens)

        while remaining:
            client = self._find_client_with_capacity()
            if client is None:
                client = await self._create_client()

            capacity = self._max_tokens - self._client_subscription_count(client)
            batch = remaining[:capacity]
            remaining = remaining[capacity:]

            await client.subscribe(batch, callback)
            for tid in batch:
                self._token_to_client[tid] = client

        logger.info(
            "WS pool subscribed: +%d new tokens, %d total across %d clients",
            len(new_tokens),
            len(self._token_to_client),
            len(self._clients),
        )

    async def unsubscribe(self, token_ids: list[str]) -> None:
        """Unsubscribe from order book updates, routing to correct clients.

        Empty clients are disconnected and removed from the pool.

        Args:
            token_ids: Token IDs to unsubscribe from.
        """
        # Group by owning client
        client_batches: dict[int, list[str]] = {}
        for tid in token_ids:
            client = self._token_to_client.pop(tid, None)
            if client is None:
                continue
            cid = id(client)
            if cid not in client_batches:
                client_batches[cid] = []
            client_batches[cid].append(tid)

        # Unsubscribe per client and tear down empty ones
        for client in list(self._clients):
            cid = id(client)
            batch = client_batches.get(cid)
            if batch is None:
                continue

            await client.unsubscribe(batch)

            # If client has no remaining subscriptions, remove it
            if client.subscription_count == 0:
                await client.disconnect()
                self._clients.remove(client)
                logger.info(
                    "WS pool removed empty client (%d remaining)",
                    len(self._clients),
                )

        logger.info(
            "WS pool unsubscribed: -%d tokens, %d total across %d clients",
            len(token_ids),
            len(self._token_to_client),
            len(self._clients),
        )

    # ── properties ─────────────────────────────────────────────────────

    @property
    def is_connected(self) -> bool:
        """True if any inner client is connected."""
        return any(c.is_connected for c in self._clients)

    @property
    def active_subscriptions(self) -> set[str]:
        """Union of all subscribed token IDs across all clients."""
        return set(self._token_to_client.keys())

    @property
    def client_count(self) -> int:
        return len(self._clients)

    @property
    def total_subscriptions(self) -> int:
        return len(self._token_to_client)

    # ── stats ──────────────────────────────────────────────────────────

    def stats(self) -> dict:
        """Pool-level statistics."""
        client_stats = [c.stats() for c in self._clients]
        total_messages = sum(s["total_messages"] for s in client_stats)
        total_reconnects = sum(s["reconnects"] for s in client_stats)
        return {
            "clients": len(self._clients),
            "total_subscriptions": len(self._token_to_client),
            "total_messages": total_messages,
            "total_reconnects": total_reconnects,
            "per_client": [
                {
                    "subscriptions": s["subscriptions"],
                    "connected": s["connected"],
                    "messages": s["total_messages"],
                }
                for s in client_stats
            ],
        }

    # ── internal helpers ───────────────────────────────────────────────

    def _client_subscription_count(self, client: WebSocketClient) -> int:
        """Count how many tokens in the mapping point to this client."""
        return sum(1 for c in self._token_to_client.values() if c is client)

    def _find_client_with_capacity(self) -> WebSocketClient | None:
        """Return the first client with room for more tokens, or None."""
        for client in self._clients:
            if self._client_subscription_count(client) < self._max_tokens:
                return client
        return None

    async def _create_client(self) -> WebSocketClient:
        """Spin up a new inner WebSocketClient and optionally connect it."""
        client = WebSocketClient(
            url=self._url,
            max_reconnect_delay=self._max_reconnect_delay,
            initial_reconnect_delay=self._initial_reconnect_delay,
            backoff_factor=self._backoff_factor,
        )
        self._clients.append(client)

        if self._is_pool_connected:
            await client.connect()

        logger.info(
            "WS pool created client #%d (%d total)",
            len(self._clients) - 1,
            len(self._clients),
        )
        return client
