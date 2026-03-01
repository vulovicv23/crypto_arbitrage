# ML Prediction Pipeline — Design Plan

## Executive Summary

Replace the bot's simple linear-regression price extrapolation with a machine learning
pipeline that produces calibrated probability estimates for BTC direction. The ML model
slots into the existing async architecture as a new prediction source alongside the
current `PredictionAggregator`, requiring zero changes to the strategy, risk, or order
management layers.

**Expected impact:** Better `predicted_return` and `confidence` values → more accurate
`P(BTC up)` estimation → fewer false signals, higher win rate, larger average edge.

---

## Problem Statement

The current `PredictionAggregator` uses **linear regression on recent price ticks**
extrapolated to the prediction horizon. This has fundamental limitations:

1. **Linear extrapolation is naive** — price dynamics are non-linear, mean-reverting
   at short horizons, and momentum-driven at others. A straight line through 300 ticks
   doesn't capture this.
2. **Confidence is R-squared** — this measures goodness-of-fit to a line, not actual
   predictive accuracy. A high R-squared can produce terrible predictions.
3. **Single-feature model** — only uses price. Ignores volume, volatility regimes,
   cross-exchange divergences, orderbook dynamics, and time-to-expiry effects.
4. **No learning from outcomes** — the bot never learns from whether its predictions
   were correct. Every trade uses the same static model.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────────┐
│                        PREDICTION LAYER                                 │
│                                                                         │
│  price_queue ──┬──▶ PredictionAggregator ──┐                           │
│                │    (existing, fallback)     │                           │
│                │                             ├──▶ prediction_queue ──▶ Strategy
│                └──▶ MLPredictor ────────────┘                           │
│                     (new, primary)                                       │
│                         │                                               │
│                    FeatureEngine                                         │
│                    (shared code for                                      │
│                     train & serve)                                       │
│                         │                                               │
│                    LightGBM model                                       │
│                    (batch-trained)                                       │
│                         +                                               │
│                    OnlineSGD model                                       │
│                    (updates from                                         │
│                     trade outcomes)                                      │
│                                                                         │
│  ┌──────────────────────────────────────────────────────────────┐       │
│  │              TRAINING PIPELINE (offline)                      │       │
│  │  Historical BTC data → Features → Labels → Train → Evaluate  │       │
│  └──────────────────────────────────────────────────────────────┘       │
└─────────────────────────────────────────────────────────────────────────┘
```

**Key principle:** The ML model emits standard `Prediction` objects into the same
`prediction_queue`. The strategy doesn't know or care whether predictions come from
linear regression or gradient-boosted trees. This means:

- Zero changes to `strategy.py`, `order_manager.py`, `risk_manager.py`
- Easy A/B testing via matrix profiles (ML-on vs ML-off)
- Graceful fallback if ML model fails to load

---

## Phased Implementation

### Phase 1: Batch LightGBM Classifier (MVP)

**Goal:** Train an offline LightGBM model on historical Binance data that predicts
P(BTC goes up in next T seconds). Deploy as a new prediction source.

**Why LightGBM:**
- Inference <1ms (well within 100ms latency budget)
- Handles tabular features natively (no normalization needed)
- No GPU required for training or inference
- Excellent out-of-the-box calibration
- Small model size (~1MB), fast startup

**Timeline:** ~1 week of development

### Phase 2: Online Learning Feedback Loop

**Goal:** Add a lightweight online model (SGD classifier) that updates from the bot's
own trade outcomes, capturing regime shifts in real-time.

**How:** Every time a position resolves (win/loss), the features at signal time + the
outcome become a training sample. The online model adapts continuously, and its
predictions are blended with the batch LightGBM model.

**Timeline:** ~3 days after Phase 1 is validated

### Phase 3: Deep Sequence Model (If Justified)

**Goal:** If Phase 1+2 show clear alpha but plateau, add an LSTM/Transformer that
processes raw tick sequences instead of handcrafted features.

**Prerequisites:**
- Phase 1+2 validated profitable
- Sufficient training data accumulated (3+ months of live data)
- GPU infrastructure available

**Timeline:** 1-2 weeks, only if data supports it

---

## Phase 1 Detailed Design

### 1. Feature Engineering

All features are computed by a shared `FeatureEngine` class used identically in both
offline training and live inference (eliminates train/serve skew).

**Input:** Rolling buffer of (timestamp_ns, price, volume) tuples from Binance.

**Feature groups:**

| Group | Features | Count |
|-------|----------|-------|
| **Returns** | Returns over 1s, 5s, 15s, 30s, 60s, 300s windows | 6 |
| **Volatility** | Realized vol over 15s, 30s, 60s, 300s | 4 |
| **Momentum** | Rate of change (1st derivative) at 5s, 15s, 60s | 3 |
| **Acceleration** | 2nd derivative at 15s, 60s | 2 |
| **Volume** | Volume over 15s, 60s, 300s; volume rate of change | 4 |
| **VWAP** | Price deviation from VWAP at 60s, 300s | 2 |
| **Bollinger** | Position within 60s, 300s Bollinger bands (z-score) | 2 |
| **EMA** | Fast-slow EMA crossover magnitude; MACD-like signal | 2 |
| **Cross-source** | Binance vs CCCAGG price divergence (when available) | 1 |
| **Orderbook** | Polymarket mid-price, spread, spread change rate | 3 |
| **Time** | Seconds to expiry, fraction of contract elapsed | 2 |
| **Total** | | **~31** |

```python
# src/ml/features.py

