"""
Async ML prediction source for BTC direction/return forecasting.

Loads a trained LightGBM model and emits ``Prediction`` objects into the
shared prediction queue — identical interface to ``PredictionAggregator``.

Supports both model types:
  - **regression** (v4+): model.predict() → continuous return → confidence=1.0
  - **classification** (v3): model.predict_proba() → P(up) → confidence derived

Architecture::

    price_queue ──▶ _ingest_loop ──▶ FeatureEngine (rolling buffer)
                                           │
                   _predict_loop ◀─────────┘
                       │
                       ▼
              model.predict()  (<1 ms)
                       │
                       ▼
              prediction_queue.put_nowait(Prediction)

The two loops run concurrently via ``asyncio.gather``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

import joblib
import numpy as np

from src.ml.features import FeatureEngine
from src.models import Prediction, PriceTick

logger = logging.getLogger(__name__)


class MLPredictor:
    """Async ML prediction source using a trained LightGBM model.

    Args:
        price_queue: Queue of ``PriceTick`` objects (from the price splitter).
        prediction_queue: Shared output queue consumed by ``StrategyEngine``.
        config: ML-specific configuration (from ``MLConfig``).
    """

    def __init__(
        self,
        price_queue: asyncio.Queue[PriceTick],
        prediction_queue: asyncio.Queue[Prediction],
        *,
        model_path: str = "models/btc_5m_v3.pkl",
        feature_window: int = 4000,
        prediction_interval: float = 0.25,
        min_confidence: float = 0.1,
        min_predicted_return: float = 0.0001,
        max_predicted_return: float = 0.01,
        horizon_s: int = 300,
    ) -> None:
        self._price_queue = price_queue
        self._pred_queue = prediction_queue

        self._prediction_interval = prediction_interval
        self._min_confidence = min_confidence
        self._min_predicted_return = min_predicted_return
        self._max_predicted_return = max_predicted_return
        self._horizon_s = horizon_s

        self._feature_engine = FeatureEngine(buffer_size=feature_window)
        self._model_artifact = self._load_model(model_path)
        self._model = self._model_artifact["model"]

        # Auto-detect model type from artifact (backward compat: default to classification)
        self._model_type: str = self._model_artifact.get(
            "model_type", "classification"
        )

        # Only load calibrator for classification models
        if self._model_type == "classification":
            self._calibrator = self._model_artifact.get("calibrator")
        else:
            self._calibrator = None

        # Stats
        self._predictions_emitted: int = 0
        self._predictions_skipped: int = 0
        self._ingest_count: int = 0

    # ------------------------------------------------------------------
    # Model loading
    # ------------------------------------------------------------------

    @staticmethod
    def _load_model(path: str) -> dict:
        """Load a trained model artifact from disk.

        Expected keys: ``model``, ``feature_names``, ``horizon_s``.
        Optional keys: ``calibrator``, ``version``, ``metrics``, ``model_type``.
        """
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(
                f"ML model not found at {p.resolve()}. "
                f"Train one with: python tools/train_model.py"
            )
        artifact = joblib.load(p)
        if "model" not in artifact:
            raise ValueError(f"Model artifact at {p} missing 'model' key.")

        # Validate feature count matches current feature engine
        from src.ml.features import NUM_FEATURES

        model_n_features = artifact.get("num_features")
        if model_n_features is not None and model_n_features != NUM_FEATURES:
            raise ValueError(
                f"Model expects {model_n_features} features but "
                f"feature engine produces {NUM_FEATURES}. "
                f"Re-train with: python tools/train_model.py"
            )

        model_type = artifact.get("model_type", "classification")
        logger.info(
            "Loaded ML model: version=%s, type=%s, horizon=%ds, features=%d",
            artifact.get("version", "unknown"),
            model_type,
            artifact.get("horizon_s", 0),
            len(artifact.get("feature_names", [])),
        )
        return artifact

    # ------------------------------------------------------------------
    # Async loops
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Run ingestion and prediction loops concurrently."""
        logger.info(
            "MLPredictor starting: type=%s interval=%.2fs horizon=%ds",
            self._model_type,
            self._prediction_interval,
            self._horizon_s,
        )
        await asyncio.gather(
            self._ingest_loop(),
            self._predict_loop(),
        )

    def update_book(self, mid: float, spread: float) -> None:
        """Forward book snapshot to the feature engine for orderbook features."""
        self._feature_engine.update_book(mid, spread)

    async def _ingest_loop(self) -> None:
        """Consume PriceTicks and feed the feature engine."""
        while True:
            tick: PriceTick = await self._price_queue.get()
            self._feature_engine.update(
                tick.timestamp_ns,
                tick.price,
                tick.volume,
            )
            self._ingest_count += 1

    async def _predict_loop(self) -> None:
        """Periodically compute features and emit predictions."""
        # Wait for enough ticks before first prediction
        from src.ml.features import _MIN_TICKS

        warmup_threshold = _MIN_TICKS
        while self._feature_engine.tick_count < warmup_threshold:
            await asyncio.sleep(1.0)
        logger.info(
            "MLPredictor warm-up complete (%d ticks). Emitting predictions.",
            self._feature_engine.tick_count,
        )

        while True:
            await asyncio.sleep(self._prediction_interval)

            prediction = self._predict()
            if prediction is None:
                self._predictions_skipped += 1
                continue

            # Emit to shared queue with back-pressure handling
            try:
                self._pred_queue.put_nowait(prediction)
            except asyncio.QueueFull:
                try:
                    self._pred_queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                self._pred_queue.put_nowait(prediction)

            self._predictions_emitted += 1

            if self._predictions_emitted % 100 == 0:
                logger.info(
                    "MLPredictor [%s]: emitted=%d skipped=%d ingested=%d "
                    "pred_return=%.6f conf=%.4f",
                    self._model_type,
                    self._predictions_emitted,
                    self._predictions_skipped,
                    self._ingest_count,
                    abs(prediction.predicted_return),
                    prediction.confidence,
                )

    # ------------------------------------------------------------------
    # Inference — dispatcher
    # ------------------------------------------------------------------

    def _predict(self) -> Prediction | None:
        """Run one inference cycle. Dispatches to model-type-specific path."""
        if self._model_type == "regression":
            return self._predict_regression()
        return self._predict_classification()

    # ------------------------------------------------------------------
    # Regression inference (v4+)
    # ------------------------------------------------------------------

    def _predict_regression(self) -> Prediction | None:
        """Regression path: model predicts continuous return directly."""
        features = self._feature_engine.compute(
            seconds_to_expiry=float(self._horizon_s),
            total_seconds=float(self._horizon_s),
        )
        if features is None:
            return None

        # LightGBM regression inference — typically <1ms
        t0 = time.time_ns()
        try:
            raw_return = float(self._model.predict(features.reshape(1, -1))[0])
        except Exception as e:
            logger.warning("ML inference error: %s", e)
            return None
        inference_ns = time.time_ns() - t0

        # Gate: skip if predicted return is below noise floor
        if abs(raw_return) < self._min_predicted_return:
            return None

        # Clip to reasonable range
        clipped_return = max(
            -self._max_predicted_return,
            min(raw_return, self._max_predicted_return),
        )

        # Convert to predicted price
        current_price = self._feature_engine.latest_price
        if current_price <= 0:
            return None

        predicted_price = current_price * (1.0 + clipped_return)

        # Regression: confidence = 1.0 always (no triple-counting)
        confidence = 1.0

        if inference_ns > 5_000_000:  # > 5ms — log warning
            logger.warning("ML inference slow: %.1fms", inference_ns / 1_000_000)

        return Prediction(
            source="ml_lgbm",
            predicted_price=predicted_price,
            current_price=current_price,
            horizon_s=self._horizon_s,
            confidence=confidence,
        )

    # ------------------------------------------------------------------
    # Classification inference (v3 backward compat)
    # ------------------------------------------------------------------

    def _predict_classification(self) -> Prediction | None:
        """Classification path: model predicts P(up) probability."""
        features = self._feature_engine.compute(
            seconds_to_expiry=float(self._horizon_s),
            total_seconds=float(self._horizon_s),
        )
        if features is None:
            return None

        # LightGBM classification inference — typically <1ms
        t0 = time.time_ns()
        try:
            raw_proba = self._model.predict_proba(features.reshape(1, -1))[0, 1]
        except Exception as e:
            logger.warning("ML inference error: %s", e)
            return None
        inference_ns = time.time_ns() - t0

        # Apply calibration if available
        if self._calibrator is not None:
            p_up = float(self._calibrator.predict(np.array([raw_proba]))[0])
        else:
            p_up = float(raw_proba)

        # Confidence: 0.5 → 0.0, 0.0 or 1.0 → 1.0
        confidence = abs(p_up - 0.5) * 2.0

        if confidence < self._min_confidence:
            return None

        # Convert probability to predicted price
        current_price = self._feature_engine.latest_price
        if current_price <= 0:
            return None

        predicted_return = (p_up - 0.5) * 2.0 * self._max_predicted_return
        predicted_price = current_price * (1.0 + predicted_return)

        if inference_ns > 5_000_000:  # > 5ms — log warning
            logger.warning("ML inference slow: %.1fms", inference_ns / 1_000_000)

        return Prediction(
            source="ml_lgbm",
            predicted_price=predicted_price,
            current_price=current_price,
            horizon_s=self._horizon_s,
            confidence=confidence,
        )

    # ------------------------------------------------------------------
    # Stats (for health monitor)
    # ------------------------------------------------------------------

    @property
    def stats(self) -> dict:
        """Return a snapshot of predictor statistics."""
        return {
            "model_type": self._model_type,
            "predictions_emitted": self._predictions_emitted,
            "predictions_skipped": self._predictions_skipped,
            "ticks_ingested": self._ingest_count,
            "buffer_size": self._feature_engine.tick_count,
        }
