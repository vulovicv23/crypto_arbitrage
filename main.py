#!/usr/bin/env python3
"""
Polymarket BTC Latency-Arbitrage Bot — main entry point.

Architecture:
  ┌──────────────────────────────────────────────────────────────────┐
  │                         MAIN LOOP                               │
  │                                                                  │
  │  ┌──────────────────┐                                           │
  │  │ Market Discovery  │  ← Gamma API: discovers 5m/15m markets   │
  │  │ (periodic scan)   │────┐                                     │
  │  └──────────────────┘    │ new token_ids                       │
  │                           ▼                                      │
  │  ┌─────────────┐   price_queue   ┌──────────────┐               │
  │  │ BinanceWS   │───────┐         │  Prediction  │               │
  │  │ CryptoComp  │───────┼────────▶│  Aggregator  │               │
  │  │ CoinGecko   │───────┘         └──────┬───────┘               │
  │                                         │ prediction_queue      │
  │  ┌───────────────┐                      ▼                       │
  │  │ Polymarket WS │──book──▶  ┌──────────────────┐               │
  │  │ (book stream) │          │  Strategy Engine  │               │
  │  └───────────────┘          └────────┬─────────┘               │
  │                                      │ signal_queue             │
  │                                      ▼                          │
  │                            ┌─────────────────┐                  │
  │                            │  Risk Manager   │                  │
  │                            └────────┬────────┘                  │
  │                                     │                           │
  │                                     ▼                           │
  │                            ┌─────────────────┐                  │
  │                            │  Order Manager  │──▶ Polymarket    │
  │                            └─────────────────┘     CLOB REST    │
  │                                                                  │
  │  ┌──────────────────────────────────────────────────────────┐   │
  │  │              Health Monitor (periodic stats)             │   │
  │  └──────────────────────────────────────────────────────────┘   │
  └──────────────────────────────────────────────────────────────────┘

Usage:
    python main.py                  # run the bot (auto-discovers markets)
    python main.py --dry-run        # run without placing real orders
    python main.py --capital 10000  # override starting capital
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
import time

import random

from config import AppConfig, load_config
from src.logger_setup import setup_logging
from src.market_discovery import DiscoveredMarket, MarketDiscovery
from src.models import (
    MarketContext,
    OrderStatus,
    Position,
    Prediction,
    PriceTick,
    Side,
    Signal,
    TokenOutcome,
)
from src.order_manager import OrderManager
from src.polymarket_client import PolymarketClient
from src.prediction_sources import (
    BinanceSource,
    CoinGeckoSource,
    CryptoCompareSource,
    CryptoCompareWSSource,
    PredictionAggregator,
    PriceSource,
)
from src.risk_manager import RiskManager
from src.strategy import StrategyEngine
from src.synthetic_books import SyntheticBookGenerator

logger = logging.getLogger(__name__)


class Bot:
    """Top-level orchestrator that wires all components and manages lifecycle."""

    def __init__(self, config: AppConfig, capital: float, dry_run: bool = False):
        self._cfg = config
        self._dry_run = dry_run
        self._capital = capital
        self._running = False

        # Async queues that connect the pipeline stages.
        self._price_queue: asyncio.Queue[PriceTick] = asyncio.Queue(maxsize=5000)
        self._prediction_queue: asyncio.Queue[Prediction] = asyncio.Queue(maxsize=500)
        self._signal_queue: asyncio.Queue[Signal] = asyncio.Queue(maxsize=200)

        # Components (initialised in start())
        self._poly_client: PolymarketClient | None = None
        self._risk_manager: RiskManager | None = None
        self._strategy: StrategyEngine | None = None
        self._order_manager: OrderManager | None = None
        self._aggregator: PredictionAggregator | None = None
        self._discovery: MarketDiscovery | None = None
        self._synthetic_books: SyntheticBookGenerator | None = None
        self._position_resolver: PositionResolver | None = None
        self._sources: list = []
        self._session_start_ns: int = time.time_ns()

        # Track subscribed token IDs for incremental WS subscription
        self._subscribed_tokens: set[str] = set()

        # Whether the Polymarket client is connected (WS + REST).
        # In dry-run mode we attempt connection but fall back to synthetic
        # books if it fails.
        self._poly_connected = False

        # Task handles for clean shutdown
        self._tasks: list[asyncio.Task] = []

    # ── lifecycle ─────────────────────────────────────────────────────

    async def start(self) -> None:
        """Wire components and launch all async tasks."""
        logger.info("=" * 60)
        logger.info("Polymarket BTC Latency-Arb Bot starting")
        mode = "PAPER (dry-run)" if self._dry_run else "LIVE"
        logger.info("Capital: $%.2f | Mode: %s", self._capital, mode)
        logger.info(
            "Edge threshold: %.4f–%.4f",
            self._cfg.strategy.min_edge_threshold,
            self._cfg.strategy.max_edge_threshold,
        )
        logger.info(
            "Max position: %.2f%% | Daily loss limit: %.2f%%",
            self._cfg.risk.max_position_pct * 100,
            self._cfg.risk.max_daily_loss_pct * 100,
        )
        if self._cfg.discovery.enabled:
            logger.info(
                "Auto-discovery: ON | Assets=%s | Timeframes=%s | Interval=%.0fs",
                self._cfg.discovery.assets,
                self._cfg.discovery.timeframes,
                self._cfg.discovery.interval_s,
            )
        else:
            logger.info(
                "Auto-discovery: OFF | Static IDs=%s",
                self._cfg.polymarket.btc_condition_ids,
            )
        logger.info("=" * 60)

        self._running = True

        # ── Polymarket client ─────────────────────────────────────────
        self._poly_client = PolymarketClient(self._cfg.polymarket, self._cfg.execution)

        # Check if we have valid API credentials for real Polymarket data.
        has_poly_creds = bool(
            self._cfg.polymarket.api_key and self._cfg.polymarket.api_secret
        )

        if has_poly_creds:
            try:
                await self._poly_client.connect()
                self._poly_connected = True
                logger.info("Polymarket client connected (real book data)")
            except Exception as exc:
                self._poly_connected = False
                if self._dry_run:
                    logger.warning(
                        "Polymarket connection failed (dry-run will use "
                        "synthetic books): %s",
                        exc,
                    )
                else:
                    raise  # Live mode requires a working connection
        elif self._dry_run:
            self._poly_connected = False
            logger.info(
                "No Polymarket API credentials — dry-run will use " "synthetic books"
            )
        else:
            raise ValueError(
                "POLY_API_KEY and POLY_API_SECRET required for live trading"
            )

        # ── Risk manager ──────────────────────────────────────────────
        self._risk_manager = RiskManager(
            self._cfg.risk, self._cfg.execution, self._capital
        )

        # ── Strategy engine ───────────────────────────────────────────
        self._strategy = StrategyEngine(
            self._cfg.strategy, self._prediction_queue, self._signal_queue
        )

        # ── Synthetic book generator (dry-run fallback) ───────────────
        if self._dry_run and not self._poly_connected:
            dr = self._cfg.dry_run
            self._synthetic_books = SyntheticBookGenerator(
                book_callback=self._strategy.on_book_update,
                update_interval=dr.book_update_interval,
                ema_alpha=dr.book_ema_alpha,
                noise_std=dr.book_noise_std,
                spread_pct=dr.book_spread_pct,
            )
            logger.info(
                "Synthetic book generator enabled (interval=%.1fs, "
                "ema_alpha=%.3f, noise=%.3f, spread=%.3f)",
                dr.book_update_interval,
                dr.book_ema_alpha,
                dr.book_noise_std,
                dr.book_spread_pct,
            )

        # ── Order manager ─────────────────────────────────────────────
        if self._dry_run:
            self._order_manager = DryRunOrderManager(
                self._cfg,
                self._signal_queue,
                self._poly_client,
                self._risk_manager,
            )
        else:
            self._order_manager = OrderManager(
                self._cfg,
                self._signal_queue,
                self._poly_client,
                self._risk_manager,
            )

        # ── Position resolver (paper mode: auto-close at market expiry) ─
        if self._dry_run:
            self._position_resolver = PositionResolver(
                risk_manager=self._risk_manager,
                get_btc_price=self._get_btc_price,
                poll_interval=1.0,
            )

        # ── Prediction sources ────────────────────────────────────────
        # CryptoCompare: prefer WebSocket (CCCAGG stream) when API key is
        # available; fall back to REST polling otherwise.
        if self._cfg.predictions.cryptocompare_api_key:
            cc_source: PriceSource = CryptoCompareWSSource(
                self._price_queue,
                api_key=self._cfg.predictions.cryptocompare_api_key,
                ws_url=self._cfg.predictions.cryptocompare_ws_url,
            )
            logger.info("CryptoCompare: using WebSocket (CCCAGG stream)")
        else:
            cc_source = CryptoCompareSource(
                self._price_queue,
                api_key="",
                url=self._cfg.predictions.cryptocompare_url,
                poll_interval=self._cfg.predictions.rest_poll_interval,
            )
            logger.info("CryptoCompare: using REST polling (no API key)")

        self._sources = [
            BinanceSource(self._price_queue, self._cfg.predictions.binance_ws_url),
            cc_source,
            CoinGeckoSource(
                self._price_queue,
                self._cfg.predictions.coingecko_url,
                self._cfg.predictions.rest_poll_interval * 2,
            ),
        ]

        self._aggregator = PredictionAggregator(
            self._price_queue,
            self._prediction_queue,
            self._cfg.strategy.prediction_horizon_s,
        )

        # ── Launch tasks ──────────────────────────────────────────────
        for src in self._sources:
            self._tasks.append(
                asyncio.create_task(src.start(), name=f"source-{src.name}")
            )

        self._tasks.append(
            asyncio.create_task(self._aggregator.start(), name="aggregator")
        )
        self._tasks.append(asyncio.create_task(self._strategy.run(), name="strategy"))
        self._tasks.append(
            asyncio.create_task(self._order_manager.run(), name="order-manager")
        )
        self._tasks.append(
            asyncio.create_task(self._health_monitor(), name="health-monitor")
        )

        # Synthetic book generator tasks (dry-run without Polymarket connection)
        if self._synthetic_books is not None:
            self._tasks.append(
                asyncio.create_task(self._synthetic_books.run(), name="synthetic-books")
            )
            self._tasks.append(
                asyncio.create_task(self._prediction_tap(), name="prediction-tap")
            )

        # Position resolver (paper mode)
        if self._position_resolver is not None:
            self._tasks.append(
                asyncio.create_task(
                    self._position_resolver.run(), name="position-resolver"
                )
            )

        # ── Market discovery or static subscription ───────────────────
        if self._cfg.discovery.enabled:
            # Dynamic: auto-discover 5m/15m markets and subscribe on the fly
            self._discovery = MarketDiscovery(
                assets=self._cfg.discovery.assets,
                timeframes=self._cfg.discovery.timeframes,
                min_seconds_to_resolution=self._cfg.discovery.min_seconds_to_resolution,
                discovery_interval=self._cfg.discovery.interval_s,
                burst_poll_interval=self._cfg.discovery.burst_poll_interval,
                burst_window=self._cfg.discovery.burst_window,
                lead_time=self._cfg.discovery.lead_time,
                callback=self._on_markets_changed,
            )
            self._tasks.append(
                asyncio.create_task(self._discovery.start(), name="market-discovery")
            )
        else:
            # Static: use hardcoded condition IDs from config
            token_mapping = {cid: cid for cid in self._cfg.polymarket.btc_condition_ids}
            self._strategy.set_token_mapping(token_mapping)

            if not self._dry_run and self._cfg.polymarket.btc_condition_ids:
                self._poly_client.set_token_condition_map(token_mapping)
                await self._poly_client.subscribe_books(
                    self._cfg.polymarket.btc_condition_ids,
                    self._strategy.on_book_update,
                )

        logger.info("All %d tasks launched", len(self._tasks))

        # Wait until shutdown
        try:
            await asyncio.gather(*self._tasks)
        except asyncio.CancelledError:
            pass

    async def stop(self) -> None:
        """Graceful shutdown: cancel orders, close connections, print summary."""
        logger.info("Shutting down…")
        self._running = False

        # Stop sources
        for src in self._sources:
            src.stop()

        # Stop discovery
        if self._discovery:
            await self._discovery.stop()

        # Stop synthetic book generator
        if self._synthetic_books:
            self._synthetic_books.stop()

        # Stop position resolver
        if self._position_resolver:
            self._position_resolver.stop()

        # Cancel pending orders
        if self._order_manager and not self._dry_run:
            await self._order_manager.cancel_all_orders()

        # Cancel tasks
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

        # Close Polymarket connection
        if self._poly_client:
            await self._poly_client.close()

        # Force-resolve any remaining open positions for P&L accounting
        if self._position_resolver:
            self._position_resolver.force_resolve_all()

        # Print session summary
        self._print_session_summary()

        logger.info("Shutdown complete")

    def _print_session_summary(self) -> None:
        """Log a comprehensive session summary at shutdown."""
        duration_s = (time.time_ns() - self._session_start_ns) / 1_000_000_000
        mins, secs = divmod(int(duration_s), 60)
        hours, mins = divmod(mins, 60)
        if hours > 0:
            duration_str = f"{hours}h {mins}m {secs}s"
        elif mins > 0:
            duration_str = f"{mins}m {secs}s"
        else:
            duration_str = f"{secs}s"

        order_stats = self._order_manager.stats() if self._order_manager else {}
        daily_pnl = order_stats.get("daily_pnl", 0.0)
        balance = self._capital + daily_pnl
        pnl_pct = (daily_pnl / self._capital * 100) if self._capital > 0 else 0.0

        resolver_stats = (
            self._position_resolver.stats() if self._position_resolver else {}
        )
        resolved = resolver_stats.get("resolved", 0)
        won = resolver_stats.get("won", 0)
        lost = resolver_stats.get("lost", 0)
        resolver_wr = (won / resolved * 100) if resolved > 0 else 0.0

        open_count = (
            len(self._risk_manager.state.open_positions) if self._risk_manager else 0
        )

        pnl_sign = "+" if daily_pnl >= 0 else ""
        volume = order_stats.get("total_volume", 0.0)

        sep = "=" * 58
        logger.info(sep)
        logger.info("              SESSION SUMMARY")
        logger.info(sep)
        logger.info("  Duration:           %s", duration_str)
        logger.info(
            "  Mode:               %s", "PAPER (dry-run)" if self._dry_run else "LIVE"
        )
        logger.info("  Starting capital:   $%.2f", self._capital)
        logger.info("  Current balance:    $%.2f", balance)
        logger.info(
            "  Session P&L:        %s$%.4f (%+.2f%%)",
            pnl_sign,
            daily_pnl,
            pnl_pct,
        )
        logger.info(sep)
        logger.info(
            "  Orders:             %d submitted, %d filled, %d rejected",
            order_stats.get("submitted", 0),
            order_stats.get("filled", 0),
            order_stats.get("rejected", 0),
        )
        logger.info(
            "  Risk blocked:       %d signals",
            order_stats.get("risk_blocked", 0),
        )
        logger.info(
            "  Resolved positions: %d (W:%d L:%d, win rate: %.1f%%)",
            resolved,
            won,
            lost,
            resolver_wr,
        )
        logger.info("  Open positions:     %d (unresolved)", open_count)
        logger.info("  Total volume:       $%.2f", volume)
        if self._risk_manager:
            logger.info(
                "  Max drawdown:       $%.4f",
                self._risk_manager.daily_pnl.max_drawdown,
            )
            logger.info(
                "  Overall win rate:   %.1f%% (%d/%d trades)",
                order_stats.get("win_rate", 0) * 100,
                self._risk_manager.daily_pnl.winning_trades,
                self._risk_manager.daily_pnl.total_trades,
            )
        logger.info(sep)

    # ── market discovery callback ──────────────────────────────────────

    async def _on_markets_changed(
        self,
        new_markets: list[DiscoveredMarket],
        expired_ids: list[str],
    ) -> None:
        """Called by MarketDiscovery when active markets change.

        Wires newly discovered markets into the strategy engine,
        subscribes to new order books via the WS pool, and
        unsubscribes from expired markets to free capacity.
        """
        if not self._strategy:
            return

        # Build token mapping: token_id → condition_id for all active markets
        assert self._discovery is not None
        token_mapping: dict[str, str] = {}
        market_contexts: dict[str, MarketContext] = {}
        for m in self._discovery.active_markets.values():
            token_mapping[m.yes_token_id] = m.condition_id
            token_mapping[m.no_token_id] = m.condition_id

            # Build MarketContext for the strategy's probability model
            ctx = MarketContext(
                condition_id=m.condition_id,
                yes_token_id=m.yes_token_id,
                no_token_id=m.no_token_id,
                end_date_ns=int(m.end_date.timestamp() * 1_000_000_000),
                timeframe_seconds=m.timeframe.resolution_seconds,
                asset=m.asset,
            )
            market_contexts[m.yes_token_id] = ctx
            market_contexts[m.no_token_id] = ctx

        self._strategy.set_market_contexts(market_contexts)

        # Update the client's condition map so WS updates carry condition_id
        if self._poly_client:
            self._poly_client.set_token_condition_map(token_mapping)

        # Update synthetic book generator with new market contexts
        if self._synthetic_books is not None:
            self._synthetic_books.set_market_contexts(market_contexts)

        # Update position resolver with new market contexts
        if self._position_resolver is not None:
            self._position_resolver.set_market_contexts(market_contexts)

        # ── Subscribe to new markets (real WS if connected) ────────────
        if self._poly_connected and self._poly_client:
            new_token_ids: list[str] = []
            for m in new_markets:
                for tid in m.token_ids:
                    if tid not in self._subscribed_tokens:
                        new_token_ids.append(tid)
                        self._subscribed_tokens.add(tid)

            if new_token_ids:
                logger.info(
                    "Subscribing to %d new token IDs from %d new markets",
                    len(new_token_ids),
                    len(new_markets),
                )
                await self._poly_client.subscribe_books(
                    new_token_ids,
                    self._strategy.on_book_update,
                )

        # ── Fetch initial book snapshots for new markets ───────────────
        if self._poly_connected and self._poly_client:
            for m in new_markets:
                for tid in m.token_ids:
                    try:
                        book = await self._poly_client.get_order_book(tid)
                        await self._strategy.on_book_update(book)
                    except Exception as exc:
                        logger.warning(
                            "Failed to fetch initial book for %s: %s",
                            tid[:12],
                            exc,
                        )

        # ── Unsubscribe from expired markets ───────────────────────────
        if expired_ids and self._poly_connected and self._poly_client:
            expired_token_ids: list[str] = []
            for cid in expired_ids:
                tokens_to_remove = [tid for tid, c in token_mapping.items() if c == cid]
                for tid in list(self._subscribed_tokens):
                    if tid in tokens_to_remove:
                        expired_token_ids.append(tid)
                        self._subscribed_tokens.discard(tid)

            if expired_token_ids:
                logger.info(
                    "Unsubscribing %d expired token IDs from %d expired markets",
                    len(expired_token_ids),
                    len(expired_ids),
                )
                await self._poly_client.unsubscribe_books(expired_token_ids)

    # ── BTC price accessor ──────────────────────────────────────────

    def _get_btc_price(self) -> float | None:
        """Return the latest BTC price from the strategy's price history."""
        if self._strategy and self._strategy._btc_price_history:
            return self._strategy._btc_price_history[-1]
        return None

    # ── prediction tap (feeds synthetic book generator) ──────────────

    async def _prediction_tap(self) -> None:
        """Tap the prediction queue to feed the synthetic book generator.

        The prediction_queue is consumed by the strategy engine.  We can't
        consume from it again, so we wrap the aggregator's output: the
        aggregator emits to the prediction_queue, and we also feed each
        prediction to the synthetic book generator.

        This task watches the prediction queue size and periodically
        peeks at the strategy's latest BTC price data to keep the
        synthetic generator updated.
        """
        assert self._synthetic_books is not None
        logger.info("Prediction tap started (feeding synthetic book generator)")

        while True:
            # The strategy consumes predictions from the queue.
            # We can't double-consume, so instead we poll the strategy's
            # internal BTC price history and the latest prediction state.
            await asyncio.sleep(0.5)

            if self._strategy and self._strategy._btc_price_history:
                latest_price = self._strategy._btc_price_history[-1]
                # Build a lightweight Prediction for the synthetic generator
                fake_pred = Prediction(
                    source="tap",
                    predicted_price=latest_price,
                    current_price=latest_price,
                    horizon_s=self._cfg.strategy.prediction_horizon_s,
                    confidence=0.5,
                )
                self._synthetic_books.feed_prediction(fake_pred)

    # ── health monitor ────────────────────────────────────────────────

    async def _health_monitor(self) -> None:
        """Periodic stats dump with balance and P&L tracking."""
        while self._running:
            await asyncio.sleep(30)
            if self._order_manager:
                stats = self._order_manager.stats()
                daily_pnl = stats["daily_pnl"]
                balance = self._capital + daily_pnl
                open_count = (
                    len(self._risk_manager.state.open_positions)
                    if self._risk_manager
                    else 0
                )

                # Resolver stats
                resolver_info = ""
                if self._position_resolver:
                    rs = self._position_resolver.stats()
                    resolver_info = (
                        f" resolved={rs['resolved']}" f" (W:{rs['won']} L:{rs['lost']})"
                    )

                discovery_info = ""
                if self._discovery:
                    d = self._discovery.stats()
                    discovery_info = (
                        f" markets_active={d['active']}"
                        f" discovered={d['total_discovered']}"
                        f" expired={d['total_expired']}"
                        f" burst_cycles={d['burst_cycles']}"
                        f" next_boundary={d['next_boundary_s']}s"
                    )

                logger.info(
                    "HEALTH | balance=$%.2f pnl=%+.4f open=%d "
                    "filled=%d rejected=%d risk_blocked=%d "
                    "win_rate=%.1f%% volume=%.2f regime=%s%s%s",
                    balance,
                    daily_pnl,
                    open_count,
                    stats["filled"],
                    stats["rejected"],
                    stats["risk_blocked"],
                    stats["win_rate"] * 100,
                    stats["total_volume"],
                    self._strategy.current_regime.name if self._strategy else "?",
                    resolver_info,
                    discovery_info,
                )

            # Log queue depths and WS/synthetic book stats
            ws_info = ""
            if self._poly_connected and self._poly_client:
                ws = self._poly_client.ws_stats()
                ws_info = (
                    f" ws_clients={ws['clients']}"
                    f" ws_subscriptions={ws['total_subscriptions']}"
                    f" ws_messages={ws['total_messages']}"
                    f" ws_reconnects={ws['total_reconnects']}"
                )
            if self._synthetic_books is not None:
                sb = self._synthetic_books.stats()
                ws_info += (
                    f" synth_books={sb['books_emitted']}"
                    f" synth_tokens={sb['tracked_tokens']}"
                    f" synth_ema={sb['market_ema_price']}"
                )
            logger.debug(
                "Queues | price=%d prediction=%d signal=%d " "subscribed_tokens=%d%s",
                self._price_queue.qsize(),
                self._prediction_queue.qsize(),
                self._signal_queue.qsize(),
                len(self._subscribed_tokens),
                ws_info,
            )