class FeatureEngine:
    """Computes ML features from a rolling price buffer.

    CRITICAL: This exact class is used in both training (offline, on
    historical data) and inference (online, on live ticks). Any feature
    added here must work in both contexts.
    """

    def __init__(self, buffer_size: int = 600):
        self._prices: deque[tuple[int, float]] = deque(maxlen=buffer_size)
        self._volumes: deque[tuple[int, float]] = deque(maxlen=buffer_size)

    def update(self, timestamp_ns: int, price: float, volume: float = 0.0):
        self._prices.append((timestamp_ns, price))
        if volume > 0:
            self._volumes.append((timestamp_ns, volume))

    def compute(self, seconds_to_expiry: float = 300.0) -> np.ndarray | None:
        """Return feature vector or None if insufficient data."""
        if len(self._prices) < 30:
            return None
        # ... compute all feature groups ...
        return feature_vector  # shape: (31,)
```

### 2. Training Data

**Source:** Binance REST API for historical kline/trade data.

**Data collection:**
```
tools/collect_data.py --symbol BTCUSDT --interval 1s --start 2025-09-01 --end 2026-02-28
```

Stores in PostgreSQL:
```sql
CREATE TABLE btc_ticks (
    timestamp_ns    BIGINT PRIMARY KEY,
    price           DOUBLE PRECISION NOT NULL,
    volume          DOUBLE PRECISION DEFAULT 0,
    trades_count    INTEGER DEFAULT 0
);

CREATE TABLE btc_klines_1s (
    open_time_ns    BIGINT PRIMARY KEY,
    open            DOUBLE PRECISION,
    high            DOUBLE PRECISION,
    low             DOUBLE PRECISION,
    close           DOUBLE PRECISION,
    volume          DOUBLE PRECISION,
    trades          INTEGER
);
```

**Label generation:**
```python
# For each timestamp t:
#   label_5m  = 1 if price(t + 300s) > price(t) else 0
#   label_15m = 1 if price(t + 900s) > price(t) else 0
#   return_5m  = (price(t + 300s) - price(t)) / price(t)
#   return_15m = (price(t + 900s) - price(t)) / price(t)
```

**Train/test split:** Walk-forward validation.
- Train on rolling 4-week window
- Validate on next 1 week
- Slide forward by 1 week, repeat
- Never peek at future data

### 3. Model Training

```python
# tools/train_model.py

