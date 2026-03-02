"""
Prediction source integrations.

Each source produces PriceTick / Prediction objects that feed the strategy.
Sources run as independent async tasks and push into a shared asyncio.Queue.

Architecture:
  ┌──────────────┐
  │ BinanceWS    │──┐
  ├──────────────┤  │
  │ CryptoCompWS │──┤──▶  price_queue (asyncio.Queue[PriceTick])
  ├──────────────┤  │
  │ CoinGeckoRE  │──┘
  └──────────────┘

  The PredictionAggregator consumes ticks, maintains a rolling window,
  and emits Prediction objects into a prediction_queue.

CryptoCompare/CoinDesk Data Streamer:
  Streams CCCAGG (volume-weighted aggregate index across all exchanges)
  via WebSocket for near-instant price updates.  Falls back to REST
  polling when no API key is configured.
"""

from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from collections import deque

import aiohttp
import numpy as np
import orjson

from src.models import Prediction, PriceTick

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------


class PriceSource(ABC):
    """Base class for all price feed sources."""

    def __init__(self, name: str, queue: asyncio.Queue[PriceTick]):
        self.name = name
        self._queue = queue
        self._running = False

    async def start(self) -> None:
        self._running = True
        logger.info("Starting price source: %s", self.name)
        backoff = 1.0
        max_backoff = 60.0
        while self._running:
            try:
                await self._run()
                break  # Clean exit (shouldn't happen, but handle gracefully)
            except asyncio.CancelledError:
                logger.info("Source %s cancelled", self.name)
                break
            except Exception:
                logger.exception(
                    "Source %s crashed — restarting in %.0fs", self.name, backoff
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)
        self._running = False

    def stop(self) -> None:
        self._running = False

    @abstractmethod
    async def _run(self) -> None: ...

    async def _emit(self, tick: PriceTick) -> None:
        try:
            self._queue.put_nowait(tick)
        except asyncio.QueueFull:
            # Drop oldest if queue is full (back-pressure).
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            self._queue.put_nowait(tick)


# ---------------------------------------------------------------------------
# Binance WebSocket (real-time BTC/USDT trades)
# ---------------------------------------------------------------------------


class BinanceSource(PriceSource):
    """
    Streams individual trades from Binance via WebSocket.
    This is the lowest-latency price source.
    """

    def __init__(
        self,
        queue: asyncio.Queue[PriceTick],
        ws_url: str = "wss://stream.binance.com:9443/ws/btcusdt@trade",
    ):
        super().__init__("binance", queue)
        self._ws_url = ws_url

    async def _run(self) -> None:
        async with aiohttp.ClientSession() as session:
            while self._running:
                try:
                    async with session.ws_connect(
                        self._ws_url,
                        heartbeat=20,
                        receive_timeout=30,
                    ) as ws:
                        logger.info("Binance WS connected")
                        async for msg in ws:
                            if not self._running:
                                break
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                data = orjson.loads(msg.data)
                                tick = PriceTick(
                                    source="binance",
                                    price=float(data["p"]),
                                    volume=float(data["q"]),
                                    timestamp_ns=int(data["T"]) * 1_000_000,
                                )
                                await self._emit(tick)
                            elif msg.type in (
                                aiohttp.WSMsgType.CLOSED,
                                aiohttp.WSMsgType.ERROR,
                            ):
                                break
                except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                    logger.warning("Binance WS error: %s — reconnecting in 1s", exc)
                    await asyncio.sleep(1)


# ---------------------------------------------------------------------------
# CryptoCompare REST (polling)
# ---------------------------------------------------------------------------


class CryptoCompareSource(PriceSource):
    """Polls CryptoCompare REST API for BTC/USD price."""

    def __init__(
        self,
        queue: asyncio.Queue[PriceTick],
        api_key: str,
        url: str = "https://min-api.cryptocompare.com/data/price?fsym=BTC&tsyms=USD",
        poll_interval: float = 1.0,
    ):
        super().__init__("cryptocompare", queue)
        self._url = url
        self._api_key = api_key
        self._poll_interval = poll_interval

    async def _run(self) -> None:
        headers = {}
        if self._api_key:
            headers["authorization"] = f"Apikey {self._api_key}"
        async with aiohttp.ClientSession(headers=headers) as session:
            while self._running:
                try:
                    async with session.get(
                        self._url, timeout=aiohttp.ClientTimeout(total=5)
                    ) as resp:
                        data = await resp.json()
                        price = float(data.get("USD", 0))
                        if price > 0:
                            await self._emit(
                                PriceTick(source="cryptocompare", price=price)
                            )
                except Exception as exc:
                    logger.warning("CryptoCompare error: %s", exc)
                await asyncio.sleep(self._poll_interval)


