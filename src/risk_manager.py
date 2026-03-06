"""
Risk management module.

Enforces:
  - Per-trade position size limits (max 0.5% of capital).
  - Daily loss limits (2% drawdown → halt).
  - Total exposure caps.
  - Regime-adaptive sizing (smaller in sideways, larger in trends).
  - Cool-down after consecutive losses.
  - Latency budget enforcement.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date

from config import ExecutionConfig, RiskConfig
from src.models import (
    DailyPnL,
    MarketRegime,
    Position,
    Signal,
    SignalStrength,
)

logger = logging.getLogger(__name__)


@dataclass
class RiskState:
    """Mutable risk state tracked across the trading day."""

    capital: float = 0.0
    open_positions: dict[str, Position] = field(default_factory=dict)
    daily_pnl: DailyPnL = field(default_factory=DailyPnL)
    consecutive_losses: int = 0
    cooldown_until: float = 0.0  # monotonic time
    halted: bool = False
    halt_reason: str = ""


class RiskManager:
    """
    Stateful risk gate that approves / rejects signals and computes
    position sizes.
    """

    def __init__(
        self,
        risk_config: RiskConfig,
        exec_config: ExecutionConfig,
        initial_capital: float,
    ):
        self._cfg = risk_config
        self._exec_cfg = exec_config
        self._state = RiskState(capital=initial_capital)
        self._state.daily_pnl.date = str(date.today())

    # ── public interface ──────────────────────────────────────────────

    def check_signal(self, signal: Signal) -> tuple[bool, float, str]:
        """
        Evaluate whether a signal passes all risk checks.

        Returns:
            (approved: bool, position_size: float, reason: str)
        """
        # 0. Reset daily counters if new day
        self._maybe_reset_day()

        # 1. Halted?
        if self._state.halted:
            return False, 0.0, f"HALTED: {self._state.halt_reason}"

        # 2. Cooldown?
        if time.monotonic() < self._state.cooldown_until:
            remaining = self._state.cooldown_until - time.monotonic()
            return False, 0.0, f"Cooldown active ({remaining:.1f}s left)"

        # 3. Daily loss limit
        if self._daily_loss_exceeded():
            self._state.halted = True
            self._state.halt_reason = (
                f"Daily loss limit hit: " f"{self._state.daily_pnl.realized_pnl:.2f}"
            )
            logger.critical(self._state.halt_reason)
            return False, 0.0, self._state.halt_reason

        # 4. Max open positions
        if len(self._state.open_positions) >= self._cfg.max_open_positions:
            return False, 0.0, "Max open positions reached"

        # 5. Per-condition checks: position limit and side lock
        if self._cfg.max_positions_per_condition > 0:
            cond_positions = [
                p
                for p in self._state.open_positions.values()
                if p.condition_id == signal.condition_id
            ]
            # 5a. Max positions per condition
            if len(cond_positions) >= self._cfg.max_positions_per_condition:
                return False, 0.0, "Max positions per condition reached"
            # 5b. Side lock — don't take opposing sides on the same condition
            if cond_positions:
                existing_side = cond_positions[0].side
                if signal.side != existing_side:
                    return False, 0.0, "Side lock: opposing position on same condition"

        # 6. Total exposure
        total_exposure = sum(p.size for p in self._state.open_positions.values())
        max_exposure = self._state.capital * self._cfg.max_total_exposure_pct
        if total_exposure >= max_exposure:
            return False, 0.0, "Total exposure limit reached"

        # 7. Latency budget
        age_ms = (time.time_ns() - signal.timestamp_ns) / 1_000_000
        if age_ms > self._exec_cfg.max_latency_ms:
            return False, 0.0, f"Signal too stale ({age_ms:.1f}ms)"

        # 8. Compute position size
        size = self._compute_size(signal)
        if size <= 0:
            return False, 0.0, "Computed size <= 0"

        # 9. Verify size doesn't exceed remaining exposure budget
        remaining_budget = max_exposure - total_exposure
        size = min(size, remaining_budget)

        return True, size, "OK"

    def record_fill(self, position: Position) -> None:
        """Register a new open position."""
        self._state.open_positions[position.order_id] = position
        logger.info(
            "Position opened: %s %s size=%.2f",
            position.side.value,
            position.token_id[:12],
            position.size,
        )

    def record_close(self, order_id: str, pnl: float, volume: float) -> None:
        """Record a closed trade and update capital with realized PnL."""
        self._state.open_positions.pop(order_id, None)
        self._state.daily_pnl.record_trade(pnl, volume)

        # Track capital so position sizing reflects realized gains/losses
        self._state.capital += pnl

        if pnl < 0:
            self._state.consecutive_losses += 1
            if self._state.consecutive_losses >= self._cfg.cooldown_after_losses:
                self._state.cooldown_until = (
                    time.monotonic() + self._cfg.cooldown_duration_s
                )
                logger.warning(
                    "Cooldown triggered after %d consecutive losses (%.1fs)",
                    self._state.consecutive_losses,
                    self._cfg.cooldown_duration_s,
                )
        elif pnl > 0:
            # Only strictly profitable trades reset the loss streak;
            # break-even trades (pnl == 0) do not reset it.
            self._state.consecutive_losses = 0

    # ── sizing ────────────────────────────────────────────────────────

    def _compute_size(self, signal: Signal) -> float:
        """
        Position size = base × regime_multiplier × strength_multiplier.

        base = capital × max_position_pct
        """
        base = self._state.capital * self._cfg.max_position_pct

        # Regime multiplier — differentiate TRENDING_UP from TRENDING_DOWN
        if signal.regime == MarketRegime.SIDEWAYS:
            regime_mult = self._cfg.sideways_size_multiplier
        elif signal.regime == MarketRegime.TRENDING_UP:
            regime_mult = self._cfg.trending_up_size_multiplier
        else:
            regime_mult = self._cfg.trend_size_multiplier

        # Strength multiplier
        strength_map = {
            SignalStrength.WEAK: self._cfg.weak_strength_multiplier,
            SignalStrength.MODERATE: self._cfg.moderate_strength_multiplier,
            SignalStrength.STRONG: 1.0,
        }
        strength_mult = strength_map.get(signal.strength, 0.5)

        # Confidence scaling from prediction
        confidence_mult = signal.prediction.confidence

        # Expiry-bucket multiplier (from strategy's time-to-expiry bucketing)
        expiry_mult = getattr(signal, "size_multiplier", 1.0)

        size = base * regime_mult * strength_mult * confidence_mult * expiry_mult

        # Floor: never go below $1
        return max(size, 0.0)

    # ── daily checks ──────────────────────────────────────────────────

    def _daily_loss_exceeded(self) -> bool:
        max_loss = self._state.capital * self._cfg.max_daily_loss_pct
        return self._state.daily_pnl.realized_pnl < -max_loss

    def _maybe_reset_day(self) -> None:
        today = str(date.today())
        if self._state.daily_pnl.date != today:
            logger.info(
                "New trading day — resetting daily P&L "
                "(prev: %.2f over %d trades, win-rate: %.1f%%)",
                self._state.daily_pnl.realized_pnl,
                self._state.daily_pnl.total_trades,
                self._state.daily_pnl.win_rate * 100,
            )
            self._state.daily_pnl = DailyPnL(date=today)
            self._state.halted = False
            self._state.halt_reason = ""
            self._state.consecutive_losses = 0

    # ── accessors ─────────────────────────────────────────────────────

    @property
    def state(self) -> RiskState:
        return self._state

    @property
    def is_halted(self) -> bool:
        return self._state.halted

    @property
    def daily_pnl(self) -> DailyPnL:
        return self._state.daily_pnl
