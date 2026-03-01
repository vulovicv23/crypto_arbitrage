"""
WebSocket client for Polymarket CLOB order book streaming.

Connects to ``wss://ws-subscriptions-clob.polymarket.com/ws/market``,
subscribes to token channels, auto-reconnects on disconnect, and routes
updates to registered async callbacks.

Key features:
  - Auto-reconnect with exponential backoff (2 s → 30 s).
  - On reconnect, re-subscribes all active tokens automatically.
  - Supports batch messages (Polymarket may send JSON arrays).
  - Per-token callback routing.
  - Dynamic subscribe / unsubscribe without restarting.

Uses ``aiohttp`` (already a project dependency) — no extra packages needed.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp
import orjson

logger = logging.getLogger(__name__)

# Callback type: async def handler(token_id: str, raw_msg: dict) -> None
BookCallback = Callable[[str, dict[str, Any]], Awaitable[None]]


class WebSocketClient:
    """Single Polymarket CLOB WebSocket connection with auto-reconnect.

    Parameters
    ----------
    url : str
        Full WebSocket URL (default: Polymarket production endpoint).
    max_reconnect_delay : float
        Upper bound on reconnect backoff (seconds).
    initial_reconnect_delay : float
        First reconnect delay (seconds).
    backoff_factor : float
        Multiplier per consecutive failure.
    """

    def __init__(
        self,
        url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market",
        max_reconnect_delay: float = 30.0,
        initial_reconnect_delay: float = 2.0,
        backoff_factor: float = 2.0,
    ) -> None:
        self._url = url
        self._max_reconnect_delay = max_reconnect_delay
        self._initial_reconnect_delay = initial_reconnect_delay
        self._backoff_factor = backoff_factor

        # token_id → async callback
        self._subscriptions: dict[str, BookCallback] = {}

        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._should_stop = False
        self._listen_task: asyncio.Task | None = None

        # Stats
        self._total_messages = 0
        self._reconnects = 0

    # ── lifecycle ──────────────────────────────────────────────────────

    async def connect(self) -> None:
        """Start the WebSocket connection loop in the background."""
        self._should_stop = False
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30),
        )
        self._listen_task = asyncio.create_task(
            self._connection_loop(), name="ws-client"
        )

    async def disconnect(self) -> None:
        """Gracefully close the WebSocket and stop reconnecting."""
        self._should_stop = True
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()
            self._ws = None
        if self._listen_task is not None:
            self._listen_task.cancel()
            try:
                await self._listen_task
            except asyncio.CancelledError:
                pass
            self._listen_task = None
        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None
        logger.info("WS client disconnected gracefully")

    # ── subscribe / unsubscribe ────────────────────────────────────────

    async def subscribe(
        self,
        token_ids: list[str],
        callback: BookCallback,
    ) -> None:
        """Subscribe to order book updates for the given token IDs.

        If the connection is already live, sends the subscribe message
        immediately.  Otherwise, subscriptions are recorded and will be
        sent on the next (re)connect.

        Args:
            token_ids: Token IDs to subscribe to.
            callback: ``async def handler(token_id, raw_msg_dict)``
        """
        for tid in token_ids:
            self._subscriptions[tid] = callback

        if self._ws is not None and not self._ws.closed:
            await self._send_subscribe(token_ids)

        logger.debug(
            "WS subscribed to %d tokens (total=%d)",
            len(token_ids),
            len(self._subscriptions),
        )

    async def unsubscribe(self, token_ids: list[str]) -> None:
        """Unsubscribe from order book updates for the given token IDs."""
        for tid in token_ids:
            self._subscriptions.pop(tid, None)

        if self._ws is not None and not self._ws.closed:
            await self._send_unsubscribe(token_ids)

        logger.debug(
            "WS unsubscribed %d tokens (total=%d)",
            len(token_ids),
            len(self._subscriptions),
        )

    # ── properties ─────────────────────────────────────────────────────

    @property
    def is_connected(self) -> bool:
        return self._ws is not None and not self._ws.closed

    @property
    def active_subscriptions(self) -> set[str]:
        return set(self._subscriptions.keys())

    @property
    def subscription_count(self) -> int:
        return len(self._subscriptions)

    # ── connection loop ────────────────────────────────────────────────

    async def _connection_loop(self) -> None:
        """Main loop: connect → listen → reconnect with backoff."""
        delay = self._initial_reconnect_delay

        while not self._should_stop:
            try:
                assert self._session is not None
                self._ws = await self._session.ws_connect(
                    self._url,
                    heartbeat=15,
                    receive_timeout=60,
                )
                delay = self._initial_reconnect_delay  # reset on success
                logger.info("WS connected to %s", self._url)

                # Re-subscribe all active tokens on (re)connect
                if self._subscriptions:
                    await self._send_subscribe(list(self._subscriptions.keys()))
                    logger.info("WS re-subscribed %d tokens", len(self._subscriptions))

                await self._listen()

            except asyncio.CancelledError:
                self._ws = None
                break

            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                self._ws = None
                if self._should_stop:
                    break
                self._reconnects += 1
                logger.warning(
                    "WS disconnected (%s) — reconnecting in %.1fs",
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)
                delay = min(delay * self._backoff_factor, self._max_reconnect_delay)

            except Exception as exc:
                self._ws = None
                if self._should_stop:
                    break
                self._reconnects += 1
                logger.error(
                    "WS unexpected error (%s) — reconnecting in %.1fs",
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)
                delay = min(delay * self._backoff_factor, self._max_reconnect_delay)

    async def _listen(self) -> None:
        """Read messages from the WebSocket and route to callbacks."""
        assert self._ws is not None

        async for msg in self._ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                await self._handle_raw(msg.data)
            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                logger.warning("WS connection closed/errored: %s", msg.type)
                break

    async def _handle_raw(self, raw: str) -> None:
        """Parse one raw WS message (may be a single object or an array)."""
        try:
            data = orjson.loads(raw)
        except Exception:
            return

        # Polymarket may batch-send as JSON array
        messages: list[dict] = data if isinstance(data, list) else [data]

        for msg in messages:
            if not isinstance(msg, dict):
                continue

            asset_id = msg.get("asset_id")
            if asset_id is None:
                continue

            self._total_messages += 1

            callback = self._subscriptions.get(asset_id)
            if callback is not None:
                try:
                    await callback(asset_id, msg)
                except Exception:
                    logger.exception("WS callback error for %s", asset_id[:12])

    # ── protocol messages ──────────────────────────────────────────────

    async def _send_subscribe(self, token_ids: list[str]) -> None:
        if self._ws is None or self._ws.closed:
            return
        payload = orjson.dumps(
            {
                "type": "subscribe",
                "channel": "book",
                "assets_ids": token_ids,
            }
        ).decode()
        await self._ws.send_str(payload)

    async def _send_unsubscribe(self, token_ids: list[str]) -> None:
        if self._ws is None or self._ws.closed:
            return
        payload = orjson.dumps(
            {
                "type": "unsubscribe",
                "channel": "book",
                "assets_ids": token_ids,
            }
        ).decode()
        await self._ws.send_str(payload)

    # ── stats ──────────────────────────────────────────────────────────

    def stats(self) -> dict:
        return {
            "connected": self.is_connected,
            "subscriptions": len(self._subscriptions),
            "total_messages": self._total_messages,
            "reconnects": self._reconnects,
        }
