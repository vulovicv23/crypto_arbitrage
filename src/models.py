"""
Core domain models shared across the bot.

All timestamps are Unix epoch **nanoseconds** (int) for latency-critical paths
and converted to human-readable only at log boundaries.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum, auto


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class Side(Enum):
    BUY = "BUY"
    SELL = "SELL"


class TokenOutcome(Enum):
    """Which side of a binary market a token represents."""

    YES = "YES"  # BTC goes UP
    NO = "NO"  # BTC goes DOWN


class OrderStatus(Enum):
    PENDING = auto()
    SUBMITTED = auto()
    FILLED = auto()
    PARTIALLY_FILLED = auto()
    CANCELLED = auto()
    REJECTED = auto()
    EXPIRED = auto()


class MarketRegime(Enum):
    """Detected via EMA crossover + volatility."""

    TRENDING_UP = auto()
    TRENDING_DOWN = auto()
    SIDEWAYS = auto()


class SignalStrength(Enum):
    WEAK = auto()  # edge just above threshold
    MODERATE = auto()  # edge 1.5–2.5× threshold
    STRONG = auto()  # edge > 2.5× threshold


# ---------------------------------------------------------------------------
# Price / Prediction
# ---------------------------------------------------------------------------


def _now_ns() -> int:
    return time.time_ns()


@dataclass(slots=True)
class PriceTick:
    """A single price observation from any source."""

    source: str
    price: float
    timestamp_ns: int = field(default_factory=_now_ns)
    volume: float = 0.0


@dataclass(slots=True)
class Prediction:
    """A directional prediction for BTC price at some horizon."""

    source: str
    predicted_price: float
    current_price: float
    horizon_s: int  # e.g. 900 for 15-min
    confidence: float  # 0.0–1.0
    timestamp_ns: int = field(default_factory=_now_ns)

    @property
    def predicted_return(self) -> float:
        """Signed fractional return expected."""
        if self.current_price == 0:
            return 0.0
        return (self.predicted_price - self.current_price) / self.current_price

    @property
    def direction(self) -> Side:
        return Side.BUY if self.predicted_return > 0 else Side.SELL


# ---------------------------------------------------------------------------
# Polymarket Specific
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class PolymarketBook:
    """Snapshot of a Polymarket order book for one outcome."""

    condition_id: str
    token_id: str
    best_bid: float
    best_ask: float
    mid_price: float
    spread: float
    timestamp_ns: int = field(default_factory=_now_ns)

    @property
    def is_valid(self) -> bool:
        return self.best_bid > 0 and self.best_ask > self.best_bid


@dataclass(frozen=True, slots=True)
class MarketContext:
    """Metadata about a discovered binary market, passed to the strategy.

    Carries the YES/NO token mapping and expiry information so the
    strategy can compute probability-based edges.
    """

    condition_id: str
    yes_token_id: str  # token for "BTC goes UP"
    no_token_id: str  # token for "BTC goes DOWN"
    end_date_ns: int  # resolution time as epoch nanoseconds
    timeframe_seconds: int  # 300 (5m), 900 (15m), 3600 (1h), 14400 (4h)
    asset: str  # e.g. "BTC"

    def seconds_remaining(self) -> float:
        """Seconds until this market resolves."""
        remaining = (self.end_date_ns - time.time_ns()) / 1_000_000_000
        return max(remaining, 0.0)

    def token_outcome(self, token_id: str) -> TokenOutcome | None:
        """Return which outcome a token represents, or None if unrecognised."""
        if token_id == self.yes_token_id:
            return TokenOutcome.YES
        if token_id == self.no_token_id:
            return TokenOutcome.NO
        return None


# ---------------------------------------------------------------------------
# Trading
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Signal:
    """An actionable trade signal produced by the strategy."""

    condition_id: str
    token_id: str
    side: Side
    edge: float  # P(outcome) - market_price [probability space]
    strength: SignalStrength
    regime: MarketRegime
    prediction: Prediction
    book: PolymarketBook
    timestamp_ns: int = field(default_factory=_now_ns)
    # Analytics fields (for logging and post-trade analysis)
    p_up: float = 0.0  # estimated P(BTC goes up)
    outcome: str = ""  # "YES" or "NO" — which token we're buying
    seconds_to_expiry: float = 0.0  # time remaining when signal was generated
    btc_volatility: float = 0.0  # BTC return volatility used in computation
    size_multiplier: float = 1.0  # expiry-bucket sizing multiplier


@dataclass(slots=True)
class Order:
    """An order to be submitted to Polymarket CLOB."""

    order_id: str  # local UUID
    condition_id: str
    token_id: str
    side: Side
    price: float
    size: float  # in USDC
    status: OrderStatus = OrderStatus.PENDING
    exchange_order_id: str | None = None
    created_ns: int = field(default_factory=_now_ns)
    submitted_ns: int = 0
    filled_ns: int = 0
    fill_price: float = 0.0
    fill_size: float = 0.0
    latency_ms: float = 0.0  # detection-to-submit latency

    def mark_submitted(self, exchange_id: str) -> None:
        self.status = OrderStatus.SUBMITTED
        self.exchange_order_id = exchange_id
        self.submitted_ns = time.time_ns()
        self.latency_ms = (self.submitted_ns - self.created_ns) / 1_000_000

    def mark_filled(self, fill_price: float, fill_size: float) -> None:
        self.status = OrderStatus.FILLED
        self.fill_price = fill_price
        self.fill_size = fill_size
        self.filled_ns = time.time_ns()


@dataclass(slots=True)
class Position:
    """Tracks an open position."""

    condition_id: str
    token_id: str
    side: Side
    entry_price: float
    size: float
    opened_ns: int = field(default_factory=_now_ns)
    unrealized_pnl: float = 0.0
    order_id: str = ""  # Links to Order.order_id for resolution tracking

    def update_pnl(self, current_price: float) -> None:
        if self.side == Side.BUY:
            self.unrealized_pnl = (current_price - self.entry_price) * self.size
        else:
            self.unrealized_pnl = (self.entry_price - current_price) * self.size


@dataclass
class DailyPnL:
    """Running daily P&L tracker."""

    date: str = ""
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    total_volume: float = 0.0
    max_drawdown: float = 0.0
    peak_pnl: float = 0.0

    @property
    def win_rate(self) -> float:
        if self.total_trades == 0:
            return 0.0
        return self.winning_trades / self.total_trades

    def record_trade(self, pnl: float, volume: float) -> None:
        self.realized_pnl += pnl
        self.total_volume += volume
        self.total_trades += 1
        if pnl > 0:
            self.winning_trades += 1
        else:
            self.losing_trades += 1
        # Track drawdown
        if self.realized_pnl > self.peak_pnl:
            self.peak_pnl = self.realized_pnl
        dd = self.peak_pnl - self.realized_pnl
        if dd > self.max_drawdown:
            self.max_drawdown = dd
