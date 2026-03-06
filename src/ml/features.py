"""
Feature engineering for BTC direction prediction (v3).

Computes 58 features from historical OHLCV data across multiple timeframes,
plus orderbook-derived and time features.

This module is the single source of truth for feature computation — used
identically in:

  1. **Training (batch):** ``compute_batch(timestamps, O, H, L, C, V, T)``
     processes entire arrays with vectorised numpy operations.

  2. **Inference (streaming):** ``update(ts, price, vol)`` feeds a rolling
     buffer, then ``compute()`` returns one feature vector.

Feature parity between the two modes is verified by ``tests/test_features.py``.

v2 additions:
  - Multi-timeframe features (1m, 5m bar aggregation)
  - Volume imbalance, autocorrelation, vol percentile rank
  - Price position in range, avg trade size ratio

v3 additions:
  - Candlestick microstructure (body ratio, shadow ratios, close location)
  - Orderbook-derived features (spread_pct, mid_return, spread_zscore)
  - Book history tracking in FeatureEngine for live orderbook features
"""

from __future__ import annotations

import logging
from collections import deque

import numpy as np

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Feature catalogue — order and names are frozen (model depends on them)
# ---------------------------------------------------------------------------

FEATURE_NAMES: list[str] = [
    # ── 1s Returns (6) ──
    "return_1s",  # 0
    "return_5s",  # 1
    "return_15s",  # 2
    "return_30s",  # 3
    "return_60s",  # 4
    "return_300s",  # 5
    # ── 1s Volatility (4) ──
    "vol_15s",  # 6
    "vol_30s",  # 7
    "vol_60s",  # 8
    "vol_300s",  # 9
    # ── 1s Momentum (3) ──
    "momentum_5s",  # 10
    "momentum_15s",  # 11
    "momentum_60s",  # 12
    # ── 1s Acceleration (2) ──
    "accel_15s",  # 13
    "accel_60s",  # 14
    # ── 1s Volume (4) ──
    "volume_15s",  # 15
    "volume_60s",  # 16
    "volume_300s",  # 17
    "volume_roc_60s",  # 18
    # ── VWAP deviation (2) ──
    "vwap_dev_60s",  # 19
    "vwap_dev_300s",  # 20
    # ── Bollinger z-score (2) ──
    "boll_z_60s",  # 21
    "boll_z_300s",  # 22
    # ── EMA / MACD (2) ──
    "ema_cross",  # 23
    "macd_signal",  # 24
    # ── Range / intensity (2) ──
    "high_low_range_60s",  # 25
    "trades_intensity_60s",  # 26
    # ── Multi-TF: 1-minute bar features (7) ──
    "return_1m_1bar",  # 27
    "return_1m_5bar",  # 28
    "return_1m_15bar",  # 29
    "vol_1m_5bar",  # 30
    "vol_1m_15bar",  # 31
    "momentum_1m_5bar",  # 32
    "ema_cross_1m",  # 33
    # ── Multi-TF: 5-minute bar features (3) ──
    "return_5m_1bar",  # 34
    "return_5m_3bar",  # 35
    "vol_5m_3bar",  # 36
    # ── New single-TF features (8) ──
    "volume_imbalance_1s",  # 37
    "volume_imbalance_60s",  # 38
    "autocorr_lag1_60s",  # 39
    "autocorr_lag5_60s",  # 40
    "vol_pctrank_1h",  # 41
    "price_position_300s",  # 42
    "price_position_900s",  # 43
    "avg_trade_size_ratio",  # 44
    # ── Candlestick microstructure (6) ──
    "body_ratio",  # 45 — |close-open|/(high-low)
    "upper_shadow_ratio",  # 46 — (high-max(open,close))/(high-low)
    "lower_shadow_ratio",  # 47 — (min(open,close)-low)/(high-low)
    "close_location_60s",  # 48 — rolling mean of (close-low)/(high-low) over 60s
    "body_momentum_60s",  # 49 — rolling mean of (close-open)/close over 60s
    "shadow_imbalance_60s",  # 50 — rolling mean of (upper-lower)/(high-low) over 60s
    # ── Orderbook features (5) ──
    "poly_mid",  # 51 — book mid-price (pass-through)
    "poly_spread",  # 52 — book spread (pass-through)
    "spread_pct",  # 53 — spread/mid_price
    "mid_return_5s",  # 54 — 5-tick return of book mid-price
    "spread_zscore_60s",  # 55 — (spread - mean_spread_60) / std_spread_60
    # ── Time features (2) ──
    "seconds_to_expiry",  # 56
    "elapsed_fraction",  # 57
]

