#!/usr/bin/env python3
"""
Historical backtest for the ML-enhanced BTC latency-arbitrage strategy.

Replays historical 1-second klines through:
  FeatureEngine → LightGBM inference → synthetic book → strategy eval
  → risk check → paper order → position resolution at 5m boundaries

Runs synchronously (no asyncio) for reproducibility and speed.

Usage:
    python tools/backtest.py \\
        --model models/btc_5m_v1.pkl \\
        --start 2026-01-01 --end 2026-02-28 \\
        --capital 10000

    # Compare ML vs no-ML (flat p_up = 0.50):
    python tools/backtest.py --no-ml --start 2026-01-01 --end 2026-02-28
"""

from __future__ import annotations

import argparse
import asyncio
import math
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

# Ensure project root is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import joblib

PG_DSN = "postgresql://postgres:postgres@localhost:6501/crypto_arbitrage"

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class KlineData:
    """Mirrors train_model.py KlineData."""

    timestamps_ms: np.ndarray  # int64
    opens: np.ndarray
    highs: np.ndarray
    lows: np.ndarray
    closes: np.ndarray
    volumes: np.ndarray
    trades_counts: np.ndarray


@dataclass
class BacktestPosition:
    """A single open paper position."""

    token_outcome: str  # "YES" or "NO"
    side: str  # "BUY" or "SELL"
    entry_price: float  # price paid/received
    size: float  # USDC size
    entry_idx: int  # kline index at entry
    market_start_idx: int  # kline index when the 5m window started
    market_end_idx: int  # kline index when the 5m window expires
    condition_id: str = ""


@dataclass
class BacktestTrade:
    """A resolved trade for metrics."""

    entry_idx: int
    exit_idx: int
    token_outcome: str
    side: str
    entry_price: float
    settlement: float  # 0.0 or 1.0
    size: float
    pnl: float
    edge: float
    p_up: float
    btc_went_up: bool
    entry_btc: float
    exit_btc: float


@dataclass
class BacktestMetrics:
    """Aggregate backtest results."""

    total_pnl: float = 0.0
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    total_volume: float = 0.0
    max_drawdown: float = 0.0
    peak_pnl: float = 0.0
    brier_sum: float = 0.0  # for Brier score (classification)
    brier_count: int = 0
    hourly_pnl: dict = field(default_factory=lambda: {h: 0.0 for h in range(24)})
    hourly_trades: dict = field(default_factory=lambda: {h: 0 for h in range(24)})
    # Calibration by decile (classification)
    calib_buckets: list = field(
        default_factory=lambda: [
            {"pred_sum": 0.0, "actual_sum": 0.0, "count": 0} for _ in range(10)
        ]
    )
    # Regression tracking
    pred_returns: list = field(default_factory=list)
    actual_returns: list = field(default_factory=list)

    @property
    def win_rate(self) -> float:
        return self.winning_trades / self.total_trades if self.total_trades > 0 else 0.0

    @property
    def brier_score(self) -> float:
        return self.brier_sum / self.brier_count if self.brier_count > 0 else 1.0

    @property
    def regression_rmse(self) -> float:
        """RMSE of predicted vs actual returns."""
        if not self.pred_returns:
            return float("nan")
        p = np.array(self.pred_returns)
        a = np.array(self.actual_returns)
        return float(np.sqrt(np.mean((p - a) ** 2)))

    @property
    def regression_direction_accuracy(self) -> float:
        """Fraction of predictions with correct sign."""
        if not self.pred_returns:
            return 0.0
        p = np.array(self.pred_returns)
        a = np.array(self.actual_returns)
        return float(np.mean(np.sign(p) == np.sign(a)))

    def record_trade(self, pnl: float, volume: float, hour: int) -> None:
        self.total_trades += 1
        self.total_pnl += pnl
        self.total_volume += volume
        self.hourly_pnl[hour] += pnl
        self.hourly_trades[hour] += 1
        if pnl > 0:
            self.winning_trades += 1
        else:
            self.losing_trades += 1
        # Drawdown tracking
        if self.total_pnl > self.peak_pnl:
            self.peak_pnl = self.total_pnl
        dd = self.peak_pnl - self.total_pnl
        if dd > self.max_drawdown:
            self.max_drawdown = dd

    def record_prediction(self, p_up: float, actual_up: bool) -> None:
        """Track Brier score and calibration (classification mode)."""
        actual = 1.0 if actual_up else 0.0
        self.brier_sum += (p_up - actual) ** 2
        self.brier_count += 1
        # Calibration bucket (decile)
        bucket_idx = min(int(p_up * 10), 9)
        self.calib_buckets[bucket_idx]["pred_sum"] += p_up
        self.calib_buckets[bucket_idx]["actual_sum"] += actual
        self.calib_buckets[bucket_idx]["count"] += 1

    def record_regression_prediction(
        self, pred_return: float, actual_return: float
    ) -> None:
        """Track predicted vs actual return (regression mode)."""
        self.pred_returns.append(pred_return)
        self.actual_returns.append(actual_return)


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------