# ---------------------------------------------------------------------------
# CryptoCompare WebSocket (CoinDesk Data Streamer — CCCAGG)
# ---------------------------------------------------------------------------


class CryptoCompareWSSource(PriceSource):
    """Streams CCCAGG aggregate index from CoinDesk Data Streamer via WebSocket.

    CCCAGG is a volume-weighted average BTC price across all major exchanges
    (Coinbase, Binance, Kraken, etc.), providing a "consensus" price that
    serves as a cross-source validator alongside the Binance raw trade feed.

    Message types:
      - TYPE 5 (CCCAGG): aggregate price updates — the primary feed.
      - TYPE 0 (raw trade): individual exchange trades (optional).
      - TYPE 999: heartbeat (every ~30s).

    Auto-reconnects with exponential backoff on disconnection.
    """

    def __init__(
        self,
        queue: asyncio.Queue[PriceTick],
        api_key: str,
        ws_url: str = "wss://data-streamer.cryptocompare.com/v2",
        subscribe_cccagg: bool = True,
        subscribe_raw_exchanges: list[str] | None = None,
    ):
        super().__init__("cryptocompare", queue)
        self._api_key = api_key
        self._ws_url = ws_url
        self._subscribe_cccagg = subscribe_cccagg
        # Optional: also subscribe to raw trades from specific exchanges
        self._raw_exchanges = subscribe_raw_exchanges or []
        self._reconnect_delay = 1.0
        self._max_reconnect_delay = 30.0
        self._last_heartbeat_ns: int = 0
        self._message_count: int = 0

    async def _run(self) -> None:
        async with aiohttp.ClientSession() as session:
            while self._running:
                try:
                    await self._connect_and_stream(session)
                except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                    logger.warning(
                        "CryptoCompare WS error: %s — reconnecting in %.1fs",
                        exc,
                        self._reconnect_delay,
                    )
                except Exception as exc:
                    logger.warning(
                        "CryptoCompare WS unexpected error: %s — reconnecting in %.1fs",
                        exc,
                        self._reconnect_delay,
                    )

                if self._running:
                    await asyncio.sleep(self._reconnect_delay)
                    # Exponential backoff, capped
                    self._reconnect_delay = min(
                        self._reconnect_delay * 2,
                        self._max_reconnect_delay,
                    )

    async def _connect_and_stream(self, session: aiohttp.ClientSession) -> None:
        """Open WS connection, subscribe, and process messages."""
        # Auth via URL query parameter (CoinDesk Data Streamer convention)
        url = f"{self._ws_url}?api_key={self._api_key}"

        async with session.ws_connect(
            url,
            heartbeat=25,  # aiohttp-level ping to detect dead connections
            receive_timeout=60,  # Expect at least a heartbeat every 30s
        ) as ws:
            logger.info("CryptoCompare WS connected to %s", self._ws_url)
            self._reconnect_delay = 1.0  # Reset backoff on successful connect

            # Build subscription list
            subs = self._build_subscription_list()
            if subs:
                sub_msg = {"action": "SubAdd", "subs": subs}
                await ws.send_json(sub_msg)
                logger.info(
                    "CryptoCompare WS subscribed: %s",
                    subs,
                )

            # Process incoming messages
            async for msg in ws:
                if not self._running:
                    break
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await self._handle_message(msg.data)
                elif msg.type in (
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.ERROR,
                ):
                    logger.warning("CryptoCompare WS connection closed/error")
                    break

    def _build_subscription_list(self) -> list[str]:
        """Build the subscription string list for the Data Streamer.

        Format: ``{type}~{exchange}~{from}~{to}``
          - Type 5 = CCCAGG aggregate index
          - Type 0 = raw trades from a specific exchange
        """
        subs: list[str] = []
        if self._subscribe_cccagg:
            subs.append("5~CCCAGG~BTC~USD")
        for exchange in self._raw_exchanges:
            subs.append(f"0~{exchange}~BTC~USD")
        return subs

    async def _handle_message(self, raw: str) -> None:
        """Parse a Data Streamer JSON message and emit PriceTick if applicable."""
        try:
            data = orjson.loads(raw)
        except (orjson.JSONDecodeError, ValueError):
            return

        msg_type = data.get("TYPE")

        # Heartbeat (TYPE 999) — just track it
        if msg_type == "999" or msg_type == 999:
            self._last_heartbeat_ns = time.time_ns()
            return

        # We want TYPE 5 (CCCAGG) or TYPE 0 (raw trade)
        if msg_type not in ("0", "5", 0, 5):
            return

        price = data.get("PRICE")
        if price is None or price <= 0:
            return

        # Extract metadata
        market = data.get("MARKET", "CCCAGG")
        volume = data.get("QUANTITY", data.get("LASTVOLUME", 0.0))

        # Use the exchange timestamp if available, otherwise local time
        exchange_ts = data.get("LASTUPDATE", data.get("TIMESTAMP"))
        if exchange_ts:
            timestamp_ns = int(exchange_ts) * 1_000_000_000
        else:
            timestamp_ns = time.time_ns()

        # Source name distinguishes CCCAGG from raw exchange trades
        source = "cryptocompare"
        if str(msg_type) == "0" and market != "CCCAGG":
            source = f"cryptocompare_{market.lower()}"

        tick = PriceTick(
            source=source,
            price=float(price),
            volume=float(volume) if volume else 0.0,
            timestamp_ns=timestamp_ns,
        )
        await self._emit(tick)
        self._message_count += 1

        if self._message_count == 1:
            logger.info(
                "CryptoCompare WS first tick: $%.2f from %s",
                tick.price,
                market,
            )
        elif self._message_count % 1000 == 0:
            logger.debug(
                "CryptoCompare WS: %d messages processed",
                self._message_count,
            )

    def stats(self) -> dict:
        """Source-level stats for health monitoring."""
        return {
            "source": self.name,
            "type": "websocket",
            "messages": self._message_count,
            "last_heartbeat_ns": self._last_heartbeat_ns,
            "running": self._running,
        }


