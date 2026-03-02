#!/usr/bin/env python3
"""
Train a LightGBM classifier for BTC direction prediction.

Uses walk-forward cross-validation to ensure no future data leakage.
Produces a model artifact (.pkl) that can be loaded by MLPredictor.

Usage:
    python tools/train_model.py --horizon 300 --output models/btc_5m_v3.pkl
    python tools/train_model.py --horizon 300 --dead-zone 0.001 --output models/btc_5m_v3.pkl
    python tools/train_model.py --horizon 300 --dead-zone 0.001 --optuna --optuna-trials 50

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
from sklearn.calibration import IsotonicRegression
from sklearn.metrics import (
    accuracy_score,
    brier_score_loss,
    log_loss,
    roc_auc_score,
)

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
) -> tuple[np.ndarray, np.ndarray]:
    """Generate binary labels and returns via array shifting.

    Args:
        closes: Close prices, shape (N,).
        horizon_steps: Number of steps (seconds) into the future.
        dead_zone: Minimum absolute return to count as a directional move.
            Samples with |return| <= dead_zone get label -1 (excluded).
            Set to 0.0 (default) for no dead-zone filtering.

    Returns:
        labels: 1 if price goes up, 0 if down, -1 if invalid or in dead zone.
                Shape (N,).
        returns: Fractional return. Shape (N,), last entries are NaN.
    """
    n = len(closes)
    labels = np.full(n, -1, dtype=np.int8)
    returns = np.full(n, np.nan, dtype=np.float64)

    if horizon_steps >= n:
        return labels, returns

    future = closes[horizon_steps:]
    current = closes[: n - horizon_steps]
    safe_current = np.where(current == 0, np.nan, current)

    ret = (future - current) / safe_current
    returns[: n - horizon_steps] = ret

    # Binary direction labels
    direction = (future > current).astype(np.int8)

    if dead_zone > 0:
        # Only label samples where |return| > dead_zone
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
    brier_score: float
    log_loss_val: float
    accuracy: float
    auc_roc: float
    label_mean: float  # fraction of positive labels in test
    pred_mean: float  # mean predicted probability


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
    min_child_samples=200,
    subsample=0.8,
    colsample_bytree=0.8,
    reg_alpha=0.1,
    reg_lambda=1.0,
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
    """Return boolean mask for rows with valid core features and valid label."""
    return ~np.any(np.isnan(features[indices, :_N_CORE_FEATURES]), axis=1) & (
        labels[indices] >= 0
    )


def train_and_evaluate(
    features: np.ndarray,
    labels: np.ndarray,
    timestamps_ms: np.ndarray,
    train_weeks: int,
    test_weeks: int,
    horizon_s: int,
    lgb_overrides: dict | None = None,
) -> tuple[lgb.LGBMClassifier, list[FoldResult], IsotonicRegression | None]:
    """Run walk-forward CV and train a final model.

    Args:
        features: Feature matrix, shape (N, NUM_FEATURES).
        labels: Label array, shape (N,).
        timestamps_ms: Timestamps, shape (N,).
        train_weeks: Training window in weeks.
        test_weeks: Test window in weeks.
        horizon_s: Prediction horizon in seconds.
        lgb_overrides: Optional dict of LightGBM params to override defaults.

    Returns:
        (final_model, fold_results, calibrator_or_none)
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
    all_test_labels: list[np.ndarray] = []
    all_test_preds: list[np.ndarray] = []

    for i, (train_idx, test_idx) in enumerate(splits):
        # Filter valid rows (no NaN in core features, valid label)
        train_valid = _get_valid_mask(features, labels, train_idx)
        test_valid = _get_valid_mask(features, labels, test_idx)

        X_train = features[train_idx[train_valid]]
        y_train = labels[train_idx[train_valid]]
        X_test = features[test_idx[test_valid]]
        y_test = labels[test_idx[test_valid]]

        if len(X_train) < 100 or len(X_test) < 100:
            logger.warning("Fold %d: insufficient data, skipping", i + 1)
            continue

        # Train with early stopping
        model = lgb.LGBMClassifier(**lgb_params)

        # Use last 10% of train as validation for early stopping
        val_split = int(len(X_train) * 0.9)
        model.fit(
            X_train[:val_split],
            y_train[:val_split],
            eval_set=[(X_train[val_split:], y_train[val_split:])],
            callbacks=[lgb.early_stopping(50, verbose=False)],
        )

        # Predict on test
        y_pred_proba = model.predict_proba(X_test)[:, 1]
        y_pred_class = (y_pred_proba > 0.5).astype(int)

        # Metrics
        result = FoldResult(
            fold=i + 1,
            train_start_ms=int(timestamps_ms[train_idx[0]]),
            train_end_ms=int(timestamps_ms[train_idx[-1]]),
            test_start_ms=int(timestamps_ms[test_idx[0]]),
            test_end_ms=int(timestamps_ms[test_idx[-1]]),
            n_train=len(X_train),
            n_test=len(X_test),
            brier_score=float(brier_score_loss(y_test, y_pred_proba)),
            log_loss_val=float(log_loss(y_test, y_pred_proba)),
            accuracy=float(accuracy_score(y_test, y_pred_class)),
            auc_roc=float(roc_auc_score(y_test, y_pred_proba)),
            label_mean=float(y_test.mean()),
            pred_mean=float(y_pred_proba.mean()),
        )
        fold_results.append(result)
        all_test_labels.append(y_test)
        all_test_preds.append(y_pred_proba)

        logger.info(
            "Fold %2d: Brier=%.4f  LogLoss=%.4f  Acc=%.4f  AUC=%.4f  "
            "n_train=%s  n_test=%s  label_mean=%.3f",
            result.fold,
            result.brier_score,
            result.log_loss_val,
            result.accuracy,
            result.auc_roc,
            f"{result.n_train:,}",
            f"{result.n_test:,}",
            result.label_mean,
        )

    if not fold_results:
        raise ValueError("All folds failed. Check data quality.")

    # Aggregate metrics
    avg_brier = np.mean([r.brier_score for r in fold_results])
    avg_logloss = np.mean([r.log_loss_val for r in fold_results])
    avg_acc = np.mean([r.accuracy for r in fold_results])
    avg_auc = np.mean([r.auc_roc for r in fold_results])

    logger.info("=" * 60)
    logger.info("WALK-FORWARD SUMMARY (%d folds):", len(fold_results))
    logger.info("  Avg Brier Score:  %.4f  (coin-flip = 0.2500)", avg_brier)
    logger.info("  Avg Log-Loss:     %.4f  (coin-flip = 0.6931)", avg_logloss)
    logger.info("  Avg Accuracy:     %.4f  (coin-flip = 0.5000)", avg_acc)
    logger.info("  Avg AUC-ROC:      %.4f  (random    = 0.5000)", avg_auc)
    logger.info("=" * 60)

    # ----- Train final model on all data -----
    logger.info("Training final model on all valid data...")
    all_idx = np.arange(len(labels))
    valid_mask = _get_valid_mask(features, labels, all_idx)
    X_all = features[all_idx[valid_mask]]
    y_all = labels[all_idx[valid_mask]]

    # Reserve last 10% for early stopping
    val_split = int(len(X_all) * 0.9)
    final_model = lgb.LGBMClassifier(**lgb_params)
    final_model.fit(
        X_all[:val_split],
        y_all[:val_split],
        eval_set=[(X_all[val_split:], y_all[val_split:])],
        callbacks=[lgb.early_stopping(50, verbose=False)],
    )
    logger.info("Final model trained on %s rows.", f"{len(X_all):,}")

    # ----- Calibration check -----
    # Combine all out-of-sample predictions for calibration analysis
    calibrator = None
    if all_test_labels:
        all_y = np.concatenate(all_test_labels)
        all_p = np.concatenate(all_test_preds)

        # Check calibration by decile
        logger.info("Calibration check (10 bins):")
        n_bins = 10
        bin_edges = np.linspace(0, 1, n_bins + 1)
        max_cal_error = 0.0
        for j in range(n_bins):
            mask = (all_p >= bin_edges[j]) & (all_p < bin_edges[j + 1])
            if mask.sum() > 0:
                actual = all_y[mask].mean()
                predicted = all_p[mask].mean()
                cal_error = abs(actual - predicted)
                max_cal_error = max(max_cal_error, cal_error)
                logger.info(
                    "  Bin [%.1f, %.1f): n=%6d  pred=%.3f  actual=%.3f  err=%.3f",
                    bin_edges[j],
                    bin_edges[j + 1],
                    mask.sum(),
                    predicted,
                    actual,
                    cal_error,
                )

        # If calibration error > 3%, fit isotonic regression
        if max_cal_error > 0.03:
            logger.info(
                "Max calibration error %.3f > 0.03 — fitting isotonic calibrator.",
                max_cal_error,
            )
            calibrator = IsotonicRegression(
                y_min=0.01, y_max=0.99, out_of_bounds="clip"
            )
            calibrator.fit(all_p, all_y)

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

    return final_model, fold_results, calibrator


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

    Minimizes average Brier score across walk-forward CV folds.

    Args:
        features: Feature matrix, shape (N, NUM_FEATURES).
        labels: Label array, shape (N,).
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
        y_tr = labels[train_idx[train_valid]]
        X_te = features[test_idx[test_valid]]
        y_te = labels[test_idx[test_valid]]

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
            min_child_samples=trial.suggest_int("min_child_samples", 50, 500),
            subsample=trial.suggest_float("subsample", 0.5, 1.0),
            colsample_bytree=trial.suggest_float("colsample_bytree", 0.5, 1.0),
            reg_alpha=trial.suggest_float("reg_alpha", 0.001, 5.0, log=True),
            reg_lambda=trial.suggest_float("reg_lambda", 0.001, 5.0, log=True),
            random_state=42,
            verbose=-1,
            n_jobs=-1,
        )

        brier_scores = []
        for fold_i, (X_tr, y_tr, X_te, y_te) in enumerate(fold_data):
            model = lgb.LGBMClassifier(**params)

            val_split = int(len(X_tr) * 0.9)
            model.fit(
                X_tr[:val_split],
                y_tr[:val_split],
                eval_set=[(X_tr[val_split:], y_tr[val_split:])],
                callbacks=[lgb.early_stopping(30, verbose=False)],
            )

            y_pred = model.predict_proba(X_te)[:, 1]
            brier = brier_score_loss(y_te, y_pred)
            brier_scores.append(brier)

            # Report intermediate value for pruning
            trial.report(np.mean(brier_scores), fold_i)
            if trial.should_prune():
                raise optuna.TrialPruned()

        return float(np.mean(brier_scores))

    study = optuna.create_study(
        direction="minimize",
        sampler=optuna.samplers.TPESampler(seed=42),
        pruner=optuna.pruners.MedianPruner(n_startup_trials=5, n_warmup_steps=2),
    )
    study.optimize(objective, n_trials=n_trials, show_progress_bar=True)

    best = study.best_trial
    logger.info("=" * 60)
    logger.info("OPTUNA BEST (trial %d):", best.number)
    logger.info("  Avg Brier Score: %.4f", best.value)
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
    model: lgb.LGBMClassifier,
    calibrator: IsotonicRegression | None,
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
        "calibrator": calibrator,
        "feature_names": FEATURE_NAMES,
        "num_features": NUM_FEATURES,
        "horizon_s": horizon_s,
        "dead_zone": dead_zone,
        "version": f"v3_h{horizon_s}",
        "metrics": {
            "avg_brier": float(np.mean([r.brier_score for r in fold_results])),
            "avg_logloss": float(np.mean([r.log_loss_val for r in fold_results])),
            "avg_accuracy": float(np.mean([r.accuracy for r in fold_results])),
            "avg_auc": float(np.mean([r.auc_roc for r in fold_results])),
            "n_folds": len(fold_results),
        },
        "fold_results": [
            {
                "fold": r.fold,
                "brier": r.brier_score,
                "accuracy": r.accuracy,
                "auc": r.auc_roc,
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
        logger.info("Limiting data to last %d days (since %s)", args.max_days, cutoff.date())
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

    # Generate labels
    horizon_steps = args.horizon  # seconds = steps for 1s data
    dead_zone = args.dead_zone
    labels, returns = generate_labels(data.closes, horizon_steps, dead_zone=dead_zone)

    valid_labels = np.sum(labels >= 0)
    total_possible = max(len(labels) - horizon_steps, 1)
    excluded = total_possible - valid_labels
    logger.info(
        "Labels: %s valid (%s up = %.1f%%, %s down = %.1f%%)",
        f"{valid_labels:,}",
        f"{np.sum(labels == 1):,}",
        np.sum(labels == 1) / max(valid_labels, 1) * 100,
        f"{np.sum(labels == 0):,}",
        np.sum(labels == 0) / max(valid_labels, 1) * 100,
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
    model, fold_results, calibrator = train_and_evaluate(
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
        calibrator,
        fold_results,
        args.horizon,
        args.output,
        dead_zone=dead_zone,
        lgb_params=lgb_overrides,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Train LightGBM model for BTC direction prediction (v2).",
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
        default=0.001,
        help="Dead-zone threshold for label filtering. "
        "Samples with |return| <= dead_zone are excluded. "
        "Set to 0 to disable. Default: 0.001",
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
        default="models/btc_5m_v3.pkl",
        help="Output model path. Default: models/btc_5m_v3.pkl",
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