NUM_FEATURES: int = len(FEATURE_NAMES)  # 58

# Number of core kline-derived features that must be non-NaN for valid training rows.
# Features 0-50 are computed from OHLCV data. Features 51+ are pass-through/orderbook/time.
_N_CORE_FEATURES: int = 51

# Minimum ticks required before we can compute a valid feature vector.
# 3600s for vol_pctrank_1h + 60s for underlying vol + 1 current tick = 3661
_MIN_TICKS: int = 3661


# ---------------------------------------------------------------------------
# Helper: rolling statistics (vectorised)
# ---------------------------------------------------------------------------


def _rolling_return(prices: np.ndarray, lag: int) -> np.ndarray:
    """(price[i] - price[i-lag]) / price[i-lag].  First ``lag`` values = NaN."""
    out = np.full(len(prices), np.nan)
    if lag < len(prices):
        prev = prices[:-lag] if lag > 0 else prices
        curr = prices[lag:]
        safe_prev = np.where(prev == 0, np.nan, prev)
        out[lag:] = (curr - prev) / safe_prev
    return out


def _rolling_std(returns: np.ndarray, window: int) -> np.ndarray:
    """Rolling std over ``window`` elements.  Uses cumulative sums for O(N)."""
    n = len(returns)
    out = np.full(n, np.nan)
    if window > n:
        return out
    # Replace NaN with 0 for accumulation, mark NaN positions
    safe = np.nan_to_num(returns, nan=0.0)
    cum = np.cumsum(safe)
    cum2 = np.cumsum(safe**2)
    # sum and sum-of-squares over each window
    s = cum[window - 1 :]
    s[1:] -= cum[:-window]
    s2 = cum2[window - 1 :]
    s2[1:] -= cum2[:-window]
    var = s2 / window - (s / window) ** 2
    var = np.maximum(var, 0.0)  # numerical guard
    out[window - 1 :] = np.sqrt(var)
    return out


def _rolling_sum(arr: np.ndarray, window: int) -> np.ndarray:
    """Rolling sum over ``window`` elements."""
    n = len(arr)
    out = np.full(n, np.nan)
    if window > n:
        return out
    safe = np.nan_to_num(arr, nan=0.0)
    cum = np.cumsum(safe)
    out[window - 1] = cum[window - 1]
    out[window:] = cum[window:] - cum[:-window]
    return out


def _rolling_mean(arr: np.ndarray, window: int) -> np.ndarray:
    """Rolling mean over ``window`` elements."""
    rsum = _rolling_sum(arr, window)
    return rsum / window


def _ema(arr: np.ndarray, span: int) -> np.ndarray:
    """Exponential moving average (forward-only, no future leakage)."""
    alpha = 2.0 / (span + 1)
    out = np.empty_like(arr)
    out[0] = arr[0]
    for i in range(1, len(arr)):
        out[i] = alpha * arr[i] + (1 - alpha) * out[i - 1]
    return out


def _vwap(prices: np.ndarray, volumes: np.ndarray, window: int) -> np.ndarray:
    """Rolling VWAP = rolling_sum(price * volume) / rolling_sum(volume)."""
    pv = prices * volumes
    sum_pv = _rolling_sum(pv, window)
    sum_v = _rolling_sum(volumes, window)
    safe_v = np.where(sum_v == 0, np.nan, sum_v)
    return sum_pv / safe_v


# ---------------------------------------------------------------------------
# Multi-timeframe helpers
# ---------------------------------------------------------------------------


