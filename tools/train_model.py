#!/usr/bin/env python3
"""
Train a LightGBM model for BTC return prediction (regression or classification).

Uses walk-forward cross-validation to ensure no future data leakage.
Produces a model artifact (.pkl) that can be loaded by MLPredictor.

Usage:
    python tools/train_model.py --horizon 300 --output models/btc_5m_v4_reg.pkl
    python tools/train_model.py --horizon 300 --dead-zone 0.003 --output models/btc_5m_v4_reg.pkl
    python tools/train_model.py --horizon 300 --dead-zone 0.003 --optuna --optuna-trials 50

Prerequisites:
    1. PostgreSQL running (docker compose up -d postgres)
    2. Historical data loaded (python tools/collect_data.py)

Dependencies (pip):
    lightgbm, scikit-learn, asyncpg, joblib, numpy
    Optional: optuna (for --optuna flag)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import joblib
import lightgbm as lgb
import numpy as np
from scipy.stats import pearsonr
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score

# Allow running from project root.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.ml.features import (  # noqa: E402
    FEATURE_NAMES,
    NUM_FEATURES,
    _MIN_TICKS,
    _N_CORE_FEATURES,
    compute_batch,
)

try:
    import asyncpg  # noqa: E402
except ImportError:
    print("ERROR: asyncpg is required. Install with: pip install asyncpg")
    sys.exit(1)

PG_DSN = "postgresql://postgres:postgres@localhost:6501/crypto_arbitrage"

logger = logging.getLogger("train_model")

# _N_CORE_FEATURES is imported from src.ml.features (currently 51)


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------


@dataclass
class KlineData:
    """Numpy arrays of kline data loaded from PostgreSQL."""

    timestamps_ms: np.ndarray
    opens: np.ndarray
    highs: np.ndarray
    lows: np.ndarray
    closes: np.ndarray
    volumes: np.ndarray
    trades_counts: np.ndarray

    @property
    def n_rows(self) -> int:
        return len(self.timestamps_ms)


async def load_klines(
    start_ms: int | None = None,
    end_ms: int | None = None,
) -> KlineData:
    """Load 1s kline data from PostgreSQL into numpy arrays."""
    pool = await asyncpg.create_pool(PG_DSN, min_size=1, max_size=3)

    where_clauses = ["interval = '1s'"]
    params = []
    if start_ms is not None:
        params.append(start_ms)
        where_clauses.append(f"open_time_ms >= ${len(params)}")
    if end_ms is not None:
        params.append(end_ms)
        where_clauses.append(f"open_time_ms < ${len(params)}")

    where = " AND ".join(where_clauses)
    query = f"""
        SELECT open_time_ms, open, high, low, close, volume, trades_count
        FROM btc_klines
        WHERE {where}
        ORDER BY open_time_ms ASC
    """

    logger.info("Loading klines from PostgreSQL...")
    t0 = time.monotonic()
    rows = await pool.fetch(query, *params)
    await pool.close()

    n = len(rows)
    if n == 0:
        raise ValueError("No kline data found. Run tools/collect_data.py first.")

    logger.info("Loaded %s rows in %.1fs", f"{n:,}", time.monotonic() - t0)

    data = KlineData(
        timestamps_ms=np.array([r["open_time_ms"] for r in rows], dtype=np.int64),
        opens=np.array([r["open"] for r in rows], dtype=np.float64),
        highs=np.array([r["high"] for r in rows], dtype=np.float64),
        lows=np.array([r["low"] for r in rows], dtype=np.float64),
        closes=np.array([r["close"] for r in rows], dtype=np.float64),
        volumes=np.array([r["volume"] for r in rows], dtype=np.float64),
        trades_counts=np.array([r["trades_count"] for r in rows], dtype=np.float64),
    )
    return data


# ---------------------------------------------------------------------------
# Label generation
# ---------------------------------------------------------------------------


def generate_labels(
    closes: np.ndarray,
    horizon_steps: int,
    dead_zone: float = 0.0,
    mode: str = "regression",
) -> tuple[np.ndarray, np.ndarray]:
    """Generate labels and returns via array shifting.

    Args:
        closes: Close prices, shape (N,).
        horizon_steps: Number of steps (seconds) into the future.
        dead_zone: Minimum absolute return to count as a valid sample.
            For classification: samples with |return| <= dead_zone get label -1.
            For regression: samples with |return| <= dead_zone get label NaN.
            Set to 0.0 (default) for no dead-zone filtering.
        mode: "regression" (continuous returns) or "classification" (binary 0/1).

    Returns:
        labels: For regression: continuous fractional returns (NaN = invalid).
                For classification: 1 if up, 0 if down, -1 if invalid/dead zone.
                Shape (N,).
        returns: Fractional return. Shape (N,), last entries are NaN.
    """
    n = len(closes)
    returns = np.full(n, np.nan, dtype=np.float64)

    if mode == "regression":
        labels = np.full(n, np.nan, dtype=np.float64)
    else:
        labels = np.full(n, -1, dtype=np.int8)

    if horizon_steps >= n:
        return labels, returns

    future = closes[horizon_steps:]
    current = closes[: n - horizon_steps]
    safe_current = np.where(current == 0, np.nan, current)

    ret = (future - current) / safe_current
    returns[: n - horizon_steps] = ret

    if mode == "regression":
        # Continuous return labels
        if dead_zone > 0:
            significant = np.abs(ret) > dead_zone
            labels[: n - horizon_steps] = np.where(significant, ret, np.nan)
        else:
            labels[: n - horizon_steps] = ret
    else:
        # Binary direction labels (classification)
        direction = (future > current).astype(np.int8)
        if dead_zone > 0:
            significant = np.abs(ret) > dead_zone
            labels[: n - horizon_steps] = np.where(significant, direction, np.int8(-1))
        else:
            labels[: n - horizon_steps] = direction

    return labels, returns


# ---------------------------------------------------------------------------
# Walk-forward cross-validation
# ---------------------------------------------------------------------------


@dataclass
class FoldResult:
    """Metrics from a single walk-forward fold."""

    fold: int
    train_start_ms: int
    train_end_ms: int
    test_start_ms: int
    test_end_ms: int
    n_train: int
    n_test: int
    rmse: float
    mae: float
    r2: float
    direction_accuracy: float  # fraction of correct sign predictions
    ic: float  # information coefficient (Pearson correlation)
    label_mean: float  # mean of test labels
    pred_mean: float  # mean of predictions


def walk_forward_splits(
    timestamps_ms: np.ndarray,
    train_weeks: int = 8,
    test_weeks: int = 2,
    step_weeks: int = 2,
) -> list[tuple[np.ndarray, np.ndarray]]:
    """Generate walk-forward train/test index splits.

    Args:
        timestamps_ms: Sorted timestamps in milliseconds.
        train_weeks: Length of training window in weeks.
        test_weeks: Length of test window in weeks.
        step_weeks: Slide step in weeks.

    Returns:
        List of (train_indices, test_indices) tuples.
    """
    ms_per_week = 7 * 24 * 3600 * 1000
    train_len = train_weeks * ms_per_week
    test_len = test_weeks * ms_per_week
    step_len = step_weeks * ms_per_week

    t_start = timestamps_ms[0]
    t_end = timestamps_ms[-1]

    splits = []
    cursor = t_start

    while cursor + train_len + test_len <= t_end:
        train_end = cursor + train_len
        test_end = train_end + test_len

        train_mask = (timestamps_ms >= cursor) & (timestamps_ms < train_end)
        test_mask = (timestamps_ms >= train_end) & (timestamps_ms < test_end)

        train_idx = np.where(train_mask)[0]
        test_idx = np.where(test_mask)[0]

        if len(train_idx) > 0 and len(test_idx) > 0:
            splits.append((train_idx, test_idx))

        cursor += step_len

    return splits


# ---------------------------------------------------------------------------
# Feature computation (chunked for memory)
# ---------------------------------------------------------------------------


def compute_features_chunked(
    data: KlineData,
    chunk_size: int = 500_000,
) -> np.ndarray:
    """Compute features for all rows, processing in chunks to manage memory.

    Returns feature matrix of shape (N, NUM_FEATURES).
    """
    n = data.n_rows
    logger.info(
        "Computing features for %s rows (v2, %d features)...", f"{n:,}", NUM_FEATURES
    )
    t0 = time.monotonic()

    # For features that need lookback context, we include overlap
    # _MIN_TICKS - 1 ensures multi-TF features have full context
    overlap = _MIN_TICKS - 1  # 3660
    all_features = np.full((n, NUM_FEATURES), np.nan, dtype=np.float64)

    # Process in chunks with overlap for context
    start = 0
    chunk_num = 0
    total_chunks = (n + chunk_size - 1) // chunk_size
    while start < n:
        # Include overlap from before the chunk for lookback context
        ctx_start = max(0, start - overlap)
        end = min(start + chunk_size, n)
        chunk_num += 1

        logger.info(
            "  Chunk %d/%d: rows %s–%s (+ %s context)...",
            chunk_num,
            total_chunks,
            f"{start:,}",
            f"{end:,}",
            f"{start - ctx_start:,}",
        )

        chunk_features = compute_batch(
            timestamps_ms=data.timestamps_ms[ctx_start:end],
            opens=data.opens[ctx_start:end],
            highs=data.highs[ctx_start:end],
            lows=data.lows[ctx_start:end],
            closes=data.closes[ctx_start:end],
            volumes=data.volumes[ctx_start:end],
            trades_counts=data.trades_counts[ctx_start:end],
        )

        # Copy only the non-overlap portion into the result
        offset = start - ctx_start  # how many overlap rows to skip
        all_features[start:end] = chunk_features[offset:]
        del chunk_features  # free chunk memory immediately

        start = end

    elapsed = time.monotonic() - t0
    # Check core features (0..44) for validity; 45-48 are poly/time pass-through
    valid_rows = np.sum(~np.any(np.isnan(all_features[:, :_N_CORE_FEATURES]), axis=1))
    logger.info(
        "Features computed in %.1fs. Valid rows: %s / %s (%.1f%%)",
        elapsed,
        f"{valid_rows:,}",
        f"{n:,}",
        valid_rows / n * 100,
    )

    return all_features


# ---------------------------------------------------------------------------
# Default LightGBM hyperparameters
# ---------------------------------------------------------------------------

DEFAULT_LGB_PARAMS: dict = dict(
    n_estimators=1000,
    max_depth=6,
    num_leaves=31,
    learning_rate=0.05,
    min_child_samples=50,
    subsample=0.8,
    colsample_bytree=0.8,
    reg_alpha=0.01,
    reg_lambda=0.1,
    random_state=42,
    verbose=-1,
    n_jobs=-1,
)


# ---------------------------------------------------------------------------
# Training pipeline
# ---------------------------------------------------------------------------


def _get_valid_mask(
    features: np.ndarray,
    labels: np.ndarray,
    indices: np.ndarray,
) -> np.ndarray:
    """Return boolean mask for rows with valid core features and valid label.

    Handles both regression labels (NaN = invalid) and classification labels
    (-1 = invalid).
    """
    valid_features = ~np.any(np.isnan(features[indices, :_N_CORE_FEATURES]), axis=1)
    if labels.dtype.kind == "f":
        # Regression: float labels, NaN = invalid
        valid_labels = ~np.isnan(labels[indices])
    else:
        # Classification: int labels, -1 = invalid
        valid_labels = labels[indices] >= 0
    return valid_features & valid_labels


def train_and_evaluate(
    features: np.ndarray,
    labels: np.ndarray,
    timestamps_ms: np.ndarray,
    train_weeks: int,
    test_weeks: int,
    horizon_s: int,
    lgb_overrides: dict | None = None,
) -> tuple[lgb.LGBMRegressor, list[FoldResult]]:
    """Run walk-forward CV and train a final regression model.

    Args:
        features: Feature matrix, shape (N, NUM_FEATURES).
        labels: Label array, shape (N,) — continuous returns (NaN = invalid).
        timestamps_ms: Timestamps, shape (N,).
        train_weeks: Training window in weeks.
        test_weeks: Test window in weeks.
        horizon_s: Prediction horizon in seconds.
        lgb_overrides: Optional dict of LightGBM params to override defaults.

    Returns:
        (final_model, fold_results)
    """
    # LightGBM hyperparameters
    lgb_params = {**DEFAULT_LGB_PARAMS}
    if lgb_overrides:
        lgb_params.update(lgb_overrides)

    # Generate walk-forward splits
    splits = walk_forward_splits(timestamps_ms, train_weeks, test_weeks)
    logger.info(
        "Walk-forward: %d folds (%d-week train, %d-week test)",
        len(splits),
        train_weeks,
        test_weeks,
    )

    if len(splits) == 0:
        raise ValueError(
            "Not enough data for walk-forward splits. "
            "Need at least %d weeks of data.",
            train_weeks + test_weeks,
        )

    fold_results: list[FoldResult] = []

    for i, (train_idx, test_idx) in enumerate(splits):
        # Filter valid rows (no NaN in core features, valid label)
        train_valid = _get_valid_mask(features, labels, train_idx)
        test_valid = _get_valid_mask(features, labels, test_idx)

        X_train = features[train_idx[train_valid]]
        y_train = labels[train_idx[train_valid]].astype(np.float64)
        X_test = features[test_idx[test_valid]]
        y_test = labels[test_idx[test_valid]].astype(np.float64)

        if len(X_train) < 100 or len(X_test) < 100:
            logger.warning("Fold %d: insufficient data, skipping", i + 1)
            continue

        # Train with early stopping
        model = lgb.LGBMRegressor(**lgb_params)

        # Use last 10% of train as validation for early stopping
        val_split = int(len(X_train) * 0.9)
        model.fit(
            X_train[:val_split],
            y_train[:val_split],
            eval_set=[(X_train[val_split:], y_train[val_split:])],
            callbacks=[lgb.early_stopping(100, verbose=False)],
        )

        # Predict on test
        y_pred = model.predict(X_test)

        # Regression metrics
        rmse = float(np.sqrt(mean_squared_error(y_test, y_pred)))
        mae = float(mean_absolute_error(y_test, y_pred))
        r2 = float(r2_score(y_test, y_pred))

        # Direction accuracy: does the sign match?
        sign_match = np.sign(y_pred) == np.sign(y_test)
        direction_acc = float(sign_match.mean())

        # Information coefficient (Pearson correlation)
        if len(y_test) > 2:
            ic_val, _ = pearsonr(y_pred, y_test)
            ic_val = float(ic_val)
        else:
            ic_val = 0.0

        result = FoldResult(
            fold=i + 1,
            train_start_ms=int(timestamps_ms[train_idx[0]]),
            train_end_ms=int(timestamps_ms[train_idx[-1]]),
            test_start_ms=int(timestamps_ms[test_idx[0]]),
            test_end_ms=int(timestamps_ms[test_idx[-1]]),
            n_train=len(X_train),
            n_test=len(X_test),
            rmse=rmse,
            mae=mae,
            r2=r2,
            direction_accuracy=direction_acc,
            ic=ic_val,
            label_mean=float(y_test.mean()),
            pred_mean=float(y_pred.mean()),
        )
        fold_results.append(result)

        logger.info(
            "Fold %2d: RMSE=%.6f  MAE=%.6f  R²=%.4f  DirAcc=%.4f  IC=%.4f  "
            "n_train=%s  n_test=%s",
            result.fold,
            result.rmse,
            result.mae,
            result.r2,
            result.direction_accuracy,
            result.ic,
            f"{result.n_train:,}",
            f"{result.n_test:,}",
        )

    if not fold_results:
        raise ValueError("All folds failed. Check data quality.")

    # Aggregate metrics
    avg_rmse = np.mean([r.rmse for r in fold_results])
    avg_mae = np.mean([r.mae for r in fold_results])
    avg_r2 = np.mean([r.r2 for r in fold_results])
    avg_dir_acc = np.mean([r.direction_accuracy for r in fold_results])
    avg_ic = np.mean([r.ic for r in fold_results])

    logger.info("=" * 60)
    logger.info("WALK-FORWARD SUMMARY (%d folds):", len(fold_results))
    logger.info("  Avg RMSE:               %.6f", avg_rmse)
    logger.info("  Avg MAE:                %.6f", avg_mae)
    logger.info("  Avg R²:                 %.4f  (> 0 = some signal)", avg_r2)
    logger.info("  Avg Direction Accuracy:  %.4f  (coin-flip = 0.5000)", avg_dir_acc)
    logger.info("  Avg IC (Pearson):        %.4f  (> 0 = some signal)", avg_ic)
    logger.info("=" * 60)

    # ----- Train final model on all data -----
    logger.info("Training final model on all valid data...")
    all_idx = np.arange(len(labels))
    valid_mask = _get_valid_mask(features, labels, all_idx)
    X_all = features[all_idx[valid_mask]]
    y_all = labels[all_idx[valid_mask]].astype(np.float64)

    # Reserve last 10% for early stopping
    val_split = int(len(X_all) * 0.9)
    final_model = lgb.LGBMRegressor(**lgb_params)
    final_model.fit(
        X_all[:val_split],
        y_all[:val_split],
        eval_set=[(X_all[val_split:], y_all[val_split:])],
        callbacks=[lgb.early_stopping(100, verbose=False)],
    )
    logger.info("Final model trained on %s rows.", f"{len(X_all):,}")

    # Feature importance
    importance = final_model.feature_importances_
    sorted_idx = np.argsort(importance)[::-1]
    logger.info("Top 15 features:")
    for rank, idx in enumerate(sorted_idx[:15]):
        logger.info(
            "  %2d. %-30s  importance=%d",
            rank + 1,
            FEATURE_NAMES[idx] if idx < len(FEATURE_NAMES) else f"feature_{idx}",
            importance[idx],
        )

    return final_model, fold_results


# ---------------------------------------------------------------------------
# Optuna hyperparameter optimization
# ---------------------------------------------------------------------------


def optuna_tune(
    features: np.ndarray,
    labels: np.ndarray,
    timestamps_ms: np.ndarray,
    train_weeks: int,
    test_weeks: int,
    n_trials: int = 50,
) -> dict:
    """Run Optuna TPE search to find optimal LightGBM hyperparameters.

    Minimizes average RMSE across walk-forward CV folds.

    Args:
        features: Feature matrix, shape (N, NUM_FEATURES).
        labels: Label array, shape (N,) — continuous returns (NaN = invalid).
        timestamps_ms: Timestamps, shape (N,).
        train_weeks: Training window in weeks.
        test_weeks: Test window in weeks.
        n_trials: Number of Optuna trials. Default: 50.

    Returns:
        Best LightGBM parameter dict.
    """
    try:
        import optuna
    except ImportError:
        print(
            "ERROR: optuna is required for --optuna. Install: pip install 'optuna>=3.5,<4'"
        )
        sys.exit(1)

    # Suppress Optuna's internal logging clutter
    optuna.logging.set_verbosity(optuna.logging.WARNING)

    splits = walk_forward_splits(timestamps_ms, train_weeks, test_weeks)
    logger.info("Optuna HPO: %d trials, %d CV folds", n_trials, len(splits))

    if len(splits) == 0:
        raise ValueError("Not enough data for walk-forward splits.")

    # Pre-compute valid masks for each fold to avoid redundant work
    fold_data: list[tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]] = []
    for train_idx, test_idx in splits:
        train_valid = _get_valid_mask(features, labels, train_idx)
        test_valid = _get_valid_mask(features, labels, test_idx)

        X_tr = features[train_idx[train_valid]]
        y_tr = labels[train_idx[train_valid]].astype(np.float64)
        X_te = features[test_idx[test_valid]]
        y_te = labels[test_idx[test_valid]].astype(np.float64)

        if len(X_tr) >= 100 and len(X_te) >= 100:
            fold_data.append((X_tr, y_tr, X_te, y_te))

    if not fold_data:
        raise ValueError("No valid folds for Optuna.")

    logger.info("Optuna: %d usable folds, starting search...", len(fold_data))

    def objective(trial: optuna.Trial) -> float:
        params = dict(
            n_estimators=1000,
            max_depth=trial.suggest_int("max_depth", 3, 8),
            num_leaves=trial.suggest_int("num_leaves", 15, 63),
            learning_rate=trial.suggest_float("learning_rate", 0.01, 0.1, log=True),
            min_child_samples=trial.suggest_int("min_child_samples", 20, 300),
            subsample=trial.suggest_float("subsample", 0.5, 1.0),
            colsample_bytree=trial.suggest_float("colsample_bytree", 0.5, 1.0),
            reg_alpha=trial.suggest_float("reg_alpha", 0.001, 1.0, log=True),
            reg_lambda=trial.suggest_float("reg_lambda", 0.001, 1.0, log=True),
            random_state=42,
            verbose=-1,
            n_jobs=-1,
        )

        rmse_scores = []
        for fold_i, (X_tr, y_tr, X_te, y_te) in enumerate(fold_data):
            model = lgb.LGBMRegressor(**params)

            val_split = int(len(X_tr) * 0.9)
            model.fit(
                X_tr[:val_split],
                y_tr[:val_split],
                eval_set=[(X_tr[val_split:], y_tr[val_split:])],
                callbacks=[lgb.early_stopping(30, verbose=False)],
            )

            y_pred = model.predict(X_te)
            rmse = float(np.sqrt(mean_squared_error(y_te, y_pred)))
            rmse_scores.append(rmse)

            # Report intermediate value for pruning
            trial.report(np.mean(rmse_scores), fold_i)
            if trial.should_prune():
                raise optuna.TrialPruned()

        return float(np.mean(rmse_scores))

    study = optuna.create_study(
        direction="minimize",
        sampler=optuna.samplers.TPESampler(seed=42),
        pruner=optuna.pruners.MedianPruner(n_startup_trials=5, n_warmup_steps=2),
    )
    study.optimize(objective, n_trials=n_trials, show_progress_bar=True)

    best = study.best_trial
    logger.info("=" * 60)
    logger.info("OPTUNA BEST (trial %d):", best.number)
    logger.info("  Avg RMSE: %.6f", best.value)
    for k, v in best.params.items():
        logger.info("  %-20s = %s", k, v)
    logger.info("=" * 60)

    # Merge best params with defaults
    best_params = {**DEFAULT_LGB_PARAMS, **best.params}
    return best_params


# ---------------------------------------------------------------------------
# Save model artifact
# ---------------------------------------------------------------------------


def save_model(
    model: lgb.LGBMRegressor,
    fold_results: list[FoldResult],
    horizon_s: int,
    output_path: str,
    dead_zone: float = 0.0,
    lgb_params: dict | None = None,
) -> None:
    """Save model artifact to disk."""
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    artifact = {
        "model": model,
        "model_type": "regression",
        "feature_names": FEATURE_NAMES,
        "num_features": NUM_FEATURES,
        "horizon_s": horizon_s,
        "dead_zone": dead_zone,
        "version": f"v4_reg_h{horizon_s}",
        "metrics": {
            "avg_rmse": float(np.mean([r.rmse for r in fold_results])),
            "avg_mae": float(np.mean([r.mae for r in fold_results])),
            "avg_r2": float(np.mean([r.r2 for r in fold_results])),
            "avg_direction_accuracy": float(
                np.mean([r.direction_accuracy for r in fold_results])
            ),
            "avg_ic": float(np.mean([r.ic for r in fold_results])),
            "n_folds": len(fold_results),
        },
        "fold_results": [
            {
                "fold": r.fold,
                "rmse": r.rmse,
                "mae": r.mae,
                "r2": r.r2,
                "direction_accuracy": r.direction_accuracy,
                "ic": r.ic,
                "n_test": r.n_test,
            }
            for r in fold_results
        ],
    }
    if lgb_params:
        artifact["lgb_params"] = {
            k: v for k, v in lgb_params.items() if k not in ("verbose", "n_jobs")
        }

    joblib.dump(artifact, path)
    size_mb = path.stat().st_size / 1_000_000
    logger.info("Model saved to %s (%.1f MB)", path, size_mb)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def run(args: argparse.Namespace) -> None:
    """Load data, compute features, train, and save."""

    # Load klines from PostgreSQL
    start_ms = None
    if args.max_days > 0:
        import datetime

        cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
            days=args.max_days
        )
        start_ms = int(cutoff.timestamp() * 1000)
        logger.info(
            "Limiting data to last %d days (since %s)", args.max_days, cutoff.date()
        )
    data = await load_klines(start_ms=start_ms)

    logger.info(
        "Data range: %s → %s (%s rows, %.1f days)",
        data.timestamps_ms[0],
        data.timestamps_ms[-1],
        f"{data.n_rows:,}",
        (data.timestamps_ms[-1] - data.timestamps_ms[0]) / 86_400_000,
    )

    # Compute features
    features = compute_features_chunked(data)

    # Generate labels (regression mode: continuous returns)
    horizon_steps = args.horizon  # seconds = steps for 1s data
    dead_zone = args.dead_zone
    labels, returns = generate_labels(
        data.closes, horizon_steps, dead_zone=dead_zone, mode="regression"
    )

    valid_labels = np.sum(~np.isnan(labels))
    total_possible = max(len(labels) - horizon_steps, 1)
    excluded = total_possible - valid_labels
    valid_returns = labels[~np.isnan(labels)]
    logger.info(
        "Labels (regression): %s valid | mean=%.6f std=%.6f | "
        "positive=%.1f%% negative=%.1f%%",
        f"{valid_labels:,}",
        float(valid_returns.mean()) if len(valid_returns) > 0 else 0.0,
        float(valid_returns.std()) if len(valid_returns) > 0 else 0.0,
        (np.sum(valid_returns > 0) / max(len(valid_returns), 1)) * 100,
        (np.sum(valid_returns < 0) / max(len(valid_returns), 1)) * 100,
    )
    if dead_zone > 0:
        logger.info(
            "Dead-zone filter (|return| <= %.4f): excluded %s / %s (%.1f%%)",
            dead_zone,
            f"{excluded:,}",
            f"{total_possible:,}",
            excluded / total_possible * 100,
        )

    # Optuna HPO (optional)
    lgb_overrides: dict | None = None
    if args.optuna:
        best_params = optuna_tune(
            features=features,
            labels=labels,
            timestamps_ms=data.timestamps_ms,
            train_weeks=args.train_weeks,
            test_weeks=args.test_weeks,
            n_trials=args.optuna_trials,
        )
        lgb_overrides = best_params

    # Train with walk-forward CV
    model, fold_results = train_and_evaluate(
        features=features,
        labels=labels,
        timestamps_ms=data.timestamps_ms,
        train_weeks=args.train_weeks,
        test_weeks=args.test_weeks,
        horizon_s=args.horizon,
        lgb_overrides=lgb_overrides,
    )

    # Save
    save_model(
        model,
        fold_results,
        args.horizon,
        args.output,
        dead_zone=dead_zone,
        lgb_params=lgb_overrides,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Train LightGBM regression model for BTC return prediction (v4).",
    )
    parser.add_argument(
        "--horizon",
        type=int,
        default=300,
        help="Prediction horizon in seconds (300=5m, 900=15m). Default: 300",
    )
    parser.add_argument(
        "--train-weeks",
        type=int,
        default=8,
        help="Training window size in weeks. Default: 8",
    )
    parser.add_argument(
        "--test-weeks",
        type=int,
        default=2,
        help="Test window size in weeks. Default: 2",
    )
    parser.add_argument(
        "--dead-zone",
        type=float,
        default=0.003,
        help="Dead-zone threshold for label filtering. "
        "Samples with |return| <= dead_zone are excluded. "
        "Set to 0 to disable. Default: 0.003",
    )
    parser.add_argument(
        "--optuna",
        action="store_true",
        help="Run Optuna hyperparameter optimization before final training.",
    )
    parser.add_argument(
        "--optuna-trials",
        type=int,
        default=50,
        help="Number of Optuna trials. Default: 50",
    )
    parser.add_argument(
        "--max-days",
        type=int,
        default=0,
        help="Limit training to the most recent N days of data. 0 = use all. Default: 0",
    )
    parser.add_argument(
        "--output",
        default="models/btc_5m_v4_reg.pkl",
        help="Output model path. Default: models/btc_5m_v4_reg.pkl",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    asyncio.run(run(args))


if __name__ == "__main__":
    main()