import lightgbm as lgb

# Walk-forward cross-validation
for train_period, val_period in walk_forward_splits(data):
    features_train = feature_engine.compute_batch(train_period)
    labels_train = generate_labels(train_period, horizon=300)

    features_val = feature_engine.compute_batch(val_period)
    labels_val = generate_labels(val_period, horizon=300)

    model = lgb.LGBMClassifier(
        n_estimators=500,
        max_depth=6,
        learning_rate=0.05,
        min_child_samples=100,
        subsample=0.8,
        colsample_bytree=0.8,
        reg_alpha=0.1,
        reg_lambda=0.1,
    )
    model.fit(
        features_train, labels_train,
        eval_set=[(features_val, labels_val)],
        callbacks=[lgb.early_stopping(50)],
    )

    # Evaluate
    evaluate_model(model, features_val, labels_val)

# Final model: train on all data except last week (held-out test)
final_model.save("models/btc_5m_v1.pkl")
```

**Hyperparameter tuning:** Use Optuna with walk-forward CV as the objective.

**Evaluation metrics:**
- **Brier score** (primary) — measures calibration of predicted probabilities
- **Log-loss** — penalizes confident wrong predictions
- **Accuracy** — baseline sanity check (should beat 50%)
- **Calibration curve** — plot predicted vs actual P(up) in decile bins
- **Profitability backtest** — simulate the full bot pipeline on historical data

### 4. Live Inference Integration

```python
# src/ml/model.py

class MLPredictor:
    """Async ML prediction source.

    Runs as an independent task, consuming from a tapped price queue
    and emitting Prediction objects into the shared prediction queue.
    """

    def __init__(
        self,
        price_queue: asyncio.Queue[PriceTick],
        prediction_queue: asyncio.Queue[Prediction],
        config: MLConfig,
    ):
        self._price_queue = price_queue
        self._pred_queue = prediction_queue
        self._cfg = config
        self._feature_engine = FeatureEngine()
        self._model = self._load_model(config.model_path)

    async def run(self):
        """Consume ticks and emit predictions."""
        asyncio.create_task(self._ingest_loop())
        while True:
            await asyncio.sleep(self._cfg.prediction_interval)
            prediction = self._predict()
            if prediction is not None:
                self._pred_queue.put_nowait(prediction)

    def _predict(self) -> Prediction | None:
        features = self._feature_engine.compute()
        if features is None:
            return None

        p_up = self._model.predict_proba(features.reshape(1, -1))[0, 1]
        confidence = abs(p_up - 0.5) * 2  # 0.5 → 0.0, 1.0 → 1.0

        if confidence < self._cfg.min_confidence:
            return None

        current_price = self._feature_engine.latest_price
        # Convert P(up) back to a predicted price
        # If P(up) > 0.5, predict price goes up proportional to confidence
        predicted_return = (p_up - 0.5) * 2 * self._cfg.max_predicted_return
        predicted_price = current_price * (1 + predicted_return)

        return Prediction(
            source="ml_lgbm",
            predicted_price=predicted_price,
            current_price=current_price,
            horizon_s=self._cfg.horizon_s,
            confidence=confidence,
        )
```

### 5. Configuration

```python
# config.py addition