def _resample_bars(
    opens: np.ndarray,
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    volumes: np.ndarray,
    bar_size: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray] | None:
    """Resample 1s OHLCV into coarser bars (1m or 5m).

    Returns (bar_open, bar_high, bar_low, bar_close, bar_volume) or None
    if not enough data for even one bar.
    """
    n = len(closes)
    n_bars = n // bar_size
    if n_bars == 0:
        return None
    trim = n_bars * bar_size
    c_reshape = closes[:trim].reshape(n_bars, bar_size)
    h_reshape = highs[:trim].reshape(n_bars, bar_size)
    l_reshape = lows[:trim].reshape(n_bars, bar_size)
    o_reshape = opens[:trim].reshape(n_bars, bar_size)
    v_reshape = volumes[:trim].reshape(n_bars, bar_size)

    bar_close = c_reshape[:, -1].copy()
    bar_high = h_reshape.max(axis=1)
    bar_low = l_reshape.min(axis=1)
    bar_open = o_reshape[:, 0].copy()
    bar_volume = v_reshape.sum(axis=1)
    return bar_open, bar_high, bar_low, bar_close, bar_volume


def _map_bar_features_to_1s(
    bar_features: np.ndarray,
    n_1s: int,
    bar_size: int,
) -> np.ndarray:
    """Map bar-level feature array to 1s indices.

    For 1s index i, the last COMPLETED bar is bar_idx = (i + 1) // bar_size - 1.
    """
    out = np.full(n_1s, np.nan)
    bar_indices = (np.arange(n_1s) + 1) // bar_size - 1
    valid = bar_indices >= 0
    # Clip to available bar range
    n_bars = len(bar_features)
    bar_indices_clipped = np.clip(bar_indices, 0, max(n_bars - 1, 0))
    out[valid] = bar_features[bar_indices_clipped[valid]]
    # NaN where bar feature itself is NaN or bar not yet available
    return out


def _compute_multi_tf_features(
    features: np.ndarray,
    n: int,
    opens: np.ndarray,
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    volumes: np.ndarray,
) -> None:
    """Compute multi-timeframe features (indices 27-36) in-place."""
    # ── 1-minute bars (features 27-33) ──
    bars_1m = _resample_bars(opens, highs, lows, closes, volumes, 60)
    if bars_1m is not None:
        _, _, _, bar_close_1m, _ = bars_1m
        n_bars_1m = len(bar_close_1m)

        # Returns on 1m bars
        ret_1m_1 = _rolling_return(bar_close_1m, 1)
        ret_1m_5 = _rolling_return(bar_close_1m, 5)
        ret_1m_15 = _rolling_return(bar_close_1m, 15)

        features[:, 27] = _map_bar_features_to_1s(ret_1m_1, n, 60)
        features[:, 28] = _map_bar_features_to_1s(ret_1m_5, n, 60)
        features[:, 29] = _map_bar_features_to_1s(ret_1m_15, n, 60)

        # Volatility on 1m returns
        ret_1m_1bar = _rolling_return(bar_close_1m, 1)
        vol_1m_5 = _rolling_std(ret_1m_1bar, 5)
        vol_1m_15 = _rolling_std(ret_1m_1bar, 15)
        features[:, 30] = _map_bar_features_to_1s(vol_1m_5, n, 60)
        features[:, 31] = _map_bar_features_to_1s(vol_1m_15, n, 60)

        # Momentum on 1m bars
        if n_bars_1m >= 5:
            mom_1m_5 = ret_1m_5 / 5.0
            features[:, 32] = _map_bar_features_to_1s(mom_1m_5, n, 60)

        # EMA cross on 1m bars
        if n_bars_1m >= 15:
            ema5_1m = _ema(bar_close_1m, 5)
            ema15_1m = _ema(bar_close_1m, 15)
            safe_c_1m = np.where(bar_close_1m == 0, np.nan, bar_close_1m)
            ema_cross_1m = (ema5_1m - ema15_1m) / safe_c_1m
            features[:, 33] = _map_bar_features_to_1s(ema_cross_1m, n, 60)

    # ── 5-minute bars (features 34-36) ──
    bars_5m = _resample_bars(opens, highs, lows, closes, volumes, 300)
    if bars_5m is not None:
        _, _, _, bar_close_5m, _ = bars_5m
        n_bars_5m = len(bar_close_5m)

        ret_5m_1 = _rolling_return(bar_close_5m, 1)
        ret_5m_3 = _rolling_return(bar_close_5m, 3)
        features[:, 34] = _map_bar_features_to_1s(ret_5m_1, n, 300)
        features[:, 35] = _map_bar_features_to_1s(ret_5m_3, n, 300)

        # Volatility on 5m returns
        ret_5m_1bar = _rolling_return(bar_close_5m, 1)
        vol_5m_3 = _rolling_std(ret_5m_1bar, 3)
        features[:, 36] = _map_bar_features_to_1s(vol_5m_3, n, 300)


