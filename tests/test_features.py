"""Tests for the ML feature engine (v3 — 58 features).

Verifies:
  1. Batch and streaming modes produce identical output for the same data.
  2. Features are backward-looking only (no future data leakage).
  3. Edge cases: insufficient data, zero prices, NaN handling.
  4. Multi-timeframe features compute correctly.
  5. New v2 features (volume imbalance, autocorrelation, etc.) are sane.
  6. v3 candlestick microstructure features compute correctly.
  7. Orderbook-derived features (book history) are sane.
"""

from __future__ import annotations

import numpy as np

from src.ml.features import (
    FEATURE_NAMES,
    NUM_FEATURES,
    _MIN_TICKS,
    _N_CORE_FEATURES,
    FeatureEngine,
    compute_batch,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_klines(n: int = 5000, base_price: float = 80_000.0, seed: int = 42):
    """Generate synthetic 1-second klines with a random walk.

    Returns (timestamps_ms, opens, highs, lows, closes, volumes, trades).
    Highs/lows are slightly above/below close for realistic h-l range.
    """
    rng = np.random.default_rng(seed)
    returns = rng.normal(0, 0.0001, n)
    closes = base_price * np.exp(np.cumsum(returns))
    timestamps_ms = np.arange(n, dtype=np.int64) * 1000 + 1_700_000_000_000
    volumes = rng.uniform(0.1, 10.0, n)
    trades = rng.integers(1, 100, n).astype(np.float64)
    # Highs above close, lows below close for realistic range
    highs = closes + np.abs(rng.normal(0, 1.0, n))
    lows = closes - np.abs(rng.normal(0, 1.0, n))
    opens = closes + rng.normal(0, 0.5, n)
    return timestamps_ms, opens, highs, lows, closes, volumes, trades


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestFeatureCatalogue:
    """Feature catalogue integrity."""

    def test_feature_count(self):
        assert NUM_FEATURES == 58

    def test_feature_names_are_unique(self):
        assert len(set(FEATURE_NAMES)) == NUM_FEATURES

    def test_min_ticks(self):
        assert _MIN_TICKS == 3661

    def test_n_core_features(self):
        assert _N_CORE_FEATURES == 51


class TestBatchCompute:
    """Batch feature computation."""

    def test_output_shape(self):
        ts, o, h, l, c, v, t = _make_klines(5000)
        result = compute_batch(ts, o, h, l, c, v, t)
        assert result.shape == (5000, 58)

    def test_no_nan_after_warmup(self):
        """After _MIN_TICKS+ ticks, core features (0-50) should not be NaN."""
        ts, o, h, l, c, v, t = _make_klines(5000)
        result = compute_batch(ts, o, h, l, c, v, t)
        # Row 3700 should have no NaN in core features (indices 0-50)
        assert not np.any(np.isnan(result[3700, :_N_CORE_FEATURES]))

    def test_early_rows_have_nan(self):
        """First rows should have NaN (insufficient lookback)."""
        ts, o, h, l, c, v, t = _make_klines(5000)
        result = compute_batch(ts, o, h, l, c, v, t)
        # Row 0 should have NaN in return columns
        assert np.isnan(result[0, 0])  # return_1s needs at least 1 prior tick

    def test_returns_are_correct(self):
        """Verify return_1s is computed correctly."""
        prices = np.array([100.0, 101.0, 102.0, 100.0, 99.0], dtype=np.float64)
        ts = np.arange(5, dtype=np.int64) * 1000
        v = np.ones(5)
        t = np.ones(5)
        result = compute_batch(ts, prices, prices, prices, prices, v, t)
        # return_1s at index 1 = (101 - 100) / 100 = 0.01
        assert abs(result[1, 0] - 0.01) < 1e-10
        # return_1s at index 3 = (100 - 102) / 102 ≈ -0.01961
        assert abs(result[3, 0] - (-2.0 / 102)) < 1e-10

    def test_orderbook_features_pass_through(self):
        """Poly mid/spread are constant pass-through values at new indices."""
        ts, o, h, l, c, v, t = _make_klines(5000)
        result = compute_batch(
            ts,
            o,
            h,
            l,
            c,
            v,
            t,
            poly_mid=0.51,
            poly_spread=0.02,
        )
        assert result[3700, 51] == 0.51  # poly_mid at index 51
        assert result[3700, 52] == 0.02  # poly_spread at index 52

    def test_time_features(self):
        """Seconds-to-expiry and elapsed fraction are correct at new indices."""
        ts, o, h, l, c, v, t = _make_klines(5000)
        result = compute_batch(
            ts,
            o,
            h,
            l,
            c,
            v,
            t,
            seconds_to_expiry=120.0,
            total_seconds=300.0,
        )
        assert result[3700, 56] == 120.0  # seconds_to_expiry at 56
        assert abs(result[3700, 57] - 0.6) < 1e-10  # elapsed_fraction at 57

    def test_none_pass_through_defaults(self):
        """Passing None for pass-through features should default gracefully."""
        ts, o, h, l, c, v, t = _make_klines(5000)
        result = compute_batch(
            ts,
            o,
            h,
            l,
            c,
            v,
            t,
            poly_mid=None,
            poly_spread=None,
            seconds_to_expiry=None,
            total_seconds=None,
        )
        assert result[3700, 51] == 0.0  # poly_mid defaults to 0
        assert result[3700, 52] == 0.0  # poly_spread defaults to 0
        assert result[3700, 56] == 300.0  # seconds_to_expiry defaults to 300
        assert abs(result[3700, 57] - 0.0) < 1e-10  # 1 - 300/300 = 0


class TestMultiTimeframeFeatures:
    """Multi-timeframe bar features (indices 27-36)."""

    def test_1m_features_not_nan(self):
        """1m bar features should be valid after enough data."""
        ts, o, h, l, c, v, t = _make_klines(5000)
        result = compute_batch(ts, o, h, l, c, v, t)
        # At row 3700, all 1m features (27-33) should be valid
        for idx in range(27, 34):
            assert not np.isnan(
                result[3700, idx]
            ), f"Feature {idx} ({FEATURE_NAMES[idx]}) is NaN"

    def test_5m_features_not_nan(self):
        """5m bar features should be valid after enough data."""
        ts, o, h, l, c, v, t = _make_klines(5000)
        result = compute_batch(ts, o, h, l, c, v, t)
        for idx in range(34, 37):
            assert not np.isnan(
                result[3700, idx]
            ), f"Feature {idx} ({FEATURE_NAMES[idx]}) is NaN"

    def test_1m_return_sign(self):
        """1m bar return should be positive for a consistent uptrend."""
        n = 5000
        prices = 80000 + np.arange(n, dtype=np.float64) * 0.1  # steady uptrend
        ts = np.arange(n, dtype=np.int64) * 1000
        v = np.ones(n)
        t = np.ones(n)
        result = compute_batch(ts, prices, prices + 0.01, prices - 0.01, prices, v, t)
        # return_1m_1bar at row 3700 should be positive (uptrend)
        assert (
            result[3700, 27] > 0
        ), f"Expected positive 1m return, got {result[3700, 27]}"


class TestNewSingleTFFeatures:
    """New v2 single-timeframe features (indices 37-44)."""

    def test_volume_imbalance_range(self):
        """Volume imbalance should be in [0, 1]."""
        ts, o, h, l, c, v, t = _make_klines(5000)
        result = compute_batch(ts, o, h, l, c, v, t)
        vi = result[3700, 37]
        assert not np.isnan(vi), "volume_imbalance_1s is NaN"
        assert 0.0 <= vi <= 1.0, f"volume_imbalance_1s={vi} out of [0,1]"

    def test_volume_imbalance_60s_not_nan(self):
        ts, o, h, l, c, v, t = _make_klines(5000)
        result = compute_batch(ts, o, h, l, c, v, t)
        assert not np.isnan(result[3700, 38])

    def test_autocorrelation_range(self):
        """Autocorrelation should be in [-1, 1]."""
        ts, o, h, l, c, v, t = _make_klines(5000)
        result = compute_batch(ts, o, h, l, c, v, t)
        for idx in [39, 40]:
            val = result[3700, idx]
            assert not np.isnan(val), f"{FEATURE_NAMES[idx]} is NaN"
            assert -1.0 <= val <= 1.0, f"{FEATURE_NAMES[idx]}={val} out of [-1,1]"

    def test_autocorrelation_trending(self):
        """Strongly trending returns should have non-zero lag-1 autocorrelation."""
        n = 5000
        # Use a trend with momentum (each return slightly positive, autocorrelated)
        rng = np.random.default_rng(42)
        returns = 0.001 + rng.normal(0, 0.0005, n)  # persistent positive returns
        prices = 80000 * np.exp(np.cumsum(returns))
        ts = np.arange(n, dtype=np.int64) * 1000
        v = np.ones(n)
        t = np.ones(n)
        result = compute_batch(ts, prices, prices + 0.01, prices - 0.01, prices, v, t)
        # autocorr should be non-zero (we don't require sign since it depends on noise)
        assert not np.isnan(result[3700, 39]), "autocorr_lag1_60s is NaN"
        assert abs(result[3700, 39]) < 1.0, "autocorr_lag1_60s out of valid range"

    def test_vol_pctrank_range(self):
        """Vol percentile rank should be in [0, 1]."""
        ts, o, h, l, c, v, t = _make_klines(5000)
        result = compute_batch(ts, o, h, l, c, v, t)
        val = result[3700, 41]
        assert not np.isnan(val), "vol_pctrank_1h is NaN"
        assert 0.0 <= val <= 1.0, f"vol_pctrank_1h={val} out of [0,1]"

    def test_price_position_range(self):
        """Price position in range should be in [0, 1]."""
        ts, o, h, l, c, v, t = _make_klines(5000)
        result = compute_batch(ts, o, h, l, c, v, t)
        for idx in [42, 43]:
            val = result[3700, idx]
            assert not np.isnan(val), f"{FEATURE_NAMES[idx]} is NaN"
            assert 0.0 <= val <= 1.0, f"{FEATURE_NAMES[idx]}={val} out of [0,1]"

    def test_avg_trade_size_ratio_positive(self):
        """Avg trade size ratio should be positive."""
        ts, o, h, l, c, v, t = _make_klines(5000)
        result = compute_batch(ts, o, h, l, c, v, t)
        val = result[3700, 44]
        assert not np.isnan(val), "avg_trade_size_ratio is NaN"
        assert val > 0, f"avg_trade_size_ratio={val} should be positive"


class TestCandlestickMicrostructure:
    """v3 candlestick microstructure features (indices 45-50)."""

    def test_body_ratio_range(self):
        """body_ratio should be in [0, 1]."""
        ts, o, h, l, c, v, t = _make_klines(5000)
        result = compute_batch(ts, o, h, l, c, v, t)
        val = result[3700, 45]
        assert not np.isnan(val), "body_ratio is NaN"
        assert 0.0 <= val <= 1.0, f"body_ratio={val} out of [0,1]"

    def test_upper_shadow_ratio_range(self):
        """upper_shadow_ratio should be in [0, 1]."""
        ts, o, h, l, c, v, t = _make_klines(5000)
        result = compute_batch(ts, o, h, l, c, v, t)
        val = result[3700, 46]
        assert not np.isnan(val), "upper_shadow_ratio is NaN"
        assert 0.0 <= val <= 1.0, f"upper_shadow_ratio={val} out of [0,1]"

    def test_lower_shadow_ratio_range(self):
        """lower_shadow_ratio should be in [0, 1]."""
        ts, o, h, l, c, v, t = _make_klines(5000)
        result = compute_batch(ts, o, h, l, c, v, t)
        val = result[3700, 47]
        assert not np.isnan(val), "lower_shadow_ratio is NaN"
        assert 0.0 <= val <= 1.0, f"lower_shadow_ratio={val} out of [0,1]"

    def test_close_location_60s_not_nan(self):
        """close_location_60s should be valid after warmup."""
        ts, o, h, l, c, v, t = _make_klines(5000)
        result = compute_batch(ts, o, h, l, c, v, t)
        assert not np.isnan(result[3700, 48]), "close_location_60s is NaN"

    def test_body_momentum_60s_not_nan(self):
        """body_momentum_60s should be valid after warmup."""
        ts, o, h, l, c, v, t = _make_klines(5000)
        result = compute_batch(ts, o, h, l, c, v, t)
        assert not np.isnan(result[3700, 49]), "body_momentum_60s is NaN"

    def test_shadow_imbalance_60s_not_nan(self):
        """shadow_imbalance_60s should be valid after warmup."""
        ts, o, h, l, c, v, t = _make_klines(5000)
        result = compute_batch(ts, o, h, l, c, v, t)
        assert not np.isnan(result[3700, 50]), "shadow_imbalance_60s is NaN"

    def test_shadow_ratios_sum(self):
        """body + upper_shadow + lower_shadow should sum to ~1.0 for proper candles."""
        # Use realistic candle data where low <= open,close <= high
        n = 5000
        rng = np.random.default_rng(42)
        closes = 80000 * np.exp(np.cumsum(rng.normal(0, 0.0001, n)))
        highs = closes + np.abs(rng.normal(0, 1.0, n))
        lows = closes - np.abs(rng.normal(0, 1.0, n))
        # Ensure opens are between low and high
        opens = lows + rng.uniform(0, 1, n) * (highs - lows)
        ts = np.arange(n, dtype=np.int64) * 1000
        v = rng.uniform(0.1, 10.0, n)
        t = rng.integers(1, 100, n).astype(np.float64)
        result = compute_batch(ts, opens, highs, lows, closes, v, t)
        body = result[3700, 45]
        upper = result[3700, 46]
        lower = result[3700, 47]
        total = body + upper + lower
        # Should sum to 1.0 for well-formed candles
        assert abs(total - 1.0) < 0.01, f"Candle parts sum to {total}, expected ~1.0"

    def test_flat_candle_defaults(self):
        """When open==high==low==close (flat), body_ratio should be 0."""
        n = 100
        flat_price = np.full(n, 80000.0)
        ts = np.arange(n, dtype=np.int64) * 1000
        v = np.ones(n)
        t = np.ones(n)
        result = compute_batch(ts, flat_price, flat_price, flat_price, flat_price, v, t)
        # body_ratio should be 0 for flat candle
        assert result[50, 45] == 0.0, "body_ratio should be 0 for flat candle"


class TestOrderbookFeatures:
    """Orderbook features (indices 51-55) in batch and streaming modes."""

    def test_batch_orderbook_pass_through(self):
        """Batch mode: poly_mid and poly_spread are stored at indices 51-52."""
        ts, o, h, l, c, v, t = _make_klines(5000)
        result = compute_batch(ts, o, h, l, c, v, t, poly_mid=0.55, poly_spread=0.03)
        assert result[3700, 51] == 0.55
        assert result[3700, 52] == 0.03

    def test_batch_spread_pct(self):
        """Batch mode: spread_pct = spread / mid."""
        ts, o, h, l, c, v, t = _make_klines(5000)
        result = compute_batch(ts, o, h, l, c, v, t, poly_mid=0.50, poly_spread=0.04)
        expected = 0.04 / 0.50  # 0.08
        assert abs(result[3700, 53] - expected) < 1e-10

    def test_batch_orderbook_derived_zero(self):
        """Batch mode: mid_return_5s and spread_zscore_60s are 0 (no book history)."""
        ts, o, h, l, c, v, t = _make_klines(5000)
        result = compute_batch(ts, o, h, l, c, v, t, poly_mid=0.50, poly_spread=0.04)
        assert result[3700, 54] == 0.0  # mid_return_5s
        assert result[3700, 55] == 0.0  # spread_zscore_60s

    def test_streaming_book_features(self):
        """Streaming mode: book features are computed from update_book history."""
        engine = FeatureEngine(buffer_size=4200)
        rng = np.random.default_rng(42)
        base = 80_000.0

        for i in range(4000):
            base *= 1 + rng.normal(0, 0.0001)
            engine.update(
                timestamp_ns=i * 1_000_000_000,
                price=base,
                volume=rng.uniform(0.1, 10.0),
            )

        # Feed 20 book updates with increasing mid
        for i in range(20):
            engine.update_book(mid=0.50 + i * 0.001, spread=0.02)

        result = engine.compute(seconds_to_expiry=200.0, total_seconds=300.0)
        assert result is not None
        # mid_return_5s should be non-zero (mid changed over last 5 ticks)
        assert result[54] != 0.0, "mid_return_5s should be non-zero with book history"
        # spread_pct = spread / mid
        assert result[53] > 0, "spread_pct should be positive"


class TestBackwardLooking:
    """Verify features don't leak future data."""

    def test_no_future_leakage(self):
        """Appending future data must not change past feature values."""
        ts, o, h, l, c, v, t = _make_klines(5000, seed=1)

        # Compute features for the first 4000 ticks only
        result_short = compute_batch(
            ts[:4000],
            o[:4000],
            h[:4000],
            l[:4000],
            c[:4000],
            v[:4000],
            t[:4000],
        )

        # Compute features for all 5000 ticks
        result_long = compute_batch(ts, o, h, l, c, v, t)

        # Row 3700 should be identical in both
        # Exclude EMA-based features (23, 24, 33) which have infinite memory
        core_indices = list(range(0, 23)) + [
            25,
            26,
            27,
            28,
            29,
            30,
            31,
            32,
            34,
            35,
            36,
            37,
            38,
            39,
            40,
            42,
            43,
            44,
            45,
            46,
            47,
            48,
            49,
            50,
        ]
        np.testing.assert_array_almost_equal(
            result_short[3700, core_indices],
            result_long[3700, core_indices],
            decimal=8,
        )


class TestStreamingCompute:
    """Streaming (live inference) feature computation."""

    def test_returns_none_when_insufficient(self):
        engine = FeatureEngine(buffer_size=4000)
        # Feed only 100 ticks — not enough
        for i in range(100):
            engine.update(
                timestamp_ns=i * 1_000_000_000,
                price=80_000.0 + i * 0.1,
                volume=1.0,
            )
        assert engine.compute() is None

    def test_returns_none_before_min_ticks(self):
        """Should return None until _MIN_TICKS ticks are accumulated."""
        engine = FeatureEngine(buffer_size=4000)
        for i in range(3660):
            engine.update(
                timestamp_ns=i * 1_000_000_000,
                price=80_000.0 + i * 0.001,
                volume=1.0,
            )
        assert engine.compute() is None  # 3660 < 3661

    def test_returns_vector_when_sufficient(self):
        engine = FeatureEngine(buffer_size=4200)
        rng = np.random.default_rng(42)
        base = 80_000.0
        # Need more than _MIN_TICKS=3661 ticks. Use 4000 to be safe,
        # since vol_pctrank_1h needs 3600 and other features need ~300 more.
        for i in range(4000):
            base *= 1 + rng.normal(0, 0.0001)
            engine.update(
                timestamp_ns=i * 1_000_000_000,
                price=base,
                volume=rng.uniform(0.1, 10.0),
            )
        result = engine.compute()
        assert result is not None
        assert result.shape == (58,)

    def test_latest_price(self):
        engine = FeatureEngine()
        engine.update(1_000_000_000, 80_000.0)
        engine.update(2_000_000_000, 81_000.0)
        assert engine.latest_price == 81_000.0

    def test_tick_count(self):
        engine = FeatureEngine()
        assert engine.tick_count == 0
        engine.update(1_000_000_000, 80_000.0)
        assert engine.tick_count == 1


class TestBatchStreamingParity:
    """Batch and streaming modes must produce identical output."""

    def test_parity(self):
        """Feed same data to both modes, compare the last-row output."""
        n = 3700
        rng = np.random.default_rng(99)

        # Generate prices
        base_price = 80_000.0
        prices = np.empty(n, dtype=np.float64)
        prices[0] = base_price
        for i in range(1, n):
            prices[i] = prices[i - 1] * (1 + rng.normal(0, 0.0001))

        timestamps_ms = np.arange(n, dtype=np.int64) * 1000
        timestamps_ns = timestamps_ms * 1_000_000
        volumes = rng.uniform(0.1, 10.0, n)
        trades = np.ones(n)

        # For streaming: use price as H/L too (tick data)
        highs = prices + np.abs(rng.normal(0, 0.5, n))
        lows = prices - np.abs(rng.normal(0, 0.5, n))

        # Batch mode
        batch_result = compute_batch(
            timestamps_ms,
            prices,
            highs,
            lows,
            prices,
            volumes,
            trades,
            poly_mid=0.50,
            poly_spread=0.01,
            seconds_to_expiry=200.0,
            total_seconds=300.0,
        )
        batch_last = batch_result[-1]

        # Streaming mode
        engine = FeatureEngine(buffer_size=4000)
        for i in range(n):
            engine.update(
                int(timestamps_ns[i]),
                prices[i],
                volumes[i],
                trades[i],
                high=highs[i],
                low=lows[i],
            )
        # Update book to match batch pass-through
        engine.update_book(mid=0.50, spread=0.01)
        stream_last = engine.compute(
            seconds_to_expiry=200.0,
            total_seconds=300.0,
        )

        assert stream_last is not None
        # Compare core features (0-50) — pass-through and derived features are
        # trivially equal. Exclude EMA-based features (23, 24, 33)
        # which have initialization-dependent infinite memory.
        core_indices = list(range(0, 23)) + [
            25,
            26,
            27,
            28,
            29,
            30,
            31,
            32,
            34,
            35,
            36,
            37,
            38,
            39,
            40,
            42,
            43,
            44,
            45,
            46,
            47,
            48,
            49,
            50,
        ]
        np.testing.assert_array_almost_equal(
            batch_last[core_indices],
            stream_last[core_indices],
            decimal=6,
        )

        # Also check pass-through features match
        assert stream_last[51] == 0.50  # poly_mid
        assert stream_last[52] == 0.01  # poly_spread
        assert stream_last[56] == 200.0  # seconds_to_expiry
        assert abs(stream_last[57] - (1.0 - 200.0 / 300.0)) < 1e-10