@dataclass(frozen=True)
class MLConfig:
    """Machine learning prediction model configuration."""
    # Enable ML prediction source (requires trained model).
    enabled: bool = os.getenv("ML_ENABLED", "false").lower() in ("true", "1", "yes")
    # Path to trained model file (relative to project root).
    model_path: str = os.getenv("ML_MODEL_PATH", "models/btc_5m_v1.pkl")
    # Feature computation window (seconds of price history to maintain).
    feature_window: int = int(os.getenv("ML_FEATURE_WINDOW", "600"))
    # How often to emit predictions (seconds).
    prediction_interval: float = float(os.getenv("ML_PREDICTION_INTERVAL", "0.25"))
    # Minimum confidence to emit a prediction (filters noise).
    min_confidence: float = float(os.getenv("ML_MIN_CONFIDENCE", "0.55"))
    # Maximum predicted return magnitude (caps wild predictions).
    max_predicted_return: float = float(os.getenv("ML_MAX_PREDICTED_RETURN", "0.01"))
    # Prediction horizon in seconds (should match training labels).
    horizon_s: int = int(os.getenv("ML_HORIZON_S", "300"))
    # Blend weight: 1.0 = ML only, 0.0 = aggregator only.
    blend_weight: float = float(os.getenv("ML_BLEND_WEIGHT", "0.7"))
```

### 6. File Structure

```
src/
├── ml/
│   ├── __init__.py
│   ├── features.py         # FeatureEngine (shared train/serve)
│   ├── model.py            # MLPredictor (async inference wrapper)
│   ├── trainer.py          # Batch training pipeline
│   ├── data_collector.py   # Historical data download from Binance
│   ├── online_learner.py   # Phase 2: online SGD model
│   └── evaluation.py       # Backtesting, calibration, metrics
│
models/                      # Trained model artifacts (gitignored)
│   ├── btc_5m_v1.pkl
│   └── btc_15m_v1.pkl
│
tools/
├── collect_data.py          # CLI: download historical BTC data
├── train_model.py           # CLI: train LightGBM model
└── backtest.py              # CLI: backtest model on historical data
```

---

## Phase 2 Detailed Design: Online Learning

### Feedback Loop Architecture

```
Position Resolved ──▶ OnlineLearner
  (win/loss)              │
                          │ features_at_signal_time + outcome
                          ▼
                     SGD Classifier
                     (updates weights)
                          │
                          ▼
                   Blended Prediction
                   = alpha * LightGBM + (1-alpha) * OnlineSGD
```

### How It Works

1. When the strategy emits a Signal, the `FeatureEngine`'s current state is
   snapshotted and attached to the signal metadata.
2. When a position resolves (via `RiskManager.record_close()`), the outcome
   (profit = correct prediction, loss = wrong prediction) + feature snapshot
   become a training sample.
3. The `OnlineLearner` runs a single SGD update on this sample.
4. The blend weight (`alpha`) between batch and online models adapts based on
   recent accuracy: if the online model outperforms, its weight increases.

### Key Design Decisions

- **Model:** `sklearn.linear_model.SGDClassifier` with log-loss
  - Updates in microseconds
  - Naturally handles concept drift via learning rate
  - Can warm-start from a pre-trained model
- **Feature normalization:** Online running mean/std (Welford's algorithm)
- **Decay:** Exponentially discount old samples (effective window ~500 trades)
- **Safety:** If online model confidence < 0.52, fall back to batch model only

### New Dependencies

```
# requirements.txt additions
lightgbm>=4.0,<5
scikit-learn>=1.4,<2
optuna>=3.5,<4              # hyperparameter tuning (Phase 1)
```

---

## Phase 3: Deep Sequence Model (Conditional)

### When to Consider

Only pursue Phase 3 if ALL of the following are true:
- Phase 1+2 running profitably for 2+ weeks
- Accumulated 1M+ labeled samples from live trading
- Identified clear patterns in prediction errors that suggest temporal structure
  the tabular model misses
- GPU infrastructure is available (even a single A100 cloud instance)

### Architecture

**Temporal Fusion Transformer (TFT):**
- Processes raw tick sequences (price, volume) at 1s resolution
- Handles variable-length history via attention
- Multi-horizon output: P(up) at 1m, 5m, 15m simultaneously
- Interpretable attention weights show what the model focuses on

**Alternative: LSTM with attention:**
- Simpler, fewer parameters
- 2-layer bidirectional LSTM → attention → FC → P(up)
- Input: last 300 ticks as (return, volume, time_delta) tuples

### Inference Latency Concern

- LSTM inference on CPU: ~5-10ms for 300-step sequence → acceptable
- Transformer inference on CPU: ~20-50ms → might need ONNX export or GPU
- **Mitigation:** Run inference async, use last prediction if new one isn't ready

---

## Backtesting Framework

Before deploying any model live, it must pass the backtesting gauntlet:

```python
# tools/backtest.py