def _rolling_autocorr(returns: np.ndarray, lag: int, window: int) -> np.ndarray:
    """Rolling lag-N autocorrelation of returns over ``window`` elements.

    Computes corr(ret[i-window+1:i+1], ret[i-window+1-lag:i+1-lag]) for each i.
    """
    n = len(returns)
    out = np.full(n, np.nan)
    need = window + lag
    if need > n:
        return out

    safe = np.nan_to_num(returns, nan=0.0)

    for i in range(need - 1, n):
        x = safe[i - window + 1 : i + 1]
        y = safe[i - window + 1 - lag : i + 1 - lag]
        mx = x.mean()
        my = y.mean()
        num = np.sum((x - mx) * (y - my))
        denom = np.sqrt(np.sum((x - mx) ** 2) * np.sum((y - my) ** 2))
        if denom > 1e-15:
            out[i] = num / denom
        else:
            out[i] = 0.0
    return out


def _vol_pctrank_1h(vol_60s: np.ndarray) -> np.ndarray:
    """Percentile rank of current vol_60s within last 3600s.

    Uses stride-based approach: samples vol_60s at 60-tick intervals
    to get ~60 values in the hour, then ranks the current value.
    """
    n = len(vol_60s)
    out = np.full(n, np.nan)
    # Need at least 3600 ticks for 1h of history
    if n < 3600:
        return out

    for i in range(3599, n):
        current = vol_60s[i]
        if np.isnan(current):
            continue
        # Sample vol_60s at 60-tick stride over the last 3600 ticks
        start = max(i - 3599, 0)
        samples = vol_60s[start : i + 1 : 60]
        valid = samples[~np.isnan(samples)]
        if len(valid) < 5:
            continue
        out[i] = np.sum(valid <= current) / len(valid)
    return out


# ---------------------------------------------------------------------------
# Batch computation (training)
# ---------------------------------------------------------------------------


