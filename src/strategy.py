"""
Trading strategy engine.

Consumes Predictions and PolymarketBook snapshots, detects actionable
edges, classifies the market regime, and emits Signal objects.

Key logic (probability-based edge computation):
  1. Estimate P(BTC goes up) using a normal CDF model:
       P(up) = Phi(predicted_return / (vol * sqrt(T_remaining))) * confidence
  2. For each binary contract token (YES/NO):
       YES token fair_value = P(up)
       NO  token fair_value = 1 - P(up)
  3. Edge is computed against the EXECUTION price (best_ask for BUY),
     not the mid-price. This ensures the edge accounts for the full
     cost of crossing the spread:
       edge = fair_value - best_ask
  4. Only BUY tokens with positive edge above threshold.
  5. Optionally place LIMIT orders at a better price when the spread
     is wide (maker mode): limit_price = fair_value - min_edge_threshold.
  6. Classify signal strength from edge magnitude.
  7. Emit Signal into signal_queue for the order manager.

The probability model accounts for:
  - BTC price volatility (std of recent tick returns)
  - Time to resolution (sqrt(T) scaling — probability sharpens near expiry)
  - Prediction confidence (dampens z-score toward P=0.5)
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from math import erf, sqrt

import numpy as np

from config import StrategyConfig
from src.models import (
    MarketContext,
    MarketRegime,
    PolymarketBook,
    Prediction,
    Side,
    Signal,
    SignalStrength,
    TokenOutcome,
)

logger = logging.getLogger(__name__)

# Minimum seconds before expiry to consider trading a market.
# Below this, order fills become unreliable.
_MIN_SECONDS_TO_TRADE = 5.0

# Clamp probability to avoid degenerate edges (0 or 1).
_P_CLAMP_MIN = 0.01
_P_CLAMP_MAX = 0.99

# Skip books where the mid-price is already at an extreme.
_MID_PRICE_FLOOR = 0.01
_MID_PRICE_CEIL = 0.99

# Minimum number of BTC price observations for volatility estimation.
_MIN_VOL_OBSERVATIONS = 10


class StrategyEngine:
    """
    Stateful strategy that fuses BTC price predictions with Polymarket
    order-book state and emits probability-based trade signals.

    For binary "BTC Up/Down" contracts:
      - YES token = bet that BTC goes UP before expiry
      - NO  token = bet that BTC goes DOWN before expiry
      - Token best_ask = actual cost to enter a BUY position

    The strategy estimates P(BTC up) independently, compares to the
    execution price (best_ask for BUY), and only trades when there is
    positive edge AFTER accounting for the spread cost. Markets with
    spreads wider than max_spread are skipped entirely.

    In maker mode, limit orders are placed inside the spread at a price
    that guarantees min_edge if filled, improving execution vs taker.
    """

    def __init__(
        self,
        config: StrategyConfig,
        prediction_queue: asyncio.Queue[Prediction],
        signal_queue: asyncio.Queue[Signal],
    ):
        self._cfg = config
        self._pred_queue = prediction_queue
        self._signal_queue = signal_queue

        # Latest book snapshots keyed by token_id
        self._books: dict[str, PolymarketBook] = {}

        # Token-ID → MarketContext mapping (set by discovery via main.py)
        self._token_to_market: dict[str, MarketContext] = {}

        # Backward-compatible token → condition mapping (for static mode)
        self._token_to_condition: dict[str, str] = {}

        # Price history for regime detection (mid-prices from contract books)
        self._price_history: deque[float] = deque(
            maxlen=max(config.ema_slow_span * 2, config.volatility_window * 2)
        )

        # BTC price history for volatility estimation (from predictions)
        self._btc_price_history: deque[float] = deque(
            maxlen=config.volatility_window * 2
        )

        # EMA state for regime detection
        self._ema_fast: float | None = None
        self._ema_slow: float | None = None
        self._regime = MarketRegime.SIDEWAYS

        # Stats
        self._signals_emitted = 0

    # ── external setters ──────────────────────────────────────────────

    def set_market_contexts(self, contexts: dict[str, MarketContext]) -> None:
        """Set token_id → MarketContext mapping.

        Called by main.py when market discovery updates active markets.
        Each token knows its outcome (YES/NO), expiry time, and timeframe.
        """
        self._token_to_market = contexts
        # Also maintain the simple condition mapping for backward compat
        self._token_to_condition = {
            tid: ctx.condition_id for tid, ctx in contexts.items()
        }

    def set_token_mapping(self, mapping: dict[str, str]) -> None:
        """Set token_id → condition_id mapping (static mode fallback).

        Deprecated: use set_market_contexts() for discovery mode.
        Maintained for backward compatibility with static condition IDs.
        """
        self._token_to_condition = mapping

    async def on_book_update(self, book: PolymarketBook) -> None:
        """Called by the Polymarket WS handler on every book change."""
        self._books[book.token_id] = book
        if book.mid_price > 0:
            self._price_history.append(book.mid_price)
            self._update_regime(book.mid_price)

    # ── main loop ─────────────────────────────────────────────────────

    async def run(self) -> None:
        """Consume predictions and evaluate signals."""
        logger.info("Strategy engine started")
        while True:
            prediction = await self._pred_queue.get()
            try:
                signals = self._evaluate(prediction)
                for sig in signals:
                    try:
                        self._signal_queue.put_nowait(sig)
                        self._signals_emitted += 1
                    except asyncio.QueueFull:
                        try:
                            self._signal_queue.get_nowait()
                        except asyncio.QueueEmpty:
                            pass
                        self._signal_queue.put_nowait(sig)
            except Exception:
                logger.exception("Strategy evaluation error")

    # ── core evaluation ───────────────────────────────────────────────

    def _evaluate(self, prediction: Prediction) -> list[Signal]:
        """Evaluate all tracked books against the current prediction.

        For each binary contract token:
          1. Look up MarketContext to determine YES/NO outcome.
          2. Reject illiquid books (spread > max_spread).
          3. Compute P(BTC goes up) via CDF model.
          4. Derive fair value: YES → P(up), NO → 1-P(up).
          5. Edge = fair_value - best_ask (execution price, not mid).
          6. Only BUY tokens with positive edge above threshold.
          7. In maker mode: place limit order at fair_value - min_edge
             (inside the spread) instead of crossing at best_ask.
        """
        signals: list[Signal] = []

        # Track BTC price for volatility estimation
        if prediction.current_price > 0:
            self._btc_price_history.append(prediction.current_price)

        btc_vol = self._btc_return_volatility()

        # Cache P(up) per condition to avoid redundant computation
        # (YES and NO tokens for the same market share the same P(up))
        p_up_cache: dict[str, float] = {}

        for token_id, book in self._books.items():
            if not book.is_valid:
                continue

            # Look up market context for this token
            market_ctx = self._token_to_market.get(token_id)
            if market_ctx is None:
                # No market context → static mode fallback (no probability model)
                continue

            # Determine if this is a YES or NO token
            outcome = market_ctx.token_outcome(token_id)
            if outcome is None:
                continue

            # Skip near-expiry markets (unreliable fills)
            seconds_left = market_ctx.seconds_remaining()
            if seconds_left < _MIN_SECONDS_TO_TRADE:
                continue

            # Skip fully-priced-in books
            if book.mid_price <= _MID_PRICE_FLOOR or book.mid_price >= _MID_PRICE_CEIL:
                continue

            # Skip illiquid markets with wide spreads
            if book.spread > self._cfg.max_spread:
                continue

            # Compute P(BTC goes up) — cached per condition
            cid = market_ctx.condition_id
            if cid not in p_up_cache:
                p_up_cache[cid] = self._compute_p_up(prediction, market_ctx, btc_vol)
            p_up = p_up_cache[cid]

            # Fair value for this specific token
            if outcome == TokenOutcome.YES:
                fair_value = p_up
            else:
                fair_value = 1.0 - p_up

            # Edge = fair_value minus EXECUTION price (not mid).
            # For BUY: execution price = best_ask (what we'd actually pay).
            # This ensures the edge accounts for the full cost of the spread.
            edge = fair_value - book.best_ask

            # Periodic debug: log what the strategy is computing (1 per 500 evals)
            if hasattr(self, "_eval_count"):
                self._eval_count += 1
            else:
                self._eval_count = 0
            if self._eval_count % 500 == 0:
                logger.info(
                    "EVAL_SAMPLE: %s %s p_up=%.4f fair=%.4f "
                    "bid=%.4f ask=%.4f mid=%.4f spread=%.4f edge=%.4f ttl=%.0fs",
                    outcome.value,
                    token_id[:12],
                    p_up,
                    fair_value,
                    book.best_bid,
                    book.best_ask,
                    book.mid_price,
                    book.spread,
                    edge,
                    seconds_left,
                )

            # Only BUY underpriced tokens (positive edge after spread cost).
            if edge <= 0:
                continue

            # Get edge thresholds (bucketed by time-to-expiry if enabled)
            min_edge, max_edge, size_mult = self._get_edge_params(seconds_left)

            # Threshold checks
            if edge < min_edge:
                continue
            if edge > max_edge:
                logger.debug(
                    "Edge %.4f exceeds max threshold (%.4f) — skipping (stale?)",
                    edge,
                    max_edge,
                )
                continue

            strength = self._classify_strength(edge, min_edge)
            condition_id = market_ctx.condition_id

            signal = Signal(
                condition_id=condition_id,
                token_id=token_id,
                side=Side.BUY,
                edge=edge,
                strength=strength,
                regime=self._regime,
                prediction=prediction,
                book=book,
                # Analytics
                p_up=p_up,
                outcome=outcome.value,
                seconds_to_expiry=seconds_left,
                btc_volatility=btc_vol,
                size_multiplier=size_mult,
            )
            signals.append(signal)

            logger.info(
                "SIGNAL: BUY %s %s edge=%.4f p_up=%.4f fair=%.4f "
                "ask=%.4f spread=%.4f strength=%s regime=%s ttl=%.0fs vol=%.6f",
                outcome.value,
                token_id[:12],
                edge,
                p_up,
                fair_value,
                book.best_ask,
                book.spread,
                strength.name,
                self._regime.name,
                seconds_left,
                btc_vol,
            )

        return signals

    # ── probability model ─────────────────────────────────────────────

    def _compute_p_up(
        self,
        prediction: Prediction,
        market_ctx: MarketContext,
        btc_volatility: float,
    ) -> float:
        """Estimate P(BTC goes up from now until market resolution).

        Uses a normal CDF model::

            P(up) = Phi(z)
            z = (predicted_return / (vol * sqrt(seconds_remaining))) * confidence

        Under a log-normal random walk, the return over a time period T
        is approximately: R ~ N(mu*T, sigma^2 * T).  We use the predicted
        return as our estimate of mu*T and scale uncertainty by sqrt(T).

        As time runs out (T → 0):
          - If BTC is clearly moving in one direction, z explodes → P → 0 or 1.
          - This is the correct time-decay behavior for binary contracts.

        Args:
            prediction: Current BTC price prediction.
            market_ctx: Market metadata (expiry, timeframe).
            btc_volatility: Std dev of BTC tick-to-tick returns.

        Returns:
            P(BTC goes up) clamped to [0.01, 0.99].
        """
        pred_return = prediction.predicted_return
        seconds_left = market_ctx.seconds_remaining()

        # Edge case: market has expired or is about to
        if seconds_left <= 0:
            if pred_return > 0:
                return _P_CLAMP_MAX
            if pred_return < 0:
                return _P_CLAMP_MIN
            return 0.50

        # Edge case: zero or near-zero volatility (insufficient data)
        if btc_volatility < 1e-10:
            # Conservative: slight bias toward prediction direction
            # but stay close to 0.5 (high uncertainty)
            p = 0.5 + pred_return * 10.0
            return max(_P_CLAMP_MIN, min(_P_CLAMP_MAX, p))

        # Scale volatility to the remaining time window.
        # For BTC with ~1s ticks, if vol = 0.0001 per tick, over 300s:
        # period_vol = 0.0001 * sqrt(300) ≈ 0.0017
        scaled_vol = btc_volatility * sqrt(seconds_left)

        # Avoid division by zero (should not happen given the check above)
        if scaled_vol < 1e-15:
            return 0.50

        # z-score: how many std devs is the predicted return from zero?
        z = pred_return / scaled_vol

        # Dampen by prediction confidence.
        # Low confidence → z pulled toward 0 → P pulled toward 0.5.
        z *= prediction.confidence

        # P(BTC goes up) = Phi(z)
        p_up = self._normal_cdf(z)

        return max(_P_CLAMP_MIN, min(_P_CLAMP_MAX, p_up))

    @staticmethod
    def _normal_cdf(x: float) -> float:
        """Standard normal CDF.  Equivalent to scipy.stats.norm.cdf(x)."""
        return 0.5 * (1.0 + erf(x / sqrt(2.0)))

    def _btc_return_volatility(self) -> float:
        """Standard deviation of recent BTC tick-to-tick returns.

        Uses the BTC price observations extracted from predictions
        (not contract mid-prices, which measure market sentiment).

        Returns:
            Std dev of returns, or 0.0 if insufficient data.
        """
        if len(self._btc_price_history) < _MIN_VOL_OBSERVATIONS:
            return 0.0
        prices = np.array(list(self._btc_price_history)[-self._cfg.volatility_window :])
        if len(prices) < 2:
            return 0.0
        returns = np.diff(prices) / prices[:-1]
        return float(np.std(returns))

    # ── regime detection ──────────────────────────────────────────────

    def _update_regime(self, price: float) -> None:
        """Update EMA-based trend/sideways classification.

        Uses contract mid-prices (not BTC prices) to detect whether
        the prediction market itself is trending.
        """
        alpha_fast = 2.0 / (self._cfg.ema_fast_span + 1)
        alpha_slow = 2.0 / (self._cfg.ema_slow_span + 1)

        if self._ema_fast is None:
            self._ema_fast = price
            self._ema_slow = price
        else:
            self._ema_fast = alpha_fast * price + (1 - alpha_fast) * self._ema_fast
            self._ema_slow = alpha_slow * price + (1 - alpha_slow) * self._ema_slow

        # Volatility check
        vol = self._recent_volatility()

        # Classification
        if self._ema_fast is None or self._ema_slow is None:
            self._regime = MarketRegime.SIDEWAYS
            return

        diff_pct = (
            (self._ema_fast - self._ema_slow) / self._ema_slow
            if self._ema_slow != 0
            else 0
        )

        if vol < 0.001:
            # Very low vol → sideways regardless of EMA
            self._regime = MarketRegime.SIDEWAYS
        elif diff_pct > 0.0005:
            self._regime = MarketRegime.TRENDING_UP
        elif diff_pct < -0.0005:
            self._regime = MarketRegime.TRENDING_DOWN
        else:
            self._regime = MarketRegime.SIDEWAYS

    def _recent_volatility(self) -> float:
        """Volatility of recent contract mid-prices (for regime detection)."""
        if len(self._price_history) < 2:
            return 0.0
        prices = np.array(list(self._price_history)[-self._cfg.volatility_window :])
        if len(prices) < 2:
            return 0.0
        returns = np.diff(prices) / prices[:-1]
        return float(np.std(returns))

    # ── expiry-bucketed thresholds ─────────────────────────────────────

    def _get_edge_params(self, seconds_left: float) -> tuple[float, float, float]:
        """Return (min_edge, max_edge, size_multiplier) for the expiry bucket.

        When ``expiry_buckets_enabled`` is False, returns the flat config
        thresholds with multiplier 1.0.

        Buckets:
          - Near (<near_expiry_s): aggressive — lower thresholds, higher sizing.
            Binary options sharpen near expiry so edges are real and large.
          - Mid (near..far): standard thresholds.
          - Far (>far_expiry_s): conservative — higher thresholds, lower sizing.
            More time = more uncertainty = need bigger edge to justify trade.
        """
        if not self._cfg.expiry_buckets_enabled:
            return (
                self._cfg.min_edge_threshold,
                self._cfg.max_edge_threshold,
                1.0,
            )

        if seconds_left < self._cfg.near_expiry_s:
            return (
                self._cfg.near_min_edge,
                self._cfg.near_max_edge,
                self._cfg.near_size_mult,
            )
        elif seconds_left > self._cfg.far_expiry_s:
            return (
                self._cfg.far_min_edge,
                self._cfg.far_max_edge,
                self._cfg.far_size_mult,
            )
        else:
            # Mid bucket — use standard thresholds
            return (
                self._cfg.min_edge_threshold,
                self._cfg.max_edge_threshold,
                1.0,
            )

    # ── helpers ───────────────────────────────────────────────────────

    def _classify_strength(
        self, abs_edge: float, min_edge: float | None = None
    ) -> SignalStrength:
        """Classify edge into signal strength tiers.

        Thresholds scale with min_edge (bucket-aware):
          WEAK:     1.0× – 1.5× min_edge
          MODERATE: 1.5× – 2.5× min_edge
          STRONG:   > 2.5× min_edge
        """
        t = min_edge if min_edge is not None else self._cfg.min_edge_threshold
        if abs_edge < 1.5 * t:
            return SignalStrength.WEAK
        elif abs_edge < 2.5 * t:
            return SignalStrength.MODERATE
        else:
            return SignalStrength.STRONG

    @property
    def current_regime(self) -> MarketRegime:
        return self._regime

    @property
    def signals_emitted(self) -> int:
        return self._signals_emitted