class Backtester:
    """Simulates the full bot pipeline on historical data.

    Replays historical BTC prices through:
      FeatureEngine → MLPredictor → StrategyEngine → RiskManager → PaperOrders

    Uses actual Polymarket historical book data (if available) or synthetic
    books for the orderbook side.
    """

    def run(self, start_date, end_date, model_path, config) -> BacktestResult:
        # 1. Load historical price data
        # 2. For each tick:
        #    a. Update FeatureEngine
        #    b. Generate ML prediction
        #    c. Generate synthetic book (or replay historical book)
        #    d. Run strategy evaluation
        #    e. Apply risk checks
        #    f. Simulate order fill
        #    g. Track P&L
        # 3. Return comprehensive metrics
        ...
```

**Backtest metrics:**
- Total P&L, Sharpe ratio, max drawdown
- Win rate, average win/loss ratio
- Trades per hour, average edge captured
- P&L by hour-of-day, by market regime
- Calibration: predicted P(up) vs actual frequency

**Pass criteria for live deployment:**
- Sharpe > 1.5 out-of-sample
- Win rate > 52%
- Max drawdown < 5% of capital
- Calibration error < 3% across all deciles

---

## Data Requirements and Storage

### Historical Data Needed

| Source | Data | Volume | Storage |
|--------|------|--------|---------|
| Binance 1s klines | OHLCV per second | ~2.6M rows/month | ~500MB/month in PG |
| Binance trades | Individual trades | ~50M/month | ~5GB/month in PG |
| Polymarket books | Book snapshots | Collected live | ~100MB/month |
| Bot trades | Signal + outcome | From matrix runs | ~10MB/month |

### PostgreSQL Schema

```sql
-- Historical BTC price data (from Binance)
CREATE TABLE btc_klines (
    open_time_ns    BIGINT NOT NULL,
    interval        TEXT NOT NULL,          -- '1s', '1m', etc.
    open            DOUBLE PRECISION,
    high            DOUBLE PRECISION,
    low             DOUBLE PRECISION,
    close           DOUBLE PRECISION,
    volume          DOUBLE PRECISION,
    trades_count    INTEGER,
    PRIMARY KEY (open_time_ns, interval)
);