def compute_batch(
    timestamps_ms: np.ndarray,
    opens: np.ndarray,
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    volumes: np.ndarray,
    trades_counts: np.ndarray,
    *,
    poly_mid: float = 0.0,
    poly_spread: float = 0.0,
    seconds_to_expiry: float = 300.0,
    total_seconds: float = 300.0,
) -> np.ndarray:
    """Compute feature matrix for an entire array of klines.

    Args:
        timestamps_ms: Binance open_time in milliseconds, shape (N,).
        opens, highs, lows, closes, volumes, trades_counts: shape (N,).
        poly_mid: Polymarket mid-price (constant for batch; 0 during training).
        poly_spread: Polymarket spread (constant for batch; 0 during training).
        seconds_to_expiry: Seconds until market resolves (constant for batch).
        total_seconds: Total market duration (for elapsed_fraction).

    Returns:
        Feature matrix of shape (N, NUM_FEATURES).  Rows with insufficient
        history contain NaN and must be dropped before training.
    """
    n = len(closes)
    features = np.full((n, NUM_FEATURES), np.nan, dtype=np.float64)

    # Pre-compute 1-second returns for volatility / momentum
    ret_1s = _rolling_return(closes, 1)

    # ----- Returns (features 0-5) -----
    for idx, lag in enumerate([1, 5, 15, 30, 60, 300]):
        features[:, idx] = _rolling_return(closes, lag)

    # ----- Volatility (features 6-9) -----
    for idx, window in enumerate([15, 30, 60, 300]):
        features[:, 6 + idx] = _rolling_std(ret_1s, window)

    # ----- Momentum = return / window (features 10-12) -----
    for idx, (lag, col) in enumerate([(5, 1), (15, 2), (60, 4)]):
        # Reuse already-computed returns columns, divide by window
        features[:, 10 + idx] = features[:, col] / lag

    # ----- Acceleration = momentum[t] - momentum[t-k] (features 13-14) -----
    mom_15 = features[:, 11].copy()
    mom_60 = features[:, 12].copy()
    accel_15 = np.full(n, np.nan)
    accel_15[15:] = mom_15[15:] - mom_15[:-15]
    features[:, 13] = accel_15
    accel_60 = np.full(n, np.nan)
    accel_60[60:] = mom_60[60:] - mom_60[:-60]
    features[:, 14] = accel_60

    # ----- Volume sums (features 15-17) -----
    for idx, window in enumerate([15, 60, 300]):
        features[:, 15 + idx] = _rolling_sum(volumes, window)

    # ----- Volume rate of change over 60s (feature 18) -----
    vol_sum_60 = features[:, 16].copy()
    vol_roc = np.full(n, np.nan)
    if n > 60:
        prev_vol = vol_sum_60[:-60]
        safe_prev = np.where(prev_vol == 0, np.nan, prev_vol)
        vol_roc[60:] = (vol_sum_60[60:] - prev_vol) / safe_prev
    features[:, 18] = vol_roc

    # ----- VWAP deviation (features 19-20) -----
    for idx, window in enumerate([60, 300]):
        vwap_val = _vwap(closes, volumes, window)
        safe_close = np.where(closes == 0, np.nan, closes)
        features[:, 19 + idx] = (closes - vwap_val) / safe_close

    # ----- Bollinger z-score (features 21-22) -----
    for idx, window in enumerate([60, 300]):
        sma = _rolling_mean(closes, window)
        std = _rolling_std(closes, window)
        safe_std = np.where(std == 0, np.nan, std)
        features[:, 21 + idx] = (closes - sma) / (2.0 * safe_std)

    # ----- EMA crossover (feature 23) -----
    ema_fast = _ema(closes, 12)
    ema_slow = _ema(closes, 26)
    safe_close = np.where(closes == 0, np.nan, closes)
    features[:, 23] = (ema_fast - ema_slow) / safe_close

    # ----- MACD signal (feature 24) -----
    macd_line = ema_fast - ema_slow
    signal_line = _ema(macd_line, 9)
    features[:, 24] = (macd_line - signal_line) / safe_close

    # ----- High-low range over 60s (feature 25) -----
    if n >= 60:
        from numpy.lib.stride_tricks import sliding_window_view

        highs_win = sliding_window_view(highs, 60)
        lows_win = sliding_window_view(lows, 60)
        rolling_high = np.max(highs_win, axis=1)
        rolling_low = np.min(lows_win, axis=1)
        sc = safe_close[59:]
        features[59:, 25] = (rolling_high - rolling_low) / sc

    # ----- Trades intensity over 60s (feature 26) -----
    trades_sum_60 = _rolling_sum(trades_counts.astype(np.float64), 60)
    trades_mean_60 = _rolling_mean(trades_counts.astype(np.float64), 60)
    safe_mean = np.where(trades_mean_60 == 0, np.nan, trades_mean_60)
    features[:, 26] = trades_sum_60 / safe_mean

    # =================================================================
    # v2 features: multi-timeframe (27-36)
    # =================================================================
    _compute_multi_tf_features(features, n, opens, highs, lows, closes, volumes)

    # =================================================================
    # v2 features: new single-timeframe (37-44)
    # =================================================================

    # ----- Volume imbalance (features 37-38) -----
    # (high - close) / (high - low): 1.0 = closed at low (selling), 0.0 = closed at high
    # When high == low (tick data or flat candle), default to 0.5 (neutral)
    hl_range = highs - lows
    no_range = hl_range < 1e-10
    safe_hl = np.where(no_range, 1.0, hl_range)  # avoid div-by-zero
    vi_raw = (highs - closes) / safe_hl
    features[:, 37] = np.where(no_range, 0.5, vi_raw)
    features[:, 38] = _rolling_mean(features[:, 37], 60)

    # ----- Autocorrelation (features 39-40) -----
    features[:, 39] = _rolling_autocorr(ret_1s, lag=1, window=60)
    features[:, 40] = _rolling_autocorr(ret_1s, lag=5, window=60)

    # ----- Volatility percentile rank over 1h (feature 41) -----
    vol_60s = features[:, 8].copy()  # reuse vol_60s computed earlier
    features[:, 41] = _vol_pctrank_1h(vol_60s)

    # ----- Price position in range (features 42-43) -----
    if n >= 300:
        from numpy.lib.stride_tricks import sliding_window_view

        h_win_300 = sliding_window_view(highs, 300)
        l_win_300 = sliding_window_view(lows, 300)
        rh_300 = np.max(h_win_300, axis=1)
        rl_300 = np.min(l_win_300, axis=1)
        range_300 = rh_300 - rl_300
        safe_range_300 = np.where(range_300 < 1e-10, np.nan, range_300)
        features[299:, 42] = (closes[299:] - rl_300) / safe_range_300

    if n >= 900:
        h_win_900 = sliding_window_view(highs, 900)
        l_win_900 = sliding_window_view(lows, 900)
        rh_900 = np.max(h_win_900, axis=1)
        rl_900 = np.min(l_win_900, axis=1)
        range_900 = rh_900 - rl_900
        safe_range_900 = np.where(range_900 < 1e-10, np.nan, range_900)
        features[899:, 43] = (closes[899:] - rl_900) / safe_range_900

    # ----- Avg trade size ratio (feature 44) -----
    # ratio of recent avg trade size (60s) to longer-term (300s)
    safe_trades = np.where(trades_counts == 0, np.nan, trades_counts.astype(np.float64))
    avg_trade_size = volumes / safe_trades
    avg_ts_60 = _rolling_mean(avg_trade_size, 60)
    avg_ts_300 = _rolling_mean(avg_trade_size, 300)
    safe_avg_300 = np.where(
        (avg_ts_300 == 0) | np.isnan(avg_ts_300), np.nan, avg_ts_300
    )
    features[:, 44] = avg_ts_60 / safe_avg_300

    # =================================================================
    # v3 features: candlestick microstructure (45-50)
    # =================================================================

    hl_range_cs = highs - lows
    no_range_cs = hl_range_cs < 1e-10
    safe_hl_cs = np.where(no_range_cs, 1.0, hl_range_cs)

    # ----- body_ratio (feature 45): |close-open| / (high-low) -----
    features[:, 45] = np.where(no_range_cs, 0.0, np.abs(closes - opens) / safe_hl_cs)

    # ----- upper_shadow_ratio (feature 46): (high - max(open,close)) / (high-low) -----
    # Clamp to 0 for robustness (open can exceed high in tick-level data)
    upper_shadow = np.maximum(highs - np.maximum(opens, closes), 0.0)
    features[:, 46] = np.where(no_range_cs, 0.0, upper_shadow / safe_hl_cs)

    # ----- lower_shadow_ratio (feature 47): (min(open,close) - low) / (high-low) -----
    lower_shadow = np.maximum(np.minimum(opens, closes) - lows, 0.0)
    features[:, 47] = np.where(no_range_cs, 0.0, lower_shadow / safe_hl_cs)

    # ----- close_location_60s (feature 48): rolling mean of (close-low)/(high-low) -----
    close_loc = np.where(no_range_cs, 0.5, (closes - lows) / safe_hl_cs)
    features[:, 48] = _rolling_mean(close_loc, 60)

    # ----- body_momentum_60s (feature 49): rolling mean of (close-open)/close -----
    safe_close_bm = np.where(closes == 0, np.nan, closes)
    body_mom = (closes - opens) / safe_close_bm
    features[:, 49] = _rolling_mean(body_mom, 60)

    # ----- shadow_imbalance_60s (feature 50): rolling mean of (upper-lower)/(high-low) -----
    shadow_imb = np.where(no_range_cs, 0.0, (upper_shadow - lower_shadow) / safe_hl_cs)
    features[:, 50] = _rolling_mean(shadow_imb, 60)

    # =================================================================
    # Orderbook features — pass-through (51-55)
    # During training these are 0; in live inference the FeatureEngine
    # overwrites 53-55 with values derived from real book history.
    # =================================================================

    _poly_mid = poly_mid if poly_mid is not None else 0.0
    _poly_spread = poly_spread if poly_spread is not None else 0.0
    features[:, 51] = _poly_mid
    features[:, 52] = _poly_spread
    # spread_pct = spread / mid (0 when mid is 0)
    features[:, 53] = (_poly_spread / _poly_mid) if _poly_mid > 0 else 0.0
    # mid_return_5s and spread_zscore_60s need historical book data — 0 during training
    features[:, 54] = 0.0
    features[:, 55] = 0.0

    # =================================================================
    # Time features (56-57)
    # =================================================================

    _ste = seconds_to_expiry if seconds_to_expiry is not None else 300.0
    _total = (
        total_seconds if (total_seconds is not None and total_seconds > 0) else 300.0
    )
    features[:, 56] = _ste
    features[:, 57] = 1.0 - (_ste / _total)

    return features