async def _load_klines(start_ms: int, end_ms: int) -> KlineData:
    """Load klines from PostgreSQL."""
    import asyncpg

    conn = await asyncpg.connect(PG_DSN)
    try:
        rows = await conn.fetch(
            """
            SELECT open_time_ms, open, high, low, close, volume, trades_count
            FROM btc_klines
            WHERE interval = '1s'
              AND open_time_ms >= $1
              AND open_time_ms < $2
            ORDER BY open_time_ms ASC
            """,
            start_ms,
            end_ms,
        )
    finally:
        await conn.close()

    if not rows:
        raise ValueError("No klines found in the given date range")

    n = len(rows)
    ts = np.empty(n, dtype=np.int64)
    o = np.empty(n, dtype=np.float64)
    h = np.empty(n, dtype=np.float64)
    lo = np.empty(n, dtype=np.float64)
    c = np.empty(n, dtype=np.float64)
    v = np.empty(n, dtype=np.float64)
    tc = np.empty(n, dtype=np.float64)
    for i, r in enumerate(rows):
        ts[i] = r[0]
        o[i] = float(r[1])
        h[i] = float(r[2])
        lo[i] = float(r[3])
        c[i] = float(r[4])
        v[i] = float(r[5])
        tc[i] = float(r[6])

    return KlineData(ts, o, h, lo, c, v, tc)


def load_klines(start_ms: int, end_ms: int) -> KlineData:
    """Synchronous wrapper."""
    return asyncio.run(_load_klines(start_ms, end_ms))


# ---------------------------------------------------------------------------
# ML inference (batch)
# ---------------------------------------------------------------------------


def batch_ml_predict(
    data: KlineData,
    model_artifact: dict,
    chunk_size: int = 2_000_000,
) -> tuple[np.ndarray, str]:
    """Run batch ML inference on all klines.

    Auto-detects model type from artifact:
      - regression: returns raw predicted returns
      - classification: returns P(up) probabilities

    Returns:
        (predictions, model_type) — predictions shape (N,), NaN where lookback
        is insufficient.
    """
    from src.ml.features import compute_batch, _MIN_TICKS

    model = model_artifact["model"]
    model_type = model_artifact.get("model_type", "classification")
    calibrator = model_artifact.get("calibrator") if model_type == "classification" else None
    n = len(data.timestamps_ms)

    preds = np.full(n, np.nan, dtype=np.float64)

    # Process in chunks (with lookback overlap so features at chunk boundaries
    # have enough history).  We always include `lookback` extra rows before the
    # "new" region so rolling features are warm.
    lookback = _MIN_TICKS - 1  # 300
    processed = 0

    while processed < n:
        # chunk_start includes lookback rows that we already processed
        chunk_start = max(0, processed - lookback)
        chunk_end = min(n, processed + chunk_size)
        sl = slice(chunk_start, chunk_end)

        # Compute features for this chunk
        features = compute_batch(
            data.timestamps_ms[sl],
            data.opens[sl],
            data.highs[sl],
            data.lows[sl],
            data.closes[sl],
            data.volumes[sl],
            data.trades_counts[sl],
            poly_mid=None,
            poly_spread=None,
            seconds_to_expiry=None,
            total_seconds=None,
        )

        # features shape: (chunk_len, NUM_FEATURES), first rows may be NaN
        valid_mask = ~np.isnan(features[:, 0])
        if valid_mask.any():
            valid_features = features[valid_mask]

            if model_type == "regression":
                chunk_preds = model.predict(valid_features)
            else:
                chunk_preds = model.predict_proba(valid_features)[:, 1]
                if calibrator is not None:
                    chunk_preds = calibrator.predict(chunk_preds)

            # Map chunk-local valid indices → global indices
            local_valid_idx = np.where(valid_mask)[0]  # indices within chunk
            global_valid_idx = (
                local_valid_idx + chunk_start
            )  # indices within full array

            # Only write results for rows in the "new" region (>= processed)
            # to avoid redundant writes on the lookback overlap.
            new_mask = global_valid_idx >= processed
            preds[global_valid_idx[new_mask]] = chunk_preds[new_mask]

        processed = chunk_end

    return preds, model_type