# ---------------------------------------------------------------------------
# CoinGecko REST (polling, free tier)
# ---------------------------------------------------------------------------


class CoinGeckoSource(PriceSource):
    """Polls CoinGecko free API for BTC/USD price."""

    def __init__(
        self,
        queue: asyncio.Queue[PriceTick],
        url: str = (
            "https://api.coingecko.com/api/v3/simple/price"
            "?ids=bitcoin&vs_currencies=usd&include_24hr_change=true"
        ),
        poll_interval: float = 2.0,
    ):
        super().__init__("coingecko", queue)
        self._url = url
        self._poll_interval = poll_interval

    async def _run(self) -> None:
        async with aiohttp.ClientSession() as session:
            while self._running:
                try:
                    async with session.get(
                        self._url, timeout=aiohttp.ClientTimeout(total=5)
                    ) as resp:
                        data = await resp.json()
                        price = float(data["bitcoin"]["usd"])
                        if price > 0:
                            await self._emit(PriceTick(source="coingecko", price=price))
                except Exception as exc:
                    logger.warning("CoinGecko error: %s", exc)
                await asyncio.sleep(self._poll_interval)


# ---------------------------------------------------------------------------
# Prediction Aggregator
# ---------------------------------------------------------------------------


class PredictionAggregator:
    """
    Consumes PriceTick objects from multiple sources, maintains a rolling
    price window, and produces 15-minute Prediction objects using a
    simple momentum-based model.

    Prediction logic:
      1.  Compute short-term momentum (Δ over last N ticks).
      2.  Extrapolate linearly over the prediction horizon.
      3.  Blend across sources by inverse-latency weighting.
      4.  Assign a confidence score based on source agreement.
    """

    def __init__(
        self,
        price_queue: asyncio.Queue[PriceTick],
        prediction_queue: asyncio.Queue[Prediction],
        horizon_s: int = 900,
        window_size: int = 300,
        emit_interval: float = 0.25,
    ):
        self._price_queue = price_queue
        self._prediction_queue = prediction_queue
        self._horizon_s = horizon_s
        self._window_size = window_size
        self._emit_interval = emit_interval

        # Per-source rolling windows: source_name → deque of (timestamp_ns, price)
        self._windows: dict[str, deque[tuple[int, float]]] = {}
        self._latest: dict[str, float] = {}

    async def start(self) -> None:
        """Run two concurrent loops: ingest ticks and emit predictions."""
        await asyncio.gather(
            self._ingest_loop(),
            self._emit_loop(),
        )

    # ── ingest ───────────────────────────────────────────────────────
    async def _ingest_loop(self) -> None:
        while True:
            tick = await self._price_queue.get()
            if tick.source not in self._windows:
                self._windows[tick.source] = deque(maxlen=self._window_size)
            self._windows[tick.source].append((tick.timestamp_ns, tick.price))
            self._latest[tick.source] = tick.price

    # ── emit ─────────────────────────────────────────────────────────
    async def _emit_loop(self) -> None:
        while True:
            await asyncio.sleep(self._emit_interval)
            pred = self._generate_prediction()
            if pred is not None:
                try:
                    self._prediction_queue.put_nowait(pred)
                except asyncio.QueueFull:
                    try:
                        self._prediction_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                    self._prediction_queue.put_nowait(pred)

    # ── prediction model ─────────────────────────────────────────────
    def _generate_prediction(self) -> Prediction | None:
        if not self._latest:
            return None

        source_predictions: list[tuple[float, float]] = []  # (predicted, confidence)

        for source, window in self._windows.items():
            if len(window) < 10:
                continue
            pred, conf = self._extrapolate(window)
            if pred is not None:
                source_predictions.append((pred, conf))

        if not source_predictions:
            return None

        # Confidence-weighted blend
        total_weight = sum(c for _, c in source_predictions)
        if total_weight <= 0:
            return None

        blended_price = sum(p * c for p, c in source_predictions) / total_weight
        avg_confidence = total_weight / len(source_predictions)

        # Cross-source agreement boosts confidence
        if len(source_predictions) >= 2:
            prices_arr = np.array([p for p, _ in source_predictions])
            cv = (
                np.std(prices_arr) / np.mean(prices_arr)
                if np.mean(prices_arr) != 0
                else 1.0
            )
            agreement_factor = max(0.0, 1.0 - cv * 100)  # penalize divergence
            avg_confidence *= agreement_factor

        avg_confidence = min(max(avg_confidence, 0.0), 1.0)

        # Use best source's current price as reference
        current_price = self._best_current_price()
        if current_price is None:
            return None

        return Prediction(
            source="aggregator",
            predicted_price=blended_price,
            current_price=current_price,
            horizon_s=self._horizon_s,
            confidence=avg_confidence,
        )

    def _extrapolate(
        self, window: deque[tuple[int, float]]
    ) -> tuple[float | None, float]:
        """
        Linear regression on recent ticks → extrapolate to horizon.
        Returns (predicted_price, confidence).
        """
        if len(window) < 10:
            return None, 0.0

        ts = np.array([t for t, _ in window], dtype=np.float64)
        prices = np.array([p for _, p in window], dtype=np.float64)

        # Normalize time to seconds from first tick
        ts_s = (ts - ts[0]) / 1e9
        if ts_s[-1] - ts_s[0] < 1.0:
            return None, 0.0

        # Linear regression
        n = len(ts_s)
        sx = np.sum(ts_s)
        sy = np.sum(prices)
        sxy = np.sum(ts_s * prices)
        sxx = np.sum(ts_s * ts_s)
        denom = n * sxx - sx * sx
        if abs(denom) < 1e-12:
            return None, 0.0

        slope = (n * sxy - sx * sy) / denom
        intercept = (sy - slope * sx) / n

        # Extrapolate
        future_t = ts_s[-1] + self._horizon_s
        predicted = intercept + slope * future_t

        # Confidence: R² of the fit
        y_hat = intercept + slope * ts_s
        ss_res = np.sum((prices - y_hat) ** 2)
        ss_tot = np.sum((prices - np.mean(prices)) ** 2)
        r_squared = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
        confidence = max(0.0, min(r_squared, 1.0))

        return predicted, confidence

    def _best_current_price(self) -> float | None:
        """Return the most recent price, preferring Binance."""
        for source in ("binance", "cryptocompare", "coingecko"):
            if source in self._latest:
                return self._latest[source]
        return next(iter(self._latest.values()), None)