# ---------------------------------------------------------------------------
# Streaming computation (live inference)
# ---------------------------------------------------------------------------


class FeatureEngine:
    """Computes ML features from a rolling buffer of price ticks.

    Usage (live inference)::

        engine = FeatureEngine(buffer_size=4000)
        for tick in live_ticks:
            engine.update(tick.timestamp_ns, tick.price, tick.volume)
        engine.update_book(mid=0.50, spread=0.02)  # from Polymarket WS
        vec = engine.compute(seconds_to_expiry=120.0)
        if vec is not None:
            proba = model.predict_proba(vec.reshape(1, -1))

    The streaming mode mirrors the batch ``compute_batch`` logic exactly,
    operating on the rolling deque converted to numpy arrays.
    Book-derived features (53-55) are computed from an independent book
    history buffer updated via ``update_book()``.
    """

    def __init__(self, buffer_size: int = 4000) -> None:
        self._buffer_size = buffer_size
        # 1-second OHLCV bar buffers (matches training data format)
        self._timestamps: deque[int] = deque(maxlen=buffer_size)
        self._opens: deque[float] = deque(maxlen=buffer_size)
        self._prices: deque[float] = deque(maxlen=buffer_size)  # closes
        self._highs: deque[float] = deque(maxlen=buffer_size)
        self._lows: deque[float] = deque(maxlen=buffer_size)
        self._volumes: deque[float] = deque(maxlen=buffer_size)
        self._trades: deque[float] = deque(maxlen=buffer_size)

        # Current 1-second bar being aggregated
        self._bar_ts_s: int | None = None  # second being built
        self._bar_open: float = 0.0
        self._bar_high: float = 0.0
        self._bar_low: float = 0.0
        self._bar_close: float = 0.0
        self._bar_volume: float = 0.0
        self._bar_trades: float = 0.0

        # Book history for orderbook-derived features (updated via update_book)
        self._book_mids: deque[float] = deque(maxlen=120)
        self._book_spreads: deque[float] = deque(maxlen=120)
        self._latest_book_mid: float = 0.0
        self._latest_book_spread: float = 0.0

    @property
    def latest_price(self) -> float:
        """Most recent price seen (including current in-progress bar)."""
        if self._bar_ts_s is not None:
            return self._bar_close
        return self._prices[-1] if self._prices else 0.0

    @property
    def tick_count(self) -> int:
        """Number of 1-second bars available (finalized + pending)."""
        return len(self._prices) + (1 if self._bar_ts_s is not None else 0)

    def update(
        self,
        timestamp_ns: int,
        price: float,
        volume: float = 0.0,
        trades_count: float = 1.0,
        high: float | None = None,
        low: float | None = None,
    ) -> None:
        """Aggregate ticks into 1-second OHLCV bars (matching training data).

        Each bar captures the open/high/low/close within a 1-second window,
        producing proper candlestick microstructure features that match the
        Binance 1s klines used during training.
        """
        ts_s = timestamp_ns // 1_000_000_000
        tick_high = high if high is not None else price
        tick_low = low if low is not None else price

        if self._bar_ts_s is None:
            # First tick ever — start a new bar
            self._bar_ts_s = ts_s
            self._bar_open = price
            self._bar_high = tick_high
            self._bar_low = tick_low
            self._bar_close = price
            self._bar_volume = volume
            self._bar_trades = trades_count
            return

        if ts_s == self._bar_ts_s:
            # Same second — update current bar
            self._bar_high = max(self._bar_high, tick_high)
            self._bar_low = min(self._bar_low, tick_low)
            self._bar_close = price
            self._bar_volume += volume
            self._bar_trades += trades_count
        else:
            # New second — finalize current bar, then start a new one
            self._finalize_bar()
            self._bar_ts_s = ts_s
            self._bar_open = price
            self._bar_high = tick_high
            self._bar_low = tick_low
            self._bar_close = price
            self._bar_volume = volume
            self._bar_trades = trades_count

    def _finalize_bar(self) -> None:
        """Append the current 1-second bar to the rolling buffers."""
        if self._bar_ts_s is None:
            return
        # Store bar timestamp in nanoseconds (start of second)
        self._timestamps.append(self._bar_ts_s * 1_000_000_000)
        self._opens.append(self._bar_open)
        self._prices.append(self._bar_close)
        self._highs.append(self._bar_high)
        self._lows.append(self._bar_low)
        self._volumes.append(self._bar_volume)
        self._trades.append(self._bar_trades)

    def update_book(self, mid: float, spread: float) -> None:
        """Update orderbook history from a Polymarket book snapshot.

        Call this each time a new book update arrives (typically every ~1s).
        The mid/spread history is used to compute features 53-55.
        """
        self._book_mids.append(mid)
        self._book_spreads.append(spread)
        self._latest_book_mid = mid
        self._latest_book_spread = spread

    def compute(
        self,
        seconds_to_expiry: float = 300.0,
        total_seconds: float = 300.0,
    ) -> np.ndarray | None:
        """Return a (NUM_FEATURES,) feature vector, or None if insufficient data.

        Requires at least _MIN_TICKS 1-second bars (finalized + pending).
        The in-progress bar is included in the computation without mutating state.
        Book-derived features (53-55) are computed from ``update_book()`` history.
        """
        has_pending = self._bar_ts_s is not None
        total_bars = len(self._prices) + (1 if has_pending else 0)
        if total_bars < _MIN_TICKS:
            return None

        # Convert deques to numpy arrays
        opens = np.array(self._opens, dtype=np.float64)
        prices = np.array(self._prices, dtype=np.float64)
        h = np.array(self._highs, dtype=np.float64)
        l = np.array(self._lows, dtype=np.float64)
        volumes = np.array(self._volumes, dtype=np.float64)
        timestamps_ms = np.array(self._timestamps, dtype=np.int64) // 1_000_000
        trades = np.array(self._trades, dtype=np.float64)

        # Append in-progress bar without finalizing it
        if has_pending:
            opens = np.append(opens, self._bar_open)
            prices = np.append(prices, self._bar_close)
            h = np.append(h, self._bar_high)
            l = np.append(l, self._bar_low)
            volumes = np.append(volumes, self._bar_volume)
            timestamps_ms = np.append(timestamps_ms, self._bar_ts_s * 1_000)
            trades = np.append(trades, self._bar_trades)

        mat = compute_batch(
            timestamps_ms=timestamps_ms,
            opens=opens,
            highs=h,
            lows=l,
            closes=prices,
            volumes=volumes,
            trades_counts=trades,
            poly_mid=self._latest_book_mid,
            poly_spread=self._latest_book_spread,
            seconds_to_expiry=seconds_to_expiry,
            total_seconds=total_seconds,
        )

        # Return the last row (current bar's features)
        last_row = mat[-1]

        # Override orderbook-derived features (53-55) with live book data
        self._compute_book_features(last_row)

        # Safety: if any core feature is NaN, we don't have enough data
        # (features 51+ are orderbook/time pass-through — allowed to be 0/constant)
        if np.any(np.isnan(last_row[:_N_CORE_FEATURES])):
            return None

        return last_row

    def _compute_book_features(self, row: np.ndarray) -> None:
        """Compute live orderbook-derived features into the feature row.

        Overwrites indices 53-55 with values computed from book history:
          53: spread_pct   = spread / mid
          54: mid_return_5s = (mid[-1] - mid[-6]) / mid[-6]  (5-tick return)
          55: spread_zscore_60s = (spread - mean_60) / std_60
        """
        mid = self._latest_book_mid
        spread = self._latest_book_spread

        # spread_pct
        if mid > 0:
            row[53] = spread / mid
        else:
            row[53] = 0.0

        # mid_return_5s — need at least 6 book snapshots
        mids = self._book_mids
        if len(mids) >= 6 and mids[-6] > 0:
            row[54] = (mids[-1] - mids[-6]) / mids[-6]
        else:
            row[54] = 0.0

        # spread_zscore_60s — need at least 10 spread samples
        spreads = self._book_spreads
        if len(spreads) >= 10:
            arr = np.array(spreads, dtype=np.float64)
            mean_s = arr.mean()
            std_s = arr.std()
            if std_s > 1e-10:
                row[55] = (spread - mean_s) / std_s
            else:
                row[55] = 0.0
        else:
            row[55] = 0.0