# ---------------------------------------------------------------------------
# Synthetic book simulation
# ---------------------------------------------------------------------------


class SyntheticBookSim:
    """Simulates a lagged Polymarket book for one 5m market window.

    The book's mid-price uses an EMA that lags behind the true P(up),
    creating the mispricing that the strategy tries to exploit.
    """

    def __init__(
        self,
        ema_alpha: float = 0.05,
        noise_std: float = 0.02,
        spread_pct: float = 0.04,
        seed: int | None = None,
    ):
        self._alpha = ema_alpha
        self._noise_std = noise_std
        self._spread_pct = spread_pct
        self._rng = np.random.default_rng(seed)
        self._ema_price: float | None = None

    def update(self, true_p_up: float) -> dict:
        """Feed true P(up) and return synthetic book prices.

        Returns dict with keys: yes_mid, yes_bid, yes_ask,
                                 no_mid, no_bid, no_ask, spread
        """
        p = np.clip(true_p_up, 0.02, 0.98)

        # EMA lags behind
        if self._ema_price is None:
            self._ema_price = p
        else:
            self._ema_price = self._alpha * p + (1 - self._alpha) * self._ema_price

        # Add noise
        noise = self._rng.normal(0, self._noise_std)
        yes_mid = np.clip(self._ema_price + noise, 0.02, 0.98)
        no_mid = 1.0 - yes_mid

        half_spread = self._spread_pct / 2
        yes_bid = max(0.01, yes_mid - half_spread)
        yes_ask = min(0.99, yes_mid + half_spread)
        no_bid = max(0.01, no_mid - half_spread)
        no_ask = min(0.99, no_mid + half_spread)

        return {
            "yes_mid": yes_mid,
            "yes_bid": yes_bid,
            "yes_ask": yes_ask,
            "no_mid": no_mid,
            "no_bid": no_bid,
            "no_ask": no_ask,
            "spread": yes_ask - yes_bid,
        }


# ---------------------------------------------------------------------------
# Strategy evaluation (standalone, no async)
# ---------------------------------------------------------------------------


def compute_p_up(
    predicted_return: float,
    confidence: float,
    btc_volatility: float,
    seconds_remaining: float,
) -> float:
    """Mirror of StrategyEngine._compute_p_up (from strategy.py).

    P(up) = Phi(z) where z = (ret / (vol * sqrt(T))) * confidence
    """
    from scipy.stats import norm

    if seconds_remaining <= 0 or btc_volatility <= 0:
        return 0.5

    z = (
        predicted_return / (btc_volatility * math.sqrt(seconds_remaining))
    ) * confidence
    p = norm.cdf(z)
    return float(np.clip(p, 0.01, 0.99))


def evaluate_signal(
    p_up: float,
    book: dict,
    min_edge: float,
    max_edge: float,
    max_spread: float,
) -> dict | None:
    """Evaluate whether there's a tradeable edge.

    Returns signal dict or None.
    """
    if book["spread"] > max_spread:
        return None

    # Evaluate YES token
    yes_fair = p_up
    yes_edge = yes_fair - book["yes_ask"]
    if min_edge <= yes_edge <= max_edge:
        return {
            "outcome": "YES",
            "side": "BUY",
            "edge": yes_edge,
            "price": book["yes_ask"],
            "fair_value": yes_fair,
        }

    # Evaluate NO token
    no_fair = 1.0 - p_up
    no_edge = no_fair - book["no_ask"]
    if min_edge <= no_edge <= max_edge:
        return {
            "outcome": "NO",
            "side": "BUY",
            "edge": no_edge,
            "price": book["no_ask"],
            "fair_value": no_fair,
        }

    return None


