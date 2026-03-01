"""
Synthetic order-book generator for dry-run mode.

When the bot runs without valid Polymarket API credentials, the strategy
engine has no book data and therefore can never evaluate edges.  This
module generates realistic synthetic ``PolymarketBook`` snapshots so
the entire pipeline (strategy -> risk -> paper orders) can run.

How it works:
  1. Listens to the same ``prediction_queue`` that feeds the strategy.
  2. Maintains a **slow EMA** of BTC price (the "market's view").
  3. Computes a market-implied P(BTC up) using a CDF model similar to
     the strategy's, but based on the slower EMA — this introduces
     natural lag that creates exploitable edges.
  4. Adds configurable Gaussian noise + spread to produce bid/ask.
  5. Feeds ``PolymarketBook`` objects to the strategy via the same
     ``on_book_update`` callback the real WS would use.

The result: the strategy detects edges when its fast prediction diverges
from the slower synthetic market, which is a realistic simulation of the
latency-arbitrage the bot is designed for.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections import deque
from math import erf, sqrt

from src.models import MarketContext, PolymarketBook, Prediction, TokenOutcome

logger = logging.getLogger(__name__)

# Minimum data points before we start generating books.
_MIN_PRICE_OBSERVATIONS = 5


class SyntheticBookGenerator:
    """Generates synthetic Polymarket books from BTC price predictions.

    Args:
        prediction_queue: Shared queue of ``Prediction`` objects (tapped,
            not consumed — we copy from the strategy's queue).
        book_callback: ``async def handler(book: PolymarketBook)`` — the
            strategy's ``on_book_update`` method.
        update_interval: Seconds between synthetic book emissions.
        ema_alpha: Smoothing factor for the market's slow EMA (lower = more
            lag, which creates bigger edges).  Default 0.05 means the
            synthetic market adapts ~20x slower than tick-by-tick.
        noise_std: Std dev of Gaussian noise added to mid-price (probability
            space, e.g. 0.02 = 2%).
        spread_pct: Bid-ask spread as fraction of mid-price (e.g. 0.04 = 4%).
        volatility_window: Number of price ticks for vol estimation.
    """

    def __init__(
        self,
        book_callback,
        *,
        update_interval: float = 1.0,
        ema_alpha: float = 0.05,
        noise_std: float = 0.02,
        spread_pct: float = 0.04,
        volatility_window: int = 60,
    ):
        self._callback = book_callback
        self._update_interval = update_interval
        self._ema_alpha = ema_alpha
        self._noise_std = noise_std
        self._spread_pct = spread_pct
        self._volatility_window = volatility_window

        # Market contexts: token_id -> MarketContext
        self._token_to_market: dict[str, MarketContext] = {}

        # BTC price tracking
        self._btc_prices: deque[float] = deque(maxlen=volatility_window * 2)
        self._market_ema_price: float | None = None  # slow EMA (market's view)

        # Latest prediction (set by feed method)
        self._latest_prediction: Prediction | None = None

        self._running = False
        self._books_emitted = 0

    # ── external setters ──────────────────────────────────────────────

    def set_market_contexts(self, contexts: dict[str, MarketContext]) -> None:
        """Update the token_id -> MarketContext mapping.

        Called by the bot whenever market discovery updates.
        """
        self._token_to_market = contexts

    def feed_prediction(self, prediction: Prediction) -> None:
        """Accept a new prediction to update the slow EMA.

        Called from a queue tap in the bot's pipeline.
        """
        self._latest_prediction = prediction

        if prediction.current_price > 0:
            self._btc_prices.append(prediction.current_price)

            # Update slow EMA
            if self._market_ema_price is None:
                self._market_ema_price = prediction.current_price
            else:
                self._market_ema_price = (
                    self._ema_alpha * prediction.current_price
                    + (1 - self._ema_alpha) * self._market_ema_price
                )

    # ── main loop ─────────────────────────────────────────────────────

    async def run(self) -> None:
        """Periodically generate synthetic books for all tracked tokens."""
        self._running = True
        logger.info(
            "Synthetic book generator started (interval=%.1fs, "
            "ema_alpha=%.3f, noise=%.3f, spread=%.3f)",
            self._update_interval,
            self._ema_alpha,
            self._noise_std,
            self._spread_pct,
        )

        while self._running:
            await asyncio.sleep(self._update_interval)
            try:
                await self._generate_books()
            except Exception:
                logger.exception("Synthetic book generation error")

    def stop(self) -> None:
        self._running = False

    # ── book generation ───────────────────────────────────────────────

    async def _generate_books(self) -> None:
        """Generate and emit synthetic books for all tracked tokens."""
        if self._latest_prediction is None:
            return
        if self._market_ema_price is None:
            return
        if len(self._btc_prices) < _MIN_PRICE_OBSERVATIONS:
            return

        prediction = self._latest_prediction
        btc_vol = self._btc_return_volatility()

        # Compute the market's implied P(BTC up) using the slow EMA price.
        # This is intentionally "stale" compared to what the strategy computes
        # from the latest prediction, creating the edge we're simulating.
        market_return = (
            (self._market_ema_price - prediction.current_price)
            / prediction.current_price
            if prediction.current_price > 0
            else 0.0
        )

        # Group tokens by condition to avoid redundant P(up) computation
        seen_conditions: dict[str, float] = {}  # condition_id -> market_p_up

        for token_id, market_ctx in self._token_to_market.items():
            cid = market_ctx.condition_id
            seconds_left = market_ctx.seconds_remaining()
            if seconds_left <= 0:
                continue

            # Compute market's P(up) per condition (cached)
            if cid not in seen_conditions:
                seen_conditions[cid] = self._compute_market_p_up(
                    prediction,
                    market_ctx,
                    btc_vol,
                )

            market_p_up = seen_conditions[cid]

            # Determine mid-price for this specific token
            outcome = market_ctx.token_outcome(token_id)
            if outcome is None:
                continue

            if outcome == TokenOutcome.YES:
                base_mid = market_p_up
            else:
                base_mid = 1.0 - market_p_up

            # Add noise to simulate market microstructure
            noise = random.gauss(0, self._noise_std)
            mid_price = max(0.02, min(0.98, base_mid + noise))

            # Compute bid/ask from mid with spread
            half_spread = mid_price * self._spread_pct / 2
            best_bid = max(0.01, mid_price - half_spread)
            best_ask = min(0.99, mid_price + half_spread)

            book = PolymarketBook(
                condition_id=cid,
                token_id=token_id,
                best_bid=round(best_bid, 4),
                best_ask=round(best_ask, 4),
                mid_price=round(mid_price, 4),
                spread=round(best_ask - best_bid, 4),
            )

            await self._callback(book)
            self._books_emitted += 1

    # ── probability model (market's slower view) ──────────────────────

    def _compute_market_p_up(
        self,
        prediction: Prediction,
        market_ctx: MarketContext,
        btc_vol: float,
    ) -> float:
        """Compute the synthetic market's implied P(BTC up).

        Uses the slow EMA price instead of the latest price,
        so it naturally lags behind the strategy's computation.
        """
        if self._market_ema_price is None or prediction.current_price <= 0:
            return 0.5

        # The "market" thinks the predicted return is based on its slow EMA
        # rather than the latest tick price.
        market_pred_return = (
            self._market_ema_price - prediction.current_price
        ) / prediction.current_price

        seconds_left = market_ctx.seconds_remaining()
        if seconds_left <= 0:
            return 0.5

        if btc_vol < 1e-10:
            p = 0.5 + market_pred_return * 5.0
            return max(0.01, min(0.99, p))

        scaled_vol = btc_vol * sqrt(seconds_left)
        if scaled_vol < 1e-15:
            return 0.5

        z = market_pred_return / scaled_vol
        # The market uses lower confidence (it's slower/noisier)
        z *= 0.7

        p_up = 0.5 * (1.0 + erf(z / sqrt(2.0)))
        return max(0.01, min(0.99, p_up))

    def _btc_return_volatility(self) -> float:
        """Std dev of recent BTC tick-to-tick returns."""
        if len(self._btc_prices) < _MIN_PRICE_OBSERVATIONS:
            return 0.0
        prices = list(self._btc_prices)[-self._volatility_window :]
        if len(prices) < 2:
            return 0.0
        returns = []
        for i in range(1, len(prices)):
            if prices[i - 1] > 0:
                returns.append((prices[i] - prices[i - 1]) / prices[i - 1])
        if not returns:
            return 0.0
        mean = sum(returns) / len(returns)
        variance = sum((r - mean) ** 2 for r in returns) / len(returns)
        return variance**0.5

    # ── stats ─────────────────────────────────────────────────────────

    def stats(self) -> dict:
        return {
            "books_emitted": self._books_emitted,
            "tracked_tokens": len(self._token_to_market),
            "market_ema_price": self._market_ema_price,
            "btc_observations": len(self._btc_prices),
        }