-- ML training samples (auto-generated from live trading)
CREATE TABLE ml_training_samples (
    id              SERIAL PRIMARY KEY,
    timestamp_ns    BIGINT NOT NULL,
    features        JSONB NOT NULL,         -- feature vector as JSON
    label_5m        SMALLINT,               -- 1=up, 0=down (NULL if not yet resolved)
    label_15m       SMALLINT,
    return_5m       DOUBLE PRECISION,
    return_15m      DOUBLE PRECISION,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX idx_ml_samples_ts ON ml_training_samples(timestamp_ns);

-- Model performance tracking
CREATE TABLE ml_model_predictions (
    id              SERIAL PRIMARY KEY,
    model_version   TEXT NOT NULL,
    timestamp_ns    BIGINT NOT NULL,
    predicted_p_up  DOUBLE PRECISION,
    confidence      DOUBLE PRECISION,
    actual_outcome  SMALLINT,               -- NULL until resolved
    horizon_s       INTEGER,
    features_hash   TEXT,                    -- for reproducibility
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
```

---

## Risks and Mitigations

| Risk | Probability | Impact | Mitigation |
|------|-------------|--------|------------|
| **Overfitting** | High | Model looks great in backtest, fails live | Walk-forward CV, feature regularization, conservative tree depth |
| **Train/serve skew** | Medium | Features computed differently in training vs inference | Single `FeatureEngine` class used in both contexts |
| **Concept drift** | High | Market regime changes, model degrades | Phase 2 online learning; automated Brier score monitoring |
| **Latency regression** | Low | ML inference adds too much latency | LightGBM is <1ms; pre-compute features on tick ingestion |
| **Data quality** | Medium | Binance outages, missing ticks create gaps | Gap detection in feature engine; skip prediction when data stale |
| **Spurious features** | Medium | Features that correlate by chance, not causation | Feature importance analysis; ablation studies; cross-val |

---

## Implementation Order (Phase 1 Tasks)

1. **Set up data infrastructure** (~1 day)
   - Add `btc_klines` table to PostgreSQL
   - Write `tools/collect_data.py` to download from Binance API
   - Download 3 months of 1s kline data

2. **Build FeatureEngine** (~2 days)
   - Implement all feature groups in `src/ml/features.py`
   - Write unit tests for each feature computation
   - Verify features produce same output on batch data vs streaming ticks

3. **Generate training labels** (~0.5 day)
   - Label each timestamp with 5m and 15m outcomes
   - Store in PostgreSQL for fast querying

4. **Train initial model** (~1 day)
   - Implement `tools/train_model.py`
   - Walk-forward cross-validation
   - Hyperparameter tuning with Optuna
   - Evaluate calibration and profitability

5. **Build backtester** (~1.5 days)
   - Implement `tools/backtest.py`
   - Replay historical data through full pipeline
   - Validate that ML model passes deployment criteria

6. **Integrate MLPredictor into bot** (~1 day)
   - Implement `src/ml/model.py`
   - Add `MLConfig` to config.py
   - Wire into `main.py` with feature flag
   - Add ML profile to matrix test

7. **Validate live** (~1 day)
   - Run matrix test with ML-on vs ML-off profiles
   - Compare win rates, P&L, Sharpe ratios
   - Iterate on features/hyperparameters based on results

**Total estimated Phase 1 effort: ~8 days**

---

## Success Criteria

### Phase 1 (Batch Model)
- [ ] Brier score < 0.24 out-of-sample (better than 50/50 = 0.25)
- [ ] Win rate > 52% in backtests
- [ ] ML profiles outperform non-ML profiles in 6h+ matrix tests
- [ ] Inference latency < 5ms (p99)

### Phase 2 (Online Learning)
- [ ] Online model adapts within 50 trades to regime change
- [ ] Blended model beats batch-only model by 1%+ win rate
- [ ] No degradation in worst-case (online model safely bounded)

### Phase 3 (Deep Model)
- [ ] Sequence model captures patterns missed by tabular model
- [ ] Sharpe ratio improvement > 0.3 vs Phase 1+2
- [ ] Inference latency < 20ms on CPU

---

## Dependencies to Add

```
# requirements.txt additions for Phase 1
lightgbm>=4.0,<5
scikit-learn>=1.4,<2
optuna>=3.5,<4
psycopg2-binary>=2.9,<3     # or asyncpg for async PG access
joblib>=1.3,<2               # model serialization
```

---

## Questions to Resolve Before Implementation

1. **Training data horizon:** Start with 3 months? 6 months? More data helps but BTC
   regime may have shifted.
2. **Multi-horizon vs single model:** One model per timeframe (5m, 15m) or one model
   with timeframe as a feature?
3. **Polymarket book data:** Can we collect historical book snapshots for backtesting,
   or rely entirely on synthetic books for the backtest?
4. **GPU for Phase 3:** Cloud GPU (Lambda, RunPod, etc.) or local?
5. **Retraining frequency:** Nightly? Weekly? On-demand when drift detected?