# ---------------------------------------------------------------------------
# Position sizing (simplified from RiskManager)
# ---------------------------------------------------------------------------


def compute_position_size(
    capital: float,
    max_position_pct: float,
    confidence: float,
    edge: float,
) -> float:
    """Simplified Kelly-inspired sizing.

    base = capital * max_position_pct
    scale by confidence
    """
    base = capital * max_position_pct
    size = base * min(confidence, 1.0)
    return max(0.01, round(size, 2))


# ---------------------------------------------------------------------------
# Main backtest loop
# ---------------------------------------------------------------------------


def run_backtest(
    data: KlineData,
    p_up_array: np.ndarray,
    *,
    capital: float = 10_000.0,
    market_window_s: int = 300,
    min_edge: float = 0.02,
    max_edge: float = 0.30,
    max_spread: float = 0.10,
    max_position_pct: float = 0.005,
    max_open_positions: int = 20,
    max_daily_loss_pct: float = 0.02,
    ema_alpha: float = 0.05,
    noise_std: float = 0.02,
    spread_pct: float = 0.04,
    fill_rate: float = 0.95,
    slippage_pct: float = 0.005,
    cooldown_ticks: int = 30,
    seed: int = 42,
    model_type: str = "classification",
    max_predicted_return: float = 0.01,
) -> tuple[BacktestMetrics, list[BacktestTrade]]:
    """Run the full backtest.

    Args:
        data: Historical kline data (1s resolution).
        p_up_array: ML predictions for each tick (NaN = no prediction).
            For classification: P(up) probabilities.
            For regression: raw predicted returns.
        capital: Starting capital in USDC.
        market_window_s: Simulated market duration (5m = 300s).
        min_edge / max_edge / max_spread: Strategy thresholds.
        max_position_pct: Per-trade risk limit.
        max_open_positions: Max concurrent positions.
        max_daily_loss_pct: Daily loss halt threshold.
        ema_alpha: Synthetic book EMA smoothing.
        noise_std: Synthetic book noise.
        spread_pct: Synthetic book bid-ask spread.
        fill_rate: Simulated fill probability.
        slippage_pct: Slippage on fills.
        cooldown_ticks: Ticks to skip after a trade.
        seed: RNG seed for reproducibility.

    Returns:
        (metrics, trades_list)
    """
    rng = np.random.default_rng(seed)
    n = len(data.timestamps_ms)
    metrics = BacktestMetrics()
    trades: list[BacktestTrade] = []
    open_positions: list[BacktestPosition] = []

    # Volatility estimation (rolling 60s std of returns)
    vol_window = 60
    returns = np.zeros(n, dtype=np.float64)
    returns[1:] = (data.closes[1:] - data.closes[:-1]) / data.closes[:-1]
    rolling_vol = np.full(n, np.nan)
    for i in range(vol_window, n):
        rolling_vol[i] = np.std(returns[i - vol_window + 1 : i + 1])

    # Track daily P&L for halt logic
    current_day: str = ""
    daily_pnl: float = 0.0
    is_halted: bool = False

    # Market window tracking
    # Each "market" spans market_window_s ticks (1 tick = 1 second)
    # We align to market_window_s boundaries
    market_start_idx = 0
    book_sim = SyntheticBookSim(ema_alpha, noise_std, spread_pct, seed=seed)
    cooldown_until = 0

    # Find the first valid tick (where we have ML predictions + vol)
    from src.ml.features import _MIN_TICKS

    first_valid = max(vol_window, _MIN_TICKS)  # need full feature lookback + vol window

    print(f"\nBacktest: {n:,} ticks ({n / 86400:.1f} days)")
    print(f"Capital: ${capital:,.2f} | Market window: {market_window_s}s")
    print(f"Edge: [{min_edge:.3f}, {max_edge:.3f}] | Max spread: {max_spread:.3f}")
    print(f"ML predictions available: {(~np.isnan(p_up_array)).sum():,} / {n:,}")
    print()

    last_progress = 0
    t0 = time.time()

    for i in range(first_valid, n):
        # Progress logging
        pct = int(i / n * 100)
        if pct >= last_progress + 10:
            elapsed = time.time() - t0
            ticks_per_sec = i / elapsed if elapsed > 0 else 0
            eta_s = (n - i) / ticks_per_sec if ticks_per_sec > 0 else 0
            print(
                f"  {pct}% ({i:,}/{n:,}) | "
                f"PnL=${metrics.total_pnl:+.2f} | "
                f"Trades={metrics.total_trades} | "
                f"WR={metrics.win_rate:.1%} | "
                f"Speed={ticks_per_sec:,.0f} ticks/s | "
                f"ETA={eta_s:.0f}s"
            )
            last_progress = pct

        # ── Day boundary: reset daily halt ─────────────────────────────
        ts_s = data.timestamps_ms[i] / 1000
        day_str = datetime.fromtimestamp(ts_s, tz=timezone.utc).strftime("%Y-%m-%d")
        if day_str != current_day:
            current_day = day_str
            daily_pnl = 0.0
            is_halted = False

        if is_halted:
            continue

        # ── Market window boundary: resolve expired positions ──────────
        ticks_in_market = i - market_start_idx
        if ticks_in_market >= market_window_s:
            # Resolve all positions from this market window
            btc_now = data.closes[i]
            btc_at_start = data.closes[market_start_idx]
            btc_went_up = btc_now > btc_at_start

            remaining = []
            for pos in open_positions:
                if pos.market_end_idx <= i:
                    # Settlement
                    if pos.token_outcome == "YES":
                        settlement = 1.0 if btc_went_up else 0.0
                    else:
                        settlement = 0.0 if btc_went_up else 1.0

                    if pos.side == "BUY":
                        pnl = (settlement - pos.entry_price) * pos.size
                    else:
                        pnl = (pos.entry_price - settlement) * pos.size

                    # Apply slippage
                    pnl -= abs(pnl) * slippage_pct

                    hour = datetime.fromtimestamp(ts_s, tz=timezone.utc).hour
                    metrics.record_trade(pnl, pos.size, hour)
                    daily_pnl += pnl

                    trades.append(
                        BacktestTrade(
                            entry_idx=pos.entry_idx,
                            exit_idx=i,
                            token_outcome=pos.token_outcome,
                            side=pos.side,
                            entry_price=pos.entry_price,
                            settlement=settlement,
                            size=pos.size,
                            pnl=pnl,
                            edge=0.0,  # filled below if needed
                            p_up=(
                                p_up_array[pos.entry_idx]
                                if not np.isnan(p_up_array[pos.entry_idx])
                                else 0.5
                            ),
                            btc_went_up=btc_went_up,
                            entry_btc=data.closes[pos.entry_idx],
                            exit_btc=btc_now,
                        )
                    )
                else:
                    remaining.append(pos)

            open_positions = remaining

            # Start new market window
            market_start_idx = i
            book_sim = SyntheticBookSim(ema_alpha, noise_std, spread_pct, seed=seed + i)

            # Check daily loss halt
            if daily_pnl < -(capital * max_daily_loss_pct):
                is_halted = True
                continue

        # ── Skip if no ML prediction or no vol ─────────────────────────
        raw_pred = p_up_array[i]
        if np.isnan(raw_pred):
            if model_type == "regression":
                continue  # no prediction available
            else:
                raw_pred = 0.5  # flat baseline for classification

        vol = rolling_vol[i]
        if np.isnan(vol) or vol <= 0:
            continue

        # ── Compute strategy P(up) from ML prediction ──────────────────
        if model_type == "regression":
            # Regression: raw_pred IS the predicted return
            predicted_return = max(
                -max_predicted_return,
                min(raw_pred, max_predicted_return),
            )
            confidence = 1.0  # no triple-counting
            p_up_val = 0.5 + predicted_return / (2.0 * max_predicted_return)  # for tracking
        else:
            # Classification: raw_pred is P(up)
            p_up_val = raw_pred
            predicted_return = (p_up_val - 0.5) * 2 * max_predicted_return
            confidence = abs(p_up_val - 0.5) * 2
        seconds_remaining = max(1, market_window_s - ticks_in_market)

        strategy_p_up = compute_p_up(
            predicted_return, confidence, vol, seconds_remaining
        )

        # ── Brier score tracking ───────────────────────────────────────
        # We can only compute actual outcome at market boundary, but we
        # track each ML prediction for aggregate Brier score
        # (actual outcome determined retroactively)

        # ── Generate synthetic book ────────────────────────────────────
        book = book_sim.update(strategy_p_up)

        # ── Evaluate signal ────────────────────────────────────────────
        if i < cooldown_until:
            continue

        if len(open_positions) >= max_open_positions:
            continue

        signal = evaluate_signal(strategy_p_up, book, min_edge, max_edge, max_spread)
        if signal is None:
            continue

        # ── Position sizing ────────────────────────────────────────────
        remaining_capital = (
            capital + metrics.total_pnl - sum(p.size for p in open_positions)
        )
        if remaining_capital <= 0:
            continue

        size = compute_position_size(
            remaining_capital, max_position_pct, confidence, signal["edge"]
        )
        if size < 0.01:
            continue

        # ── Simulate fill ──────────────────────────────────────────────
        if rng.random() > fill_rate:
            continue  # no fill

        entry_price = signal["price"] * (1 + slippage_pct)  # adverse slippage
        entry_price = np.clip(entry_price, 0.01, 0.99)

        # Recalculate edge after slippage
        actual_edge = signal["fair_value"] - entry_price
        if actual_edge < min_edge * 0.5:
            continue  # edge eroded by slippage

        market_end = market_start_idx + market_window_s
        open_positions.append(
            BacktestPosition(
                token_outcome=signal["outcome"],
                side=signal["side"],
                entry_price=entry_price,
                size=size,
                entry_idx=i,
                market_start_idx=market_start_idx,
                market_end_idx=market_end,
            )
        )

        cooldown_until = i + cooldown_ticks

    # ── Force-resolve remaining open positions ─────────────────────────
    btc_final = data.closes[-1]
    for pos in open_positions:
        btc_at_start = data.closes[pos.market_start_idx]
        btc_went_up = btc_final > btc_at_start
        if pos.token_outcome == "YES":
            settlement = 1.0 if btc_went_up else 0.0
        else:
            settlement = 0.0 if btc_went_up else 1.0
        if pos.side == "BUY":
            pnl = (settlement - pos.entry_price) * pos.size
        else:
            pnl = (pos.entry_price - settlement) * pos.size
        pnl -= abs(pnl) * slippage_pct

        ts_s = data.timestamps_ms[-1] / 1000
        hour = datetime.fromtimestamp(ts_s, tz=timezone.utc).hour
        metrics.record_trade(pnl, pos.size, hour)
        trades.append(
            BacktestTrade(
                entry_idx=pos.entry_idx,
                exit_idx=len(data.timestamps_ms) - 1,
                token_outcome=pos.token_outcome,
                side=pos.side,
                entry_price=pos.entry_price,
                settlement=settlement,
                size=pos.size,
                pnl=pnl,
                edge=0.0,
                p_up=(
                    p_up_array[pos.entry_idx]
                    if not np.isnan(p_up_array[pos.entry_idx])
                    else 0.5
                ),
                btc_went_up=btc_went_up,
                entry_btc=data.closes[pos.entry_idx],
                exit_btc=btc_final,
            )
        )

    # ── Compute Brier score from trades ────────────────────────────────
    for t in trades:
        actual_up = t.btc_went_up
        metrics.record_prediction(t.p_up, actual_up)

    return metrics, trades


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def print_report(
    metrics: BacktestMetrics,
    trades: list[BacktestTrade],
    capital: float,
    duration_s: float,
    use_ml: bool,
) -> None:
    """Print comprehensive backtest report."""
    sep = "=" * 62
    print(f"\n{sep}")
    print("            BACKTEST RESULTS")
    print(sep)
    print(f"  Mode:               {'ML-enhanced' if use_ml else 'No-ML baseline'}")
    print(f"  Starting capital:   ${capital:,.2f}")
    print(f"  Final balance:      ${capital + metrics.total_pnl:,.2f}")
    pnl_sign = "+" if metrics.total_pnl >= 0 else ""
    pnl_pct = metrics.total_pnl / capital * 100 if capital > 0 else 0
    print(
        f"  Total P&L:          {pnl_sign}${metrics.total_pnl:,.4f} ({pnl_pct:+.2f}%)"
    )
    print(
        f"  Max drawdown:       ${metrics.max_drawdown:,.4f} ({metrics.max_drawdown / capital * 100:.2f}%)"
    )
    print(sep)
    print(f"  Total trades:       {metrics.total_trades}")
    print(f"  Winning trades:     {metrics.winning_trades}")
    print(f"  Losing trades:      {metrics.losing_trades}")
    print(f"  Win rate:           {metrics.win_rate:.1%}")
    print(f"  Total volume:       ${metrics.total_volume:,.2f}")
    print(
        f"  Avg trade size:     ${metrics.total_volume / metrics.total_trades:,.2f}"
        if metrics.total_trades > 0
        else "  Avg trade size:     N/A"
    )
    avg_pnl = (
        metrics.total_pnl / metrics.total_trades if metrics.total_trades > 0 else 0
    )
    print(f"  Avg P&L per trade:  ${avg_pnl:+.4f}")
    print(sep)

    # Profit factor
    gross_profit = sum(t.pnl for t in trades if t.pnl > 0)
    gross_loss = abs(sum(t.pnl for t in trades if t.pnl < 0))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")
    print(f"  Profit factor:      {profit_factor:.2f}")

    # Sharpe ratio (annualized from per-trade returns)
    if trades:
        returns = np.array([t.pnl / t.size for t in trades if t.size > 0])
        if len(returns) > 1:
            sharpe = (np.mean(returns) / np.std(returns)) * np.sqrt(
                252 * 288
            )  # 288 = 5m windows/day
            print(f"  Sharpe ratio:       {sharpe:.2f}")
        else:
            print("  Sharpe ratio:       N/A")

    print(sep)
    print(
        f"  Brier score:        {metrics.brier_score:.4f} {'(< 0.25 = better than coin flip)' if metrics.brier_score < 0.25 else '(>= 0.25 = no better than coin flip)'}"
    )
    print(f"  Predictions scored: {metrics.brier_count}")

    # Calibration by decile
    print(f"\n  {'Calibration by P(up) decile':^50}")
    print(f"  {'Bucket':>10} {'Pred Avg':>10} {'Actual':>10} {'Count':>8} {'Error':>8}")
    for i, b in enumerate(metrics.calib_buckets):
        if b["count"] > 0:
            pred_avg = b["pred_sum"] / b["count"]
            actual_avg = b["actual_sum"] / b["count"]
            err = pred_avg - actual_avg
            label = f"{i * 10}-{(i + 1) * 10}%"
            print(
                f"  {label:>10} {pred_avg:>10.3f} {actual_avg:>10.3f} {b['count']:>8} {err:>+8.3f}"
            )

    # Hourly breakdown
    print(f"\n  {'P&L by hour (UTC)':^50}")
    print(f"  {'Hour':>6} {'PnL':>10} {'Trades':>8}")
    for h in range(24):
        if metrics.hourly_trades[h] > 0:
            print(
                f"  {h:>4}:00 ${metrics.hourly_pnl[h]:>+9.4f} {metrics.hourly_trades[h]:>8}"
            )

    # Trade outcome distribution
    if trades:
        yes_trades = [t for t in trades if t.token_outcome == "YES"]
        no_trades = [t for t in trades if t.token_outcome == "NO"]
        print(f"\n  {'Trade breakdown':^50}")
        print(
            f"  YES trades: {len(yes_trades)} (win rate: {sum(1 for t in yes_trades if t.pnl > 0) / len(yes_trades):.1%})"
            if yes_trades
            else "  YES trades: 0"
        )
        print(
            f"  NO trades:  {len(no_trades)} (win rate: {sum(1 for t in no_trades if t.pnl > 0) / len(no_trades):.1%})"
            if no_trades
            else "  NO trades:  0"
        )

    print(sep)

    # Pass/fail criteria
    print("\n  DEPLOYMENT CRITERIA:")
    checks = [
        ("Brier score < 0.24", metrics.brier_score < 0.24),
        ("Win rate > 52%", metrics.win_rate > 0.52),
        ("Max drawdown < 5%", metrics.max_drawdown < capital * 0.05),
    ]
    if trades:
        returns = np.array([t.pnl / t.size for t in trades if t.size > 0])
        if len(returns) > 1:
            sharpe = (np.mean(returns) / np.std(returns)) * np.sqrt(252 * 288)
            checks.append(("Sharpe ratio > 1.0", sharpe > 1.0))

    all_pass = True
    for label, passed in checks:
        status = "✓ PASS" if passed else "✗ FAIL"
        print(f"    {status}  {label}")
        if not passed:
            all_pass = False

    print(
        f"\n  {'ALL CRITERIA MET — Ready for deployment' if all_pass else 'SOME CRITERIA NOT MET — Review before deployment'}"
    )
    print(sep)
    print(f"\n  Backtest completed in {duration_s:.1f}s")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backtest ML-enhanced BTC latency-arbitrage strategy"
    )
    parser.add_argument(
        "--model",
        default="models/btc_5m_v2.pkl",
        help="Path to trained model artifact (default: models/btc_5m_v2.pkl)",
    )
    parser.add_argument(
        "--start",
        required=True,
        help="Start date (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--end",
        required=True,
        help="End date (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--capital",
        type=float,
        default=10_000.0,
        help="Starting capital in USDC (default: 10000)",
    )
    parser.add_argument(
        "--no-ml",
        action="store_true",
        help="Run without ML (flat P(up)=0.50 baseline for comparison)",
    )
    parser.add_argument(
        "--market-window",
        type=int,
        default=300,
        help="Simulated market window in seconds (default: 300 = 5min)",
    )
    parser.add_argument(
        "--min-edge",
        type=float,
        default=0.02,
        help="Minimum edge threshold (default: 0.02)",
    )
    parser.add_argument(
        "--max-edge",
        type=float,
        default=0.30,
        help="Maximum edge threshold (default: 0.30)",
    )
    parser.add_argument(
        "--max-spread",
        type=float,
        default=0.10,
        help="Maximum spread threshold (default: 0.10)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed for reproducibility (default: 42)",
    )

    args = parser.parse_args()

    # Parse dates to milliseconds
    start_dt = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end_dt = datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)

    print(f"Loading klines from {args.start} to {args.end}...")
    t0 = time.time()
    data = load_klines(start_ms, end_ms)
    print(f"Loaded {len(data.timestamps_ms):,} klines in {time.time() - t0:.1f}s")

    # ML predictions
    use_ml = not args.no_ml
    model_type = "classification"  # default for no-ML baseline
    if use_ml:
        model_path = Path(args.model)
        if not model_path.exists():
            print(f"ERROR: Model file not found: {model_path}")
            sys.exit(1)

        print(f"Loading model from {model_path}...")
        artifact = joblib.load(model_path)
        model_type = artifact.get("model_type", "classification")
        model_metrics = artifact.get("metrics", {})
        print(f"  Model version: {artifact.get('version', '?')}")
        print(f"  Model type:    {model_type}")
        if model_type == "regression":
            print(f"  Training RMSE:  {model_metrics.get('avg_rmse', '?')}")
            print(f"  Training R²:    {model_metrics.get('avg_r2', '?')}")
            print(f"  Training IC:    {model_metrics.get('avg_ic', '?')}")
        else:
            print(f"  Training Brier: {model_metrics.get('avg_brier', '?')}")
            print(f"  Training AUC:   {model_metrics.get('avg_auc', '?')}")

        print("Running batch ML inference...")
        t1 = time.time()
        pred_array, model_type = batch_ml_predict(data, artifact)
        valid = (~np.isnan(pred_array)).sum()
        print(f"ML inference done: {valid:,} predictions in {time.time() - t1:.1f}s")
    else:
        print("No-ML mode: using flat P(up) = 0.50 for all ticks")
        pred_array = np.full(len(data.timestamps_ms), 0.5)

    # Run backtest
    print("\nRunning backtest...")
    t2 = time.time()
    metrics, trades = run_backtest(
        data,
        pred_array,
        capital=args.capital,
        market_window_s=args.market_window,
        min_edge=args.min_edge,
        max_edge=args.max_edge,
        max_spread=args.max_spread,
        seed=args.seed,
        model_type=model_type,
    )
    bt_duration = time.time() - t2

    # Report
    print_report(metrics, trades, args.capital, bt_duration, use_ml)


if __name__ == "__main__":
    main()