class PositionResolver:
    """Auto-resolves paper positions when their binary markets expire.

    At expiry, determines if BTC went UP or DOWN by comparing current
    BTC price against the reference price recorded when the market was
    first discovered.

    Settlement:
      - YES tokens: settle at 1.0 if BTC UP, 0.0 if DOWN
      - NO tokens:  settle at 1.0 if BTC DOWN, 0.0 if UP
      - P&L = (settlement - entry_price) * size  (for BUY)
      - P&L = (entry_price - settlement) * size  (for SELL)
    """

    def __init__(
        self,
        risk_manager: RiskManager,
        get_btc_price,
        poll_interval: float = 1.0,
    ):
        self._risk = risk_manager
        self._get_btc_price = get_btc_price
        self._poll_interval = poll_interval

        # condition_id -> reference BTC price recorded at market discovery
        self._reference_prices: dict[str, float] = {}
        # token_id -> MarketContext
        self._market_contexts: dict[str, MarketContext] = {}

        self._running = False

        # Session stats
        self._total_resolved = 0
        self._total_won = 0
        self._total_lost = 0
        self._cumulative_pnl = 0.0

    def set_market_contexts(self, contexts: dict[str, MarketContext]) -> None:
        """Merge new market contexts (accumulates; never drops old ones).

        We must keep contexts for expired markets so positions opened
        in those markets can still be resolved after expiry.
        """
        self._market_contexts.update(contexts)
        btc_price = self._get_btc_price()
        if btc_price is not None and btc_price > 0:
            for ctx in contexts.values():
                if ctx.condition_id not in self._reference_prices:
                    self._reference_prices[ctx.condition_id] = btc_price

    async def run(self) -> None:
        self._running = True
        logger.info("Position resolver started (poll=%.1fs)", self._poll_interval)
        while self._running:
            await asyncio.sleep(self._poll_interval)
            try:
                self._check_expired()
            except Exception:
                logger.exception("Position resolver error")

    def stop(self) -> None:
        self._running = False

    def _check_expired(self) -> None:
        """Resolve all positions whose markets have expired."""
        open_positions = self._risk.state.open_positions
        if not open_positions:
            return

        now_ns = time.time_ns()
        btc_price = self._get_btc_price()
        if btc_price is None or btc_price <= 0:
            return

        to_close: list[tuple[str, Position, MarketContext]] = []
        for token_id, position in list(open_positions.items()):
            ctx = self._market_contexts.get(token_id)
            if ctx is None:
                continue
            if now_ns >= ctx.end_date_ns:
                to_close.append((token_id, position, ctx))

        for token_id, position, ctx in to_close:
            self._resolve_position(token_id, position, ctx, btc_price)

    def _resolve_position(
        self,
        token_id: str,
        position: Position,
        ctx: MarketContext,
        current_btc_price: float,
    ) -> None:
        ref_price = self._reference_prices.get(ctx.condition_id)
        if ref_price is None or ref_price <= 0:
            logger.warning(
                "No reference price for %s — settling flat",
                ctx.condition_id[:12],
            )
            self._risk.record_close(token_id, 0.0, position.size)
            self._total_resolved += 1
            return

        btc_went_up = current_btc_price > ref_price

        outcome = ctx.token_outcome(token_id)
        if outcome == TokenOutcome.YES:
            settlement = 1.0 if btc_went_up else 0.0
        elif outcome == TokenOutcome.NO:
            settlement = 0.0 if btc_went_up else 1.0
        else:
            settlement = position.entry_price  # unknown, no P&L

        if position.side == Side.BUY:
            pnl = (settlement - position.entry_price) * position.size
        else:
            pnl = (position.entry_price - settlement) * position.size

        self._risk.record_close(token_id, pnl, position.size)

        self._total_resolved += 1
        self._cumulative_pnl += pnl
        if pnl > 0:
            self._total_won += 1
        else:
            self._total_lost += 1

        btc_change_pct = (current_btc_price - ref_price) / ref_price * 100
        result = "WIN" if pnl > 0 else "LOSS"
        outcome_str = outcome.value if outcome else "?"

        logger.info(
            "RESOLVED %s: %s %s %s settle=%.2f entry=%.4f pnl=%+.4f "
            "size=%.2f btc_ref=%.2f now=%.2f (%+.3f%% %s)",
            result,
            position.side.value,
            outcome_str,
            token_id[:12],
            settlement,
            position.entry_price,
            pnl,
            position.size,
            ref_price,
            current_btc_price,
            btc_change_pct,
            "UP" if btc_went_up else "DOWN",
        )

    def force_resolve_all(self) -> None:
        """Resolve all open positions at current BTC price (used at shutdown).

        For positions whose markets haven't expired yet, we settle based on
        current BTC price direction vs. the reference price — an early exit
        at the 'current fair value' rather than waiting for binary settlement.
        """
        open_positions = dict(self._risk.state.open_positions)
        if not open_positions:
            return

        btc_price = self._get_btc_price()
        if btc_price is None or btc_price <= 0:
            logger.warning("Cannot force-resolve: no BTC price available")
            return

        logger.info(
            "Force-resolving %d open positions at BTC=$%.2f",
            len(open_positions),
            btc_price,
        )

        for token_id, position in open_positions.items():
            ctx = self._market_contexts.get(token_id)
            if ctx is None:
                # No context — settle flat
                self._risk.record_close(token_id, 0.0, position.size)
                self._total_resolved += 1
                continue
            self._resolve_position(token_id, position, ctx, btc_price)

    def stats(self) -> dict:
        return {
            "resolved": self._total_resolved,
            "won": self._total_won,
            "lost": self._total_lost,
            "cumulative_pnl": self._cumulative_pnl,
            "tracked_conditions": len(self._reference_prices),
        }


