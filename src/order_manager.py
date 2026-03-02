"""
Order management and execution pipeline.

Consumes Signal objects from the strategy, passes them through risk
checks, builds Order objects, submits them to Polymarket, and tracks
their lifecycle.

Pipeline:
  signal_queue → risk_check → build_order → submit → track → close

All operations are async; submission targets <100ms latency.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from pathlib import Path

from config import AppConfig
from src.models import (
    Order,
    OrderStatus,
    Position,
    Side,
    Signal,
)
from src.polymarket_client import PolymarketClient
from src.risk_manager import RiskManager

logger = logging.getLogger(__name__)


class OrderManager:
    """
    Async order lifecycle manager.

    Responsibilities:
      - Consume signals, gate through risk manager.
      - Submit orders with minimal latency.
      - Track open orders and positions.
      - Emit trade logs (JSONL).
    """

    def __init__(
        self,
        config: AppConfig,
        signal_queue: asyncio.Queue[Signal],
        poly_client: PolymarketClient,
        risk_manager: RiskManager,
    ):
        self._cfg = config
        self._signal_queue = signal_queue
        self._poly = poly_client
        self._risk = risk_manager

        # Local order tracking
        self._pending_orders: dict[str, Order] = {}
        self._filled_orders: list[Order] = []

        # Trade log path
        self._trade_log = Path(config.logging.log_dir) / config.logging.trade_log_file

        # Stats
        self._total_submitted = 0
        self._total_filled = 0
        self._total_rejected = 0
        self._total_risk_blocked = 0

    # ── main loop ─────────────────────────────────────────────────────

    async def run(self) -> None:
        """Process signals from the queue and manage orders."""
        logger.info("Order manager started")
        while True:
            signal = await self._signal_queue.get()
            try:
                await self._process_signal(signal)
            except Exception:
                logger.exception("Error processing signal")

    async def _process_signal(self, signal: Signal) -> None:
        """Full pipeline for one signal: risk → order → submit."""
        # 1. Risk check
        approved, size, reason = self._risk.check_signal(signal)
        if not approved:
            self._total_risk_blocked += 1
            logger.debug("Signal blocked by risk: %s", reason)
            return

        # 2. Build order
        order = self._build_order(signal, size)

        # 3. Submit
        await self._submit_order(order, signal)

    # ── order construction ────────────────────────────────────────────

    def _build_order(self, signal: Signal, size: float) -> Order:
        """Create an Order from a Signal.

        In taker mode (default when maker_mode=False):
          BUY  → cross the spread at best_ask
          SELL → cross the spread at best_bid

        In maker mode (maker_mode=True):
          BUY  → place limit at fair_value - min_edge (inside the spread)
          SELL → place limit at fair_value + min_edge (inside the spread)
          This gives better execution but risks non-fill.
        """
        maker_mode = self._cfg.strategy.maker_mode

        if signal.side == Side.BUY:
            if maker_mode:
                # Limit price inside the spread: we're willing to pay up to
                # fair_value minus our required edge (guaranteed edge if filled).
                fair_value = signal.edge + signal.book.best_ask
                limit_price = fair_value - self._cfg.strategy.min_edge_threshold
                # Clamp between bid+tick and ask (must improve on best_ask)
                price = max(
                    signal.book.best_bid + 0.01,
                    min(limit_price, signal.book.best_ask - 0.01),
                )
            else:
                price = signal.book.best_ask
        else:
            if maker_mode:
                fair_value = signal.book.best_bid - signal.edge
                limit_price = fair_value + self._cfg.strategy.min_edge_threshold
                price = min(
                    signal.book.best_ask - 0.01,
                    max(limit_price, signal.book.best_bid + 0.01),
                )
            else:
                price = signal.book.best_bid

        # Round to Polymarket's tick size (0.01)
        price = round(price, 2)

        return Order(
            order_id=str(uuid.uuid4()),
            condition_id=signal.condition_id,
            token_id=signal.token_id,
            side=signal.side,
            price=price,
            size=size,
        )

    # ── submission ────────────────────────────────────────────────────

    async def _submit_order(self, order: Order, signal: Signal) -> None:
        """Submit order to Polymarket and track result."""
        submit_start = time.time_ns()

        try:
            result = await self._poly.place_order(
                token_id=order.token_id,
                side=order.side,
                price=order.price,
                size=order.size,
            )
            exchange_id = result.get("orderID", result.get("id", ""))
            order.mark_submitted(exchange_id)

            self._pending_orders[order.order_id] = order
            self._total_submitted += 1

            logger.info(
                "ORDER SUBMITTED: %s %s price=%.4f size=%.2f "
                "latency=%.1fms edge=%.4f",
                order.side.value,
                order.token_id[:12],
                order.price,
                order.size,
                order.latency_ms,
                signal.edge,
            )

            # Optimistic fill tracking: assume GTC orders fill quickly
            # on Polymarket CLOB.  In production, you'd poll or subscribe
            # to order-status updates.
            asyncio.create_task(self._track_order(order, signal))

        except Exception as exc:
            order.status = OrderStatus.REJECTED
            self._total_rejected += 1
            logger.error(
                "ORDER REJECTED: %s %s — %s",
                order.side.value,
                order.token_id[:12],
                exc,
            )

    async def _track_order(self, order: Order, signal: Signal) -> None:
        """
        Poll for order fill status.

        In production, this would subscribe to a WebSocket fill feed.
        Here we poll with exponential back-off up to a timeout.
        """
        timeout_s = 30.0
        start = time.monotonic()
        poll_interval = 0.5

        while time.monotonic() - start < timeout_s:
            await asyncio.sleep(poll_interval)
            try:
                open_orders = await self._poly.get_open_orders()
                still_open = any(
                    o.get("orderID") == order.exchange_order_id
                    or o.get("id") == order.exchange_order_id
                    for o in open_orders
                )
                if not still_open:
                    # Assume filled (could also be cancelled — check positions)
                    order.mark_filled(order.price, order.size)
                    self._total_filled += 1

                    # Register position with risk manager
                    position = Position(
                        condition_id=order.condition_id,
                        token_id=order.token_id,
                        side=order.side,
                        entry_price=order.fill_price,
                        size=order.fill_size,
                        order_id=order.order_id,
                    )
                    self._risk.record_fill(position)

                    # Log trade
                    self._log_trade(order, signal)

                    self._pending_orders.pop(order.order_id, None)
                    self._filled_orders.append(order)
                    return

            except Exception as exc:
                logger.warning("Order tracking error: %s", exc)

            poll_interval = min(poll_interval * 1.5, 5.0)

        # Timeout — cancel stale order
        logger.warning("Order %s timed out — cancelling", order.order_id[:8])
        try:
            if order.exchange_order_id:
                await self._poly.cancel_order(order.exchange_order_id)
        except Exception:
            logger.exception("Failed to cancel stale order")
        order.status = OrderStatus.EXPIRED
        self._pending_orders.pop(order.order_id, None)

    # ── position exit ─────────────────────────────────────────────────

    async def close_position(self, token_id: str, current_price: float) -> float | None:
        """
        Close an open position by submitting the opposite side.

        Returns realised P&L or None on failure.
        """
        pos = self._risk.state.open_positions.get(token_id)
        if pos is None:
            return None

        exit_side = Side.SELL if pos.side == Side.BUY else Side.BUY
        try:
            await self._poly.place_order(
                token_id=token_id,
                side=exit_side,
                price=current_price,
                size=pos.size,
            )
            if pos.side == Side.BUY:
                pnl = (current_price - pos.entry_price) * pos.size
            else:
                pnl = (pos.entry_price - current_price) * pos.size

            self._risk.record_close(token_id, pnl, pos.size)
            logger.info(
                "POSITION CLOSED: %s pnl=%.4f size=%.2f",
                token_id[:12],
                pnl,
                pos.size,
            )
            return pnl

        except Exception:
            logger.exception("Failed to close position %s", token_id[:12])
            return None

    # ── trade logging ─────────────────────────────────────────────────

    def _log_trade(self, order: Order, signal: Signal) -> None:
        """Append a structured trade record to the JSONL trade log."""
        record = {
            "type": "entry",
            "ts": time.time(),
            "order_id": order.order_id,
            "exchange_id": order.exchange_order_id,
            "condition_id": order.condition_id,
            "token_id": order.token_id,
            "side": order.side.value,
            "price": order.price,
            "size": order.size,
            "fill_price": order.fill_price,
            "fill_size": order.fill_size,
            "latency_ms": order.latency_ms,
            "edge": signal.edge,
            "strength": signal.strength.name,
            "regime": signal.regime.name,
            "pred_return": signal.prediction.predicted_return,
            "pred_confidence": signal.prediction.confidence,
            # Probability model analytics
            "p_up": signal.p_up,
            "outcome": signal.outcome,
            "seconds_to_expiry": signal.seconds_to_expiry,
            "btc_volatility": signal.btc_volatility,
        }
        try:
            self._trade_log.parent.mkdir(parents=True, exist_ok=True)
            with open(self._trade_log, "a") as f:
                f.write(json.dumps(record) + "\n")
        except Exception:
            logger.exception("Trade log write failed")

    # ── emergency ─────────────────────────────────────────────────────

    async def cancel_all_orders(self) -> None:
        """Emergency: cancel everything on the exchange."""
        logger.warning("EMERGENCY: cancelling all orders")
        try:
            await self._poly.cancel_all()
        except Exception:
            logger.exception("Failed to cancel all orders")
        self._pending_orders.clear()

    # ── stats ─────────────────────────────────────────────────────────

    def stats(self) -> dict:
        return {
            "submitted": self._total_submitted,
            "filled": self._total_filled,
            "rejected": self._total_rejected,
            "risk_blocked": self._total_risk_blocked,
            "pending": len(self._pending_orders),
            "daily_pnl": self._risk.daily_pnl.realized_pnl,
            "win_rate": self._risk.daily_pnl.win_rate,
            "total_volume": self._risk.daily_pnl.total_volume,
        }
