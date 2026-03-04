"""
Tests for the probability-based strategy engine.

Covers:
  - Normal CDF accuracy
  - P(up) computation under various scenarios
  - Edge computation for YES/NO tokens
  - Time decay (sqrt(T) scaling)
  - Confidence dampening
  - Edge cases (expired, zero vol, extreme vol, near-expiry)
  - Signal generation from _evaluate()
  - Regime detection
  - BTC return volatility estimation
"""

from __future__ import annotations

import asyncio
import time

import numpy as np
import pytest

from config import StrategyConfig
from src.models import (
    MarketContext,
    MarketRegime,
    PolymarketBook,
    Prediction,
    Signal,
    SignalStrength,
    TokenOutcome,
)
from src.strategy import (
    StrategyEngine,
    _P_CLAMP_MAX,
    _P_CLAMP_MIN,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(**overrides) -> StrategyConfig:
    """Build a StrategyConfig with optional overrides."""
    defaults = {
        "min_edge_threshold": 0.02,
        "max_edge_threshold": 0.30,
        "prediction_horizon_s": 900,
        "ema_fast_span": 12,
        "ema_slow_span": 26,
        "volatility_window": 60,
        "confidence_scale": True,
    }
    defaults.update(overrides)
    return StrategyConfig(**defaults)


def _make_engine(
    config: StrategyConfig | None = None,
) -> tuple[StrategyEngine, asyncio.Queue[Prediction], asyncio.Queue[Signal]]:
    """Create a StrategyEngine with mock queues."""
    cfg = config or _make_config()
    pred_q: asyncio.Queue[Prediction] = asyncio.Queue()
    sig_q: asyncio.Queue[Signal] = asyncio.Queue()
    engine = StrategyEngine(cfg, pred_q, sig_q)
    return engine, pred_q, sig_q


def _make_market_context(
    condition_id: str = "cond-1",
    yes_token_id: str = "tok-yes",
    no_token_id: str = "tok-no",
    seconds_from_now: float = 300.0,
    timeframe_seconds: int = 300,
    asset: str = "BTC",
) -> MarketContext:
    """Build a MarketContext expiring `seconds_from_now` in the future."""
    end_date_ns = time.time_ns() + int(seconds_from_now * 1_000_000_000)
    return MarketContext(
        condition_id=condition_id,
        yes_token_id=yes_token_id,
        no_token_id=no_token_id,
        end_date_ns=end_date_ns,
        timeframe_seconds=timeframe_seconds,
        asset=asset,
    )


def _make_prediction(
    current_price: float = 100_000.0,
    predicted_price: float = 100_100.0,
    confidence: float = 0.8,
    horizon_s: int = 900,
) -> Prediction:
    return Prediction(
        source="test",
        predicted_price=predicted_price,
        current_price=current_price,
        horizon_s=horizon_s,
        confidence=confidence,
    )


def _make_book(
    token_id: str = "tok-yes",
    condition_id: str = "cond-1",
    mid_price: float = 0.50,
    spread: float = 0.02,
) -> PolymarketBook:
    half_spread = spread / 2
    return PolymarketBook(
        condition_id=condition_id,
        token_id=token_id,
        best_bid=mid_price - half_spread,
        best_ask=mid_price + half_spread,
        mid_price=mid_price,
        spread=spread,
    )


# ---------------------------------------------------------------------------
# Normal CDF Tests
# ---------------------------------------------------------------------------


class TestNormalCDF:
    """Verify the stdlib-based CDF matches expected values."""

    def test_cdf_at_zero(self):
        """Phi(0) = 0.5 exactly."""
        assert StrategyEngine._normal_cdf(0.0) == pytest.approx(0.5, abs=1e-10)

    def test_cdf_positive(self):
        """Phi(1.0) ≈ 0.8413."""
        assert StrategyEngine._normal_cdf(1.0) == pytest.approx(0.8413, abs=1e-3)

    def test_cdf_negative(self):
        """Phi(-1.0) ≈ 0.1587."""
        assert StrategyEngine._normal_cdf(-1.0) == pytest.approx(0.1587, abs=1e-3)

    def test_cdf_symmetry(self):
        """Phi(x) + Phi(-x) = 1."""
        for x in [0.5, 1.0, 2.0, 3.0]:
            assert StrategyEngine._normal_cdf(x) + StrategyEngine._normal_cdf(
                -x
            ) == pytest.approx(1.0, abs=1e-10)

    def test_cdf_extreme_positive(self):
        """Phi(5) ≈ 1.0."""
        assert StrategyEngine._normal_cdf(5.0) > 0.999

    def test_cdf_extreme_negative(self):
        """Phi(-5) ≈ 0.0."""
        assert StrategyEngine._normal_cdf(-5.0) < 0.001

    def test_cdf_two_sigma(self):
        """Phi(2) ≈ 0.9772."""
        assert StrategyEngine._normal_cdf(2.0) == pytest.approx(0.9772, abs=1e-3)


# ---------------------------------------------------------------------------
# P(up) Computation Tests
# ---------------------------------------------------------------------------


class TestComputePUp:
    """Tests for the CDF-based P(BTC goes up) model."""

    def test_positive_return_gives_p_above_half(self):
        """A positive predicted return should give P(up) > 0.5."""
        engine, _, _ = _make_engine()
        pred = _make_prediction(
            current_price=100_000,
            predicted_price=100_500,
            confidence=0.8,
        )
        ctx = _make_market_context(seconds_from_now=300)
        p_up = engine._compute_p_up(pred, ctx, btc_volatility=0.0001)
        assert p_up > 0.5

    def test_negative_return_gives_p_below_half(self):
        """A negative predicted return should give P(up) < 0.5."""
        engine, _, _ = _make_engine()
        pred = _make_prediction(
            current_price=100_000,
            predicted_price=99_500,
            confidence=0.8,
        )
        ctx = _make_market_context(seconds_from_now=300)
        p_up = engine._compute_p_up(pred, ctx, btc_volatility=0.0001)
        assert p_up < 0.5

    def test_zero_return_gives_half(self):
        """No predicted move → P(up) = 0.5."""
        engine, _, _ = _make_engine()
        pred = _make_prediction(
            current_price=100_000,
            predicted_price=100_000,
            confidence=0.8,
        )
        ctx = _make_market_context(seconds_from_now=300)
        p_up = engine._compute_p_up(pred, ctx, btc_volatility=0.0001)
        assert p_up == pytest.approx(0.5, abs=1e-6)

    def test_clamped_to_range(self):
        """P(up) must always be in [P_CLAMP_MIN, P_CLAMP_MAX]."""
        engine, _, _ = _make_engine()
        # Huge positive return → should clamp to 0.99
        pred = _make_prediction(
            current_price=100_000,
            predicted_price=200_000,
            confidence=1.0,
        )
        ctx = _make_market_context(seconds_from_now=10)
        p_up = engine._compute_p_up(pred, ctx, btc_volatility=0.0001)
        assert _P_CLAMP_MIN <= p_up <= _P_CLAMP_MAX

        # Huge negative return → should clamp to 0.01
        pred = _make_prediction(
            current_price=100_000,
            predicted_price=50_000,
            confidence=1.0,
        )
        p_up = engine._compute_p_up(pred, ctx, btc_volatility=0.0001)
        assert _P_CLAMP_MIN <= p_up <= _P_CLAMP_MAX

    def test_confidence_dampening(self):
        """Lower confidence → P(up) closer to 0.5."""
        engine, _, _ = _make_engine()
        ctx = _make_market_context(seconds_from_now=300)

        pred_high = _make_prediction(
            current_price=100_000,
            predicted_price=100_500,
            confidence=0.9,
        )
        pred_low = _make_prediction(
            current_price=100_000,
            predicted_price=100_500,
            confidence=0.1,
        )

        p_high = engine._compute_p_up(pred_high, ctx, btc_volatility=0.0001)
        p_low = engine._compute_p_up(pred_low, ctx, btc_volatility=0.0001)

        # Both above 0.5 (positive return), but low conf closer to 0.5
        assert p_high > p_low > 0.5
        assert abs(p_low - 0.5) < abs(p_high - 0.5)

    def test_zero_confidence_gives_half(self):
        """Confidence = 0 → z = 0 → P(up) = 0.5."""
        engine, _, _ = _make_engine()
        pred = _make_prediction(
            current_price=100_000,
            predicted_price=101_000,
            confidence=0.0,
        )
        ctx = _make_market_context(seconds_from_now=300)
        p_up = engine._compute_p_up(pred, ctx, btc_volatility=0.0001)
        assert p_up == pytest.approx(0.5, abs=1e-6)

    def test_time_decay_sharpens_probability(self):
        """Less time remaining → P(up) further from 0.5 (probability sharpens)."""
        engine, _, _ = _make_engine()
        pred = _make_prediction(
            current_price=100_000,
            predicted_price=100_300,
            confidence=0.8,
        )

        ctx_far = _make_market_context(seconds_from_now=600)
        ctx_near = _make_market_context(seconds_from_now=30)

        p_far = engine._compute_p_up(pred, ctx_far, btc_volatility=0.0001)
        p_near = engine._compute_p_up(pred, ctx_near, btc_volatility=0.0001)

        # Both above 0.5, but near-expiry should be further from 0.5
        assert p_far > 0.5
        assert p_near > 0.5
        assert abs(p_near - 0.5) > abs(p_far - 0.5)

    def test_higher_volatility_compresses_probability(self):
        """Higher BTC vol → P(up) closer to 0.5 (more uncertainty)."""
        engine, _, _ = _make_engine()
        pred = _make_prediction(
            current_price=100_000,
            predicted_price=100_300,
            confidence=0.8,
        )
        ctx = _make_market_context(seconds_from_now=300)

        p_low_vol = engine._compute_p_up(pred, ctx, btc_volatility=0.00005)
        p_high_vol = engine._compute_p_up(pred, ctx, btc_volatility=0.005)

        # Both above 0.5, but high vol → closer to 0.5
        assert p_low_vol > 0.5
        assert p_high_vol > 0.5
        assert abs(p_high_vol - 0.5) < abs(p_low_vol - 0.5)

    def test_expired_market_positive_return(self):
        """Expired market with positive return → P_CLAMP_MAX."""
        engine, _, _ = _make_engine()
        pred = _make_prediction(
            current_price=100_000,
            predicted_price=100_500,
            confidence=0.8,
        )
        # Already expired
        ctx = _make_market_context(seconds_from_now=-10)
        p_up = engine._compute_p_up(pred, ctx, btc_volatility=0.0001)
        assert p_up == _P_CLAMP_MAX

    def test_expired_market_negative_return(self):
        """Expired market with negative return → P_CLAMP_MIN."""
        engine, _, _ = _make_engine()
        pred = _make_prediction(
            current_price=100_000,
            predicted_price=99_500,
            confidence=0.8,
        )
        ctx = _make_market_context(seconds_from_now=-10)
        p_up = engine._compute_p_up(pred, ctx, btc_volatility=0.0001)
        assert p_up == _P_CLAMP_MIN

    def test_expired_market_zero_return(self):
        """Expired market with zero return → 0.50."""
        engine, _, _ = _make_engine()
        pred = _make_prediction(
            current_price=100_000,
            predicted_price=100_000,
            confidence=0.8,
        )
        ctx = _make_market_context(seconds_from_now=-10)
        p_up = engine._compute_p_up(pred, ctx, btc_volatility=0.0001)
        assert p_up == 0.50

    def test_zero_volatility_conservative_fallback(self):
        """Zero vol → conservative linear fallback near 0.5."""
        engine, _, _ = _make_engine()
        pred = _make_prediction(
            current_price=100_000,
            predicted_price=100_100,
            confidence=0.8,
        )
        ctx = _make_market_context(seconds_from_now=300)
        p_up = engine._compute_p_up(pred, ctx, btc_volatility=0.0)

        # Should be above 0.5 but not extreme — linear fallback
        assert 0.5 < p_up < 0.6
        assert _P_CLAMP_MIN <= p_up <= _P_CLAMP_MAX


# ---------------------------------------------------------------------------
# Edge Computation / Signal Generation Tests
# ---------------------------------------------------------------------------


class TestEvaluate:
    """Tests for _evaluate() — the full signal generation pipeline."""

    def _setup_engine_with_market(
        self,
        mid_price: float = 0.50,
        seconds_from_now: float = 300.0,
        btc_vol_ticks: int = 50,
    ) -> tuple[StrategyEngine, MarketContext]:
        """Set up engine with a market, books, and enough vol history."""
        engine, _, _ = _make_engine()

        ctx = _make_market_context(seconds_from_now=seconds_from_now)

        # Register market context
        engine.set_market_contexts(
            {
                ctx.yes_token_id: ctx,
                ctx.no_token_id: ctx,
            }
        )

        # Set up order books for both tokens
        yes_book = _make_book(
            token_id=ctx.yes_token_id,
            condition_id=ctx.condition_id,
            mid_price=mid_price,
        )
        no_book = _make_book(
            token_id=ctx.no_token_id,
            condition_id=ctx.condition_id,
            mid_price=1.0 - mid_price,
        )
        engine._books[ctx.yes_token_id] = yes_book
        engine._books[ctx.no_token_id] = no_book

        # Inject BTC price history with realistic noise for volatility estimation.
        # ~0.01% per-tick volatility ≈ realistic for 1-second BTC ticks.
        np.random.seed(42)
        base = 100_000.0
        for i in range(btc_vol_ticks):
            noise = np.random.normal(0, base * 0.0001)  # ~$10 std dev
            engine._btc_price_history.append(base + noise)

        return engine, ctx

    def test_buy_yes_when_underpriced(self):
        """When P(up) > mid, should BUY the YES token.

        With ~0.01% per-tick BTC vol, a 0.1% predicted return over 300s
        gives P(up) ≈ 0.66, producing a ~0.26 edge vs mid=0.40
        (under the 0.30 max_edge threshold).
        """
        engine, ctx = self._setup_engine_with_market(mid_price=0.40)

        # Moderate positive return → P(up) ≈ 0.66
        pred = _make_prediction(
            current_price=100_000,
            predicted_price=100_100,
            confidence=0.9,
        )

        signals = engine._evaluate(pred)

        # Should get at least one BUY signal for the YES token
        yes_signals = [s for s in signals if s.token_id == ctx.yes_token_id]
        assert len(yes_signals) >= 1
        assert yes_signals[0].side.value == "BUY"
        assert yes_signals[0].edge > 0
        assert yes_signals[0].outcome == "YES"

    def test_buy_no_when_yes_overpriced(self):
        """When P(up) < mid (YES overpriced), should BUY the NO token.

        With a -0.1% predicted return, P(up) ≈ 0.34, so:
          NO edge = (1 - 0.34) - 0.30 = 0.36... still too high.
        Use -0.05% return: P(up) ≈ 0.42, NO edge = 0.58 - 0.30 = 0.28 < 0.30.
        """
        engine, ctx = self._setup_engine_with_market(mid_price=0.70)

        # Moderate negative return → P(up) ≈ 0.42 → NO fair = 0.58
        # NO mid = 0.30, edge = 0.58 - 0.30 = 0.28 (under max 0.30)
        pred = _make_prediction(
            current_price=100_000,
            predicted_price=99_950,
            confidence=0.9,
        )

        signals = engine._evaluate(pred)

        # Should get a BUY signal for the NO token
        no_signals = [s for s in signals if s.token_id == ctx.no_token_id]
        assert len(no_signals) >= 1
        assert no_signals[0].side.value == "BUY"
        assert no_signals[0].edge > 0
        assert no_signals[0].outcome == "NO"

    def test_no_signal_when_fairly_priced(self):
        """When the market agrees with our estimate, no signal."""
        engine, ctx = self._setup_engine_with_market(mid_price=0.50)

        # Zero return → P(up) ≈ 0.50 → no edge on either token
        pred = _make_prediction(
            current_price=100_000,
            predicted_price=100_000,
            confidence=0.8,
        )

        signals = engine._evaluate(pred)
        assert len(signals) == 0

    def test_skip_near_expiry_market(self):
        """Markets with < 5 seconds left should be skipped."""
        engine, ctx = self._setup_engine_with_market(
            mid_price=0.30,
            seconds_from_now=3.0,
        )

        pred = _make_prediction(
            current_price=100_000,
            predicted_price=101_000,
            confidence=0.9,
        )

        signals = engine._evaluate(pred)
        assert len(signals) == 0  # Skipped due to near-expiry

    def test_skip_extreme_mid_price(self):
        """Books with mid < 0.01 or > 0.99 should be skipped."""
        engine, ctx = self._setup_engine_with_market(mid_price=0.005)

        pred = _make_prediction(
            current_price=100_000,
            predicted_price=101_000,
            confidence=0.9,
        )

        signals = engine._evaluate(pred)
        assert len(signals) == 0  # YES at 0.005 is below floor

    def test_edge_below_threshold_filtered(self):
        """Edges below min_edge_threshold should not produce signals."""
        # Use a high threshold that no realistic edge can reach
        engine, _, _ = _make_engine(config=_make_config(min_edge_threshold=0.40))

        ctx = _make_market_context(seconds_from_now=300)
        engine.set_market_contexts(
            {
                ctx.yes_token_id: ctx,
                ctx.no_token_id: ctx,
            }
        )

        engine._books[ctx.yes_token_id] = _make_book(
            token_id=ctx.yes_token_id,
            mid_price=0.45,
        )
        engine._books[ctx.no_token_id] = _make_book(
            token_id=ctx.no_token_id,
            mid_price=0.55,
        )

        # Realistic vol data
        np.random.seed(77)
        base = 100_000.0
        for i in range(50):
            noise = np.random.normal(0, base * 0.0001)
            engine._btc_price_history.append(base + noise)

        # Tiny return → small P(up) deviation → edge well under 0.40
        pred = _make_prediction(
            current_price=100_000,
            predicted_price=100_030,
            confidence=0.8,
        )

        signals = engine._evaluate(pred)
        assert len(signals) == 0  # Edge too small for high threshold

    def test_edge_above_max_filtered(self):
        """Edges above max_edge_threshold should be filtered out."""
        engine, _, _ = _make_engine(
            config=_make_config(min_edge_threshold=0.01, max_edge_threshold=0.05),
        )

        ctx = _make_market_context(seconds_from_now=300)
        engine.set_market_contexts(
            {
                ctx.yes_token_id: ctx,
                ctx.no_token_id: ctx,
            }
        )

        # YES at 0.10 with strong predicted return → large edge
        engine._books[ctx.yes_token_id] = _make_book(
            token_id=ctx.yes_token_id,
            mid_price=0.10,
        )
        engine._books[ctx.no_token_id] = _make_book(
            token_id=ctx.no_token_id,
            mid_price=0.90,
        )

        # Realistic vol data
        np.random.seed(99)
        base = 100_000.0
        for i in range(50):
            noise = np.random.normal(0, base * 0.0001)
            engine._btc_price_history.append(base + noise)

        # 0.1% return → P(up) ≈ 0.66 → edge vs 0.10 ≈ 0.56, exceeds max 0.05
        pred = _make_prediction(
            current_price=100_000,
            predicted_price=100_100,
            confidence=1.0,
        )

        signals = engine._evaluate(pred)
        yes_signals = [s for s in signals if s.token_id == ctx.yes_token_id]
        assert len(yes_signals) == 0  # Edge exceeds max_edge=0.05

    def test_signal_analytics_fields_populated(self):
        """Verify Signal includes p_up, outcome, seconds_to_expiry, btc_volatility."""
        engine, ctx = self._setup_engine_with_market(mid_price=0.40)

        # Moderate return to produce an edge within thresholds
        pred = _make_prediction(
            current_price=100_000,
            predicted_price=100_100,
            confidence=0.9,
        )

        signals = engine._evaluate(pred)
        assert len(signals) >= 1

        sig = signals[0]
        assert sig.p_up > 0
        assert sig.outcome in ("YES", "NO")
        assert sig.seconds_to_expiry > 0
        assert sig.btc_volatility >= 0

    def test_only_buy_signals_emitted(self):
        """Strategy should never emit SELL signals (BUY-only for binary)."""
        engine, ctx = self._setup_engine_with_market(mid_price=0.40)

        pred = _make_prediction(
            current_price=100_000,
            predicted_price=100_100,
            confidence=0.9,
        )

        signals = engine._evaluate(pred)
        for sig in signals:
            assert sig.side.value == "BUY"

    def test_no_signal_without_market_context(self):
        """Tokens without MarketContext should be skipped."""
        engine, _, _ = _make_engine()

        # Add a book but no market context
        engine._books["unknown-token"] = _make_book(
            token_id="unknown-token",
            mid_price=0.50,
        )

        pred = _make_prediction(
            current_price=100_000,
            predicted_price=101_000,
            confidence=0.9,
        )

        signals = engine._evaluate(pred)
        assert len(signals) == 0


# ---------------------------------------------------------------------------
# BTC Return Volatility Tests
# ---------------------------------------------------------------------------


class TestBTCVolatility:
    """Tests for _btc_return_volatility()."""

    def test_insufficient_data_returns_zero(self):
        """Less than _MIN_VOL_OBSERVATIONS → 0.0."""
        engine, _, _ = _make_engine()
        # Only add a few prices
        for p in [100_000, 100_010, 100_020]:
            engine._btc_price_history.append(p)
        assert engine._btc_return_volatility() == 0.0

    def test_constant_prices_zero_vol(self):
        """All same price → zero vol."""
        engine, _, _ = _make_engine()
        for _ in range(20):
            engine._btc_price_history.append(100_000.0)
        assert engine._btc_return_volatility() == 0.0

    def test_volatile_prices_nonzero(self):
        """Varying prices → nonzero vol."""
        engine, _, _ = _make_engine()
        np.random.seed(42)
        base = 100_000.0
        for i in range(50):
            engine._btc_price_history.append(base + np.random.normal(0, 10))
        vol = engine._btc_return_volatility()
        assert vol > 0


# ---------------------------------------------------------------------------
# Regime Detection Tests
# ---------------------------------------------------------------------------


class TestRegimeDetection:
    """Tests for EMA-based regime classification."""

    def test_initial_regime_sideways(self):
        engine, _, _ = _make_engine()
        assert engine.current_regime == MarketRegime.SIDEWAYS

    def test_trending_up_detection(self):
        """Steadily increasing prices → TRENDING_UP.

        Regime detection requires that _recent_volatility() > 0.001
        AND that the EMA fast/slow diff > 0.0005.  We use noisy upward
        prices to ensure both conditions are met.
        """
        engine, _, _ = _make_engine()
        np.random.seed(123)
        for i in range(200):
            # Strong uptrend with noise (so vol > 0.001)
            price = 0.30 + i * 0.003 + np.random.normal(0, 0.005)
            price = max(price, 0.01)
            engine._update_regime(price)
            engine._price_history.append(price)
        assert engine.current_regime == MarketRegime.TRENDING_UP

    def test_trending_down_detection(self):
        """Steadily decreasing prices → TRENDING_DOWN."""
        engine, _, _ = _make_engine()
        np.random.seed(456)
        for i in range(200):
            price = 0.90 - i * 0.003 + np.random.normal(0, 0.005)
            price = max(price, 0.01)
            engine._update_regime(price)
            engine._price_history.append(price)
        assert engine.current_regime == MarketRegime.TRENDING_DOWN

    def test_flat_prices_sideways(self):
        """Constant prices → SIDEWAYS."""
        engine, _, _ = _make_engine()
        for _ in range(100):
            engine._update_regime(0.50)
            engine._price_history.append(0.50)
        assert engine.current_regime == MarketRegime.SIDEWAYS


# ---------------------------------------------------------------------------
# Signal Strength Classification Tests
# ---------------------------------------------------------------------------


class TestClassifyStrength:
    """Tests for edge → signal strength mapping."""

    def test_weak_signal(self):
        """Edge at 1.0–1.5× threshold → WEAK."""
        engine, _, _ = _make_engine(config=_make_config(min_edge_threshold=0.02))
        assert engine._classify_strength(0.025) == SignalStrength.WEAK

    def test_moderate_signal(self):
        """Edge at 1.5–2.5× threshold → MODERATE."""
        engine, _, _ = _make_engine(config=_make_config(min_edge_threshold=0.02))
        assert engine._classify_strength(0.04) == SignalStrength.MODERATE

    def test_strong_signal(self):
        """Edge at > 2.5× threshold → STRONG."""
        engine, _, _ = _make_engine(config=_make_config(min_edge_threshold=0.02))
        assert engine._classify_strength(0.06) == SignalStrength.STRONG

    def test_boundary_weak_moderate(self):
        """Edge exactly at 1.5× → MODERATE (not WEAK)."""
        engine, _, _ = _make_engine(config=_make_config(min_edge_threshold=0.02))
        assert engine._classify_strength(0.03) == SignalStrength.MODERATE

    def test_boundary_moderate_strong(self):
        """Edge exactly at 2.5× → STRONG (not MODERATE)."""
        engine, _, _ = _make_engine(config=_make_config(min_edge_threshold=0.02))
        assert engine._classify_strength(0.05) == SignalStrength.STRONG


# ---------------------------------------------------------------------------
# MarketContext Tests
# ---------------------------------------------------------------------------


class TestMarketContext:
    """Tests for MarketContext dataclass methods."""

    def test_seconds_remaining_positive(self):
        ctx = _make_market_context(seconds_from_now=120.0)
        remaining = ctx.seconds_remaining()
        assert 110 < remaining <= 120

    def test_seconds_remaining_expired(self):
        ctx = _make_market_context(seconds_from_now=-10.0)
        assert ctx.seconds_remaining() == 0.0

    def test_token_outcome_yes(self):
        ctx = _make_market_context(yes_token_id="tok-y", no_token_id="tok-n")
        assert ctx.token_outcome("tok-y") == TokenOutcome.YES

    def test_token_outcome_no(self):
        ctx = _make_market_context(yes_token_id="tok-y", no_token_id="tok-n")
        assert ctx.token_outcome("tok-n") == TokenOutcome.NO

    def test_token_outcome_unknown(self):
        ctx = _make_market_context(yes_token_id="tok-y", no_token_id="tok-n")
        assert ctx.token_outcome("tok-other") is None


# ---------------------------------------------------------------------------
# Integration: set_market_contexts backward compat
# ---------------------------------------------------------------------------


class TestSetMarketContexts:
    """Verify set_market_contexts also populates condition mapping."""

    def test_sets_token_to_condition(self):
        engine, _, _ = _make_engine()
        ctx = _make_market_context(
            condition_id="c1",
            yes_token_id="y1",
            no_token_id="n1",
        )
        engine.set_market_contexts({"y1": ctx, "n1": ctx})

        assert engine._token_to_condition == {"y1": "c1", "n1": "c1"}

    def test_sets_token_to_market(self):
        engine, _, _ = _make_engine()
        ctx = _make_market_context()
        engine.set_market_contexts(
            {
                ctx.yes_token_id: ctx,
                ctx.no_token_id: ctx,
            }
        )
        assert engine._token_to_market[ctx.yes_token_id] is ctx
        assert engine._token_to_market[ctx.no_token_id] is ctx


# ---------------------------------------------------------------------------
# Regression Compatibility Tests
# ---------------------------------------------------------------------------


class TestRegressionCompatibility:
    """Tests verifying that regression models (confidence=1.0) work correctly
    through the strategy pipeline.

    With regression, the MLPredictor sets confidence=1.0 so:
    - z *= confidence is a no-op (pure z-score)
    - Risk manager's confidence_mult = 1.0 (no dampening)
    The return magnitude flows through naturally via predicted_price.
    """

    def test_confidence_one_no_dampening(self):
        """confidence=1.0 should give undampened z-score (same as full confidence)."""
        engine, _, _ = _make_engine()
        ctx = _make_market_context(seconds_from_now=300)

        # Regression-style prediction: confidence=1.0
        pred_reg = _make_prediction(
            current_price=100_000,
            predicted_price=100_500,
            confidence=1.0,
        )

        p_up_reg = engine._compute_p_up(pred_reg, ctx, btc_volatility=0.0001)

        # Verify it's above 0.5 and NOT dampened toward 0.5
        assert p_up_reg > 0.5

        # Compare with confidence=0.5 to verify dampening is absent at 1.0
        pred_half = _make_prediction(
            current_price=100_000,
            predicted_price=100_500,
            confidence=0.5,
        )
        p_up_half = engine._compute_p_up(pred_half, ctx, btc_volatility=0.0001)

        # confidence=1.0 should produce P(up) further from 0.5 than 0.5
        assert abs(p_up_reg - 0.5) > abs(p_up_half - 0.5)

    def test_regression_signal_generation(self):
        """Full pipeline should generate signals with confidence=1.0.

        This mirrors how regression models emit predictions: the return
        magnitude determines predicted_price, confidence is always 1.0.
        """
        engine, _, _ = _make_engine()
        ctx = _make_market_context(seconds_from_now=300)

        engine.set_market_contexts(
            {
                ctx.yes_token_id: ctx,
                ctx.no_token_id: ctx,
            }
        )

        # Set up books — YES underpriced at 0.40
        yes_book = _make_book(
            token_id=ctx.yes_token_id,
            condition_id=ctx.condition_id,
            mid_price=0.40,
        )
        no_book = _make_book(
            token_id=ctx.no_token_id,
            condition_id=ctx.condition_id,
            mid_price=0.60,
        )
        engine._books[ctx.yes_token_id] = yes_book
        engine._books[ctx.no_token_id] = no_book

        # Inject BTC price history for volatility estimation
        np.random.seed(42)
        base = 100_000.0
        for i in range(50):
            noise = np.random.normal(0, base * 0.0001)
            engine._btc_price_history.append(base + noise)

        # Regression-style prediction: moderate return, confidence=1.0
        pred = _make_prediction(
            current_price=100_000,
            predicted_price=100_100,  # +0.1% return
            confidence=1.0,
        )

        signals = engine._evaluate(pred)

        # Should produce a BUY YES signal (P(up) > 0.5, YES underpriced)
        yes_signals = [s for s in signals if s.token_id == ctx.yes_token_id]
        assert len(yes_signals) >= 1
        assert yes_signals[0].side.value == "BUY"
        assert yes_signals[0].edge > 0
        # Confidence in the signal's prediction should be 1.0
        assert yes_signals[0].prediction.confidence == 1.0