class DryRunOrderManager(OrderManager):
    """Paper-trading order manager with realistic fill simulation.

    Simulates:
      - Configurable fill rate (random rejections).
      - Taker mode: slippage on fill price (adverse price movement).
      - Maker mode: fill at limit price (no slippage — you're providing
        liquidity), but with a lower fill probability since the market
        must come to your price.
      - Latency delay before fill.

    Overrides _process_signal to refresh signal timestamps: in paper mode,
    hundreds of signals queue up and would all be rejected as "too stale"
    by the risk manager's latency check. We refresh the timestamp to the
    current time so the risk check evaluates the signal's *quality*, not
    its queue wait time.
    """

    async def _process_signal(self, signal: Signal) -> None:
        # Refresh timestamp so the risk manager's latency check evaluates
        # signal age from now, not from when it was originally generated.
        signal.timestamp_ns = time.time_ns()
        await super()._process_signal(signal)

    async def _submit_order(self, order, signal) -> None:
        dr = self._cfg.dry_run
        is_maker = self._cfg.strategy.maker_mode

        # 1. Simulate latency
        if dr.latency_ms > 0:
            await asyncio.sleep(dr.latency_ms / 1000)

        # 2. Simulate fill probability
        # Maker orders have lower fill rate since the market must
        # come to your price; taker orders use the configured rate.
        if is_maker:
            # Maker fill probability: how far is limit from mid?
            # Closer to mid = more likely to fill.
            mid = signal.book.mid_price
            if mid > 0:
                distance_from_mid = abs(order.price - mid) / mid
                # Base fill rate scaled by distance: orders at mid fill ~70%,
                # orders far from mid fill less.
                maker_fill_rate = max(0.3, min(0.7, 0.7 - distance_from_mid))
            else:
                maker_fill_rate = 0.5
            effective_fill_rate = maker_fill_rate
        else:
            effective_fill_rate = dr.fill_rate

        if random.random() > effective_fill_rate:
            order.status = OrderStatus.REJECTED
            self._total_rejected += 1
            mode_str = "MAKER" if is_maker else "TAKER"
            logger.info(
                "PAPER REJECT [%s]: %s %s price=%.4f edge=%.4f (fill_rate=%.0f%%)",
                mode_str,
                order.side.value,
                order.token_id[:12],
                order.price,
                signal.edge,
                effective_fill_rate * 100,
            )
            return

        # 3. Determine fill price
        if is_maker:
            # Maker: fill at limit price (no adverse slippage — you're
            # providing liquidity, so you get price improvement).
            fill_price = order.price
        else:
            # Taker: apply slippage (adverse direction)
            if dr.slippage_pct > 0:
                slippage = order.price * dr.slippage_pct
                if order.side == Side.BUY:
                    fill_price = order.price + slippage  # pay more
                else:
                    fill_price = order.price - slippage  # receive less
            else:
                fill_price = order.price

        # 4. Mark filled
        order.mark_submitted(f"paper-{order.order_id[:8]}")
        order.mark_filled(fill_price, order.size)

        position = Position(
            condition_id=order.condition_id,
            token_id=order.token_id,
            side=order.side,
            entry_price=order.fill_price,
            size=order.fill_size,
        )
        self._risk.record_fill(position)
        self._log_trade(order, signal)

        self._total_submitted += 1
        self._total_filled += 1

        slippage_bps = (
            (fill_price - order.price) / order.price * 10_000 if order.price > 0 else 0
        )
        mode_str = "MAKER" if is_maker else "TAKER"
        logger.info(
            "PAPER ORDER [%s]: %s %s price=%.4f fill=%.4f size=%.2f "
            "slippage=%.1fbps latency=%.1fms edge=%.4f spread=%.4f",
            mode_str,
            order.side.value,
            order.token_id[:12],
            order.price,
            fill_price,
            order.size,
            slippage_bps,
            order.latency_ms,
            signal.edge,
            signal.book.spread,
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Polymarket BTC Latency-Arbitrage Bot")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log trades without submitting real orders",
    )
    parser.add_argument(
        "--capital",
        type=float,
        default=10_000.0,
        help="Starting capital in USDC (default: 10000)",
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=0,
        help="Run for N seconds then shut down (0 = forever, default: 0)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    # Load and validate config (reads .env)
    try:
        config = load_config()
    except ValueError as exc:
        if args.dry_run:
            # In dry-run mode, allow missing API keys.
            from config import AppConfig

            config = AppConfig()
            print(f"[WARNING] Config validation skipped for dry-run: {exc}")
        else:
            print(f"[FATAL] Configuration error: {exc}", file=sys.stderr)
            sys.exit(1)

    setup_logging(config.logging)

    bot = Bot(config, capital=args.capital, dry_run=args.dry_run)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    shutdown_event = asyncio.Event()

    def _request_shutdown():
        logger.info("Shutdown signal received")
        shutdown_event.set()

    for sig_name in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig_name, _request_shutdown)

    async def _run():
        bot_task = asyncio.ensure_future(bot.start())
        # Wait for a shutdown signal or duration timeout
        if args.duration > 0:
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=args.duration)
            except asyncio.TimeoutError:
                logger.info(
                    "Duration limit reached (%ds) — shutting down",
                    args.duration,
                )
        else:
            await shutdown_event.wait()
        await bot.stop()
        bot_task.cancel()
        try:
            await bot_task
        except asyncio.CancelledError:
            pass

    try:
        loop.run_until_complete(_run())
    except KeyboardInterrupt:
        loop.run_until_complete(bot.stop())
    finally:
        # Give pending tasks a moment to clean up
        pending = asyncio.all_tasks(loop)
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()


if __name__ == "__main__":
    main()
