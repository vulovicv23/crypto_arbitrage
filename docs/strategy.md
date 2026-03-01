# Strategy Engine

> **Source file:** `src/strategy.py`
> **Class:** `StrategyEngine`
> **Config:** `StrategyConfig` in `config.py`
> **Models:** `MarketContext`, `TokenOutcome`, `Signal`, `SignalStrength`, `MarketRegime` in `src/models.py`

The strategy engine is the core decision-maker. It consumes `Prediction` objects from the prediction aggregator and `PolymarketBook` snapshots from the WebSocket feed, estimates the probability that BTC will go up before each market's expiry using a normal CDF model, compares that probability to the market-implied probability (mid-price), and emits BUY signals for underpriced tokens.

---

## Table of Contents

1. [Data Flow](#data-flow)
2. [Binary Contract Semantics](#binary-contract-semantics)
3. [Probability Model](#probability-model)
4. [Edge Formula](#edge-formula)
5. [Signal Strength Classification](#signal-strength-classification)
6. [Market Regime Detection](#market-regime-detection)
7. [Prediction Sources](#prediction-sources)
8. [Token Mapping and Market Context](#token-mapping-and-market-context)
9. [Edge Cases](#edge-cases)
10. [Modification Guide](#modification-guide)

---

## Data Flow

```
prediction_queue ──> StrategyEngine._evaluate()
                          |
                          |  for each monitored book:
                          |    1. look up MarketContext (YES/NO token, expiry)
                          |    2. compute P(BTC up) via CDF model
                          |    3. derive fair value for this token
                          |    4. edge = fair_value - mid_price
                          |    5. filter by min/max threshold
                          |    6. classify strength, attach regime
                          |    7. emit BUY signal (positive edge only)
                          |
                          v
                     signal_queue ──> OrderManager
```

The strategy runs in an infinite loop (`StrategyEngine.run()`). Each iteration:

1. Awaits the next `Prediction` from `self._pred_queue`.
2. Tracks BTC price from the prediction for volatility estimation.
3. Computes BTC return volatility from recent tick history.
4. Calls `self._evaluate(prediction)`, which iterates over **all** currently monitored order books (`self._books`).
5. For each valid book with a `MarketContext`, computes the probability-based edge and (if it passes thresholds) emits a `Signal`.

Book state is updated asynchronously via `on_book_update()`, which is called by the Polymarket WebSocket handler. Every book update also appends to `_price_history` and triggers a regime re-classification.

---

## Binary Contract Semantics

Polymarket BTC Up/Down markets are **binary contracts**:

- **YES token** = bet that BTC goes **UP** from creation price to expiry
- **NO token** = bet that BTC goes **DOWN** from creation price to expiry
- Each token pays **$1** if the outcome occurs, **$0** otherwise
- Token mid-price IS the market-implied probability of that outcome

There is **no strike price**. These markets are purely directional ("did BTC go up or down?"). The strategy only needs to estimate P(BTC goes up) independently and compare to the market price.

**Trading model:** BUY-only signals. On Polymarket, you buy YES or buy NO tokens. You never short a token. An overpriced YES token means an underpriced NO token, so the strategy naturally buys the complementary token instead.

---

## Probability Model

Defined in `StrategyEngine._compute_p_up()` (`src/strategy.py`).

### CDF Formula

```
P(BTC goes up) = Phi(z)

where:
    z = (predicted_return / scaled_vol) * confidence
    scaled_vol = btc_volatility * sqrt(seconds_remaining)
    Phi(x) = 0.5 * (1 + erf(x / sqrt(2)))    # standard normal CDF
```

### Components

| Component           | Definition                                                              | Source                                  |
|---------------------|-------------------------------------------------------------------------|-----------------------------------------|
| `predicted_return`  | `(predicted_price - current_price) / current_price`                     | `Prediction.predicted_return` property  |
| `btc_volatility`    | Std dev of recent BTC tick-to-tick returns                              | `_btc_return_volatility()`              |
| `seconds_remaining` | Seconds until market resolves                                           | `MarketContext.seconds_remaining()`     |
| `confidence`        | R-squared of linear regression fit, blended across sources (0.0-1.0)    | `Prediction.confidence`                 |

### How the Model Works

Under a log-normal random walk, BTC returns over time T are approximately:
`R ~ N(mu*T, sigma^2 * T)`

The predicted return serves as our estimate of `mu*T`, and we scale uncertainty by `sqrt(T)`:

- **sqrt(T) scaling** gives proper time decay. As expiry approaches (T -> 0):
  - If BTC is clearly moving in one direction, z explodes, P -> 0 or 1
  - This is the correct time-decay behavior for binary contracts
  - Near-expiry markets with clear momentum produce strong, legitimate signals

- **Confidence dampens z-score** (not probability directly):
  - `z *= confidence` means low-confidence predictions push P toward 0.5 (uncertain)
  - Confidence = 0 gives z = 0, which gives P = 0.5 (maximum uncertainty, no edge)
  - This is more principled than dampening probability directly

- **Probability is clamped** to `[0.01, 0.99]` for numerical safety

### BTC Return Volatility

Defined in `StrategyEngine._btc_return_volatility()` (`src/strategy.py`).

Uses BTC price observations from predictions (not contract mid-prices):

```python
prices = recent BTC prices from _btc_price_history[-volatility_window:]
returns = np.diff(prices) / prices[:-1]
volatility = np.std(returns)
```

Requires at least 10 observations. Returns 0.0 if insufficient data (triggers conservative fallback).

---

## Edge Formula

For each binary contract token:

```
# YES token:
fair_value = P(BTC goes up)
edge = fair_value - mid_price

# NO token:
fair_value = 1 - P(BTC goes up)
edge = fair_value - mid_price
```

**Only positive edges produce signals.** If edge <= 0, the token is fairly priced or overpriced, and is skipped. The complementary token will naturally have the positive edge.

**Threshold filtering:**

| Check                | Config Key            | Default  | Behavior                                             |
|----------------------|-----------------------|----------|------------------------------------------------------|
| Minimum edge         | `MIN_EDGE_THRESHOLD`  | `0.02`   | Below this, signal is ignored (no trade)             |
| Maximum edge         | `MAX_EDGE_THRESHOLD`  | `0.30`   | Above this, signal is discarded as stale/erroneous   |

```python
# From _evaluate():
if edge < self._cfg.min_edge_threshold:
    continue  # too small to trade
if edge > self._cfg.max_edge_threshold:
    continue  # too large -- likely stale or erroneous
```

The thresholds are in **probability space** (not return space). An edge of 0.02 means our model estimates 2% higher probability than the market. Near-expiry markets can legitimately show large edges (0.10-0.25), so the max threshold is generous at 0.30.

### Additional Filters

| Filter                        | Condition                   | Reason                                    |
|-------------------------------|-----------------------------|-------------------------------------------|
| Near-expiry skip              | `seconds_left < 5`         | Order fills become unreliable             |
| Extreme mid-price skip        | `mid < 0.01` or `mid > 0.99` | Fully priced in, no edge to capture    |
| No MarketContext              | `market_ctx is None`       | Token not mapped to a discovered market   |
| Unknown token outcome         | `outcome is None`          | Token not YES or NO for this market       |

---

## Signal Strength Classification

Defined in `StrategyEngine._classify_strength()` (`src/strategy.py`).

The classification uses multiples of `min_edge_threshold` (denoted `t`):

```python
def _classify_strength(self, abs_edge: float) -> SignalStrength:
    t = self._cfg.min_edge_threshold
    if abs_edge < 1.5 * t:
        return SignalStrength.WEAK
    elif abs_edge < 2.5 * t:
        return SignalStrength.MODERATE
    else:
        return SignalStrength.STRONG
```

With the default `min_edge_threshold = 0.02`:

| Strength   | Edge Range (probability)  | Example Edge | Position Size Multiplier |
|------------|---------------------------|--------------|--------------------------|
| `WEAK`     | `0.02 <= edge < 0.03`    | 0.025        | 0.5x                     |
| `MODERATE` | `0.03 <= edge < 0.05`    | 0.04         | 0.75x                    |
| `STRONG`   | `0.05 <= edge < 0.30`    | 0.10         | 1.0x                     |

The strength multiplier is applied during position sizing in the risk manager (see `docs/RISK_MANAGEMENT.md`).

---

## Market Regime Detection

Defined in `StrategyEngine._update_regime()` (`src/strategy.py`).

The regime classifies current market conditions based on **contract mid-prices** (not BTC prices):

```python
class MarketRegime(Enum):
    TRENDING_UP = auto()
    TRENDING_DOWN = auto()
    SIDEWAYS = auto()
```

### Dual-EMA Crossover

Two exponential moving averages are maintained on incoming book mid-prices:

| EMA    | Config Key       | Default Span | Alpha Formula              |
|--------|------------------|-------------|----------------------------|
| Fast   | `EMA_FAST_SPAN`  | 12          | `2 / (span + 1) = 0.1538`  |
| Slow   | `EMA_SLOW_SPAN`  | 26          | `2 / (span + 1) = 0.0741`  |

Update logic (executed on every `on_book_update()` call with a valid mid-price):

```python
self._ema_fast = alpha_fast * price + (1 - alpha_fast) * self._ema_fast
self._ema_slow = alpha_slow * price + (1 - alpha_slow) * self._ema_slow
```

On first tick, both EMAs are initialized to the incoming price.

### Volatility Calculation (Regime Detection)

Defined in `_recent_volatility()` (`src/strategy.py`). This uses **contract mid-prices** (not BTC prices, which are used separately for the probability model):

```python
prices = last N mid-prices  # N = VOLATILITY_WINDOW (default 60)
returns = np.diff(prices) / prices[:-1]
volatility = np.std(returns)
```

### Classification Rules

```python
diff_pct = (ema_fast - ema_slow) / ema_slow

if volatility < 0.001:
    regime = SIDEWAYS           # very low vol overrides EMA signal
elif diff_pct > 0.0005:
    regime = TRENDING_UP        # fast EMA above slow by > 0.05%
elif diff_pct < -0.0005:
    regime = TRENDING_DOWN      # fast EMA below slow by > 0.05%
else:
    regime = SIDEWAYS           # EMAs converged
```

**Key thresholds hardcoded in `_update_regime()`:**
- Volatility floor: `0.001` -- below this, regime is always SIDEWAYS.
- EMA diff threshold: `0.0005` (0.05%) -- separates trending from sideways.

### Regime Impact on Position Sizing

The regime affects position sizing via multipliers in the risk manager:

| Regime           | Size Multiplier (default) | Config Key                  |
|------------------|---------------------------|-----------------------------|
| `SIDEWAYS`       | 0.4x                     | `SIDEWAYS_SIZE_MULTIPLIER`  |
| `TRENDING_UP`    | 1.0x                     | `TREND_SIZE_MULTIPLIER`     |
| `TRENDING_DOWN`  | 1.0x                     | `TREND_SIZE_MULTIPLIER`     |

### Price History Buffers

```python
# Contract mid-prices for regime detection
self._price_history: deque[float] = deque(
    maxlen=max(ema_slow_span * 2, volatility_window * 2)
)

# BTC prices for probability model volatility
self._btc_price_history: deque[float] = deque(
    maxlen=volatility_window * 2
)
```

With defaults, `_price_history` maxlen = `max(52, 120) = 120` mid-price values, and `_btc_price_history` maxlen = `120`.

---

## Prediction Sources

Defined in `src/prediction_sources.py`, class `PredictionAggregator`.

### Architecture

```
BinanceWS ────┐
CryptoCompare ┼──> price_queue ──> PredictionAggregator ──> prediction_queue
CoinGecko ────┘
```

The aggregator runs two concurrent async loops:

1. **Ingest loop** (`_ingest_loop`) -- drains `price_queue`, appends `(timestamp_ns, price)` tuples to per-source rolling windows (deque, `maxlen=300`).
2. **Emit loop** (`_emit_loop`) -- every `0.25` seconds, calls `_generate_prediction()` and pushes the result to `prediction_queue`.

### Per-Source Extrapolation (Linear Regression)

For each source with >= 10 ticks in its window, the aggregator performs linear regression on `(time_seconds, price)` using a manual formula (no scipy dependency):

```python
# Normalize timestamps to seconds from first tick
ts_s = (timestamps - timestamps[0]) / 1e9

# Linear regression coefficients
slope = (n * sum(x*y) - sum(x) * sum(y)) / (n * sum(x^2) - sum(x)^2)
intercept = (sum(y) - slope * sum(x)) / n

# Extrapolate to prediction horizon
future_t = ts_s[-1] + prediction_horizon_s   # default 900s = 15 min
predicted_price = intercept + slope * future_t

# Confidence = R-squared of the regression
R_squared = 1 - (SS_residual / SS_total)
confidence = clamp(R_squared, 0.0, 1.0)
```

### Cross-Source Blending

Multiple per-source predictions are blended using confidence-weighted averaging:

```python
blended_price = sum(predicted_price_i * confidence_i) / sum(confidence_i)
avg_confidence = sum(confidence_i) / num_sources
```

### Agreement Factor

When >= 2 sources are available, an agreement factor penalizes divergence between sources:

```python
cv = std(predicted_prices) / mean(predicted_prices)  # coefficient of variation
agreement_factor = max(0.0, 1.0 - cv * 100)
avg_confidence *= agreement_factor
```

If sources agree (low CV), confidence stays high. If sources diverge, confidence drops sharply. The final confidence is clamped to `[0.0, 1.0]`.

### Current Price Selection

The "current price" used as the reference in `Prediction.current_price` is selected by source priority:

1. `binance` (lowest latency WebSocket)
2. `cryptocompare`
3. `coingecko`
4. Any available source (fallback)

---

## Token Mapping and Market Context

The strategy needs two things for each tracked token:
1. A `MarketContext` that identifies the token as YES or NO and knows the expiry time.
2. The `condition_id` for constructing valid `Signal` objects.

### Auto-Discovery Mode (default, `DISCOVERY_ENABLED=true`)

1. `MarketDiscovery` periodically queries the Gamma API for active BTC Up/Down markets.
2. For each discovered market, it provides `yes_token_id` (Up) and `no_token_id` (Down), `end_date`, `timeframe`, and `asset`.
3. `main.py` builds `MarketContext` objects from each `DiscoveredMarket` and calls `strategy.set_market_contexts(contexts)`.
4. In `_evaluate()`, the strategy looks up the `MarketContext` for each token, determines the YES/NO outcome, and uses the expiry time for the probability model.

**`MarketContext` dataclass:**

```python
@dataclass(frozen=True, slots=True)
class MarketContext:
    condition_id: str
    yes_token_id: str           # token for "BTC goes UP"
    no_token_id: str            # token for "BTC goes DOWN"
    end_date_ns: int            # resolution time as epoch nanoseconds
    timeframe_seconds: int      # 300 (5m), 900 (15m), 3600 (1h), 14400 (4h)
    asset: str                  # e.g. "BTC"

    def seconds_remaining(self) -> float: ...
    def token_outcome(self, token_id: str) -> TokenOutcome | None: ...
```

### Static Mode (`DISCOVERY_ENABLED=false`)

1. Condition IDs are read from `POLY_BTC_CONDITION_IDS` (comma-separated env var).
2. The mapping is `{cid: cid}` -- the condition ID is used as both key and value.
3. The strategy receives book updates but **cannot compute probability-based edges** without `MarketContext` (no YES/NO distinction, no expiry time). Tokens without context are skipped.
4. The deprecated `set_token_mapping()` method is maintained for backward compatibility.

---

## Edge Cases

| Case | Handling |
|------|----------|
| Market expired (0s left) | `_compute_p_up` returns 0.99/0.01 by predicted direction, or 0.50 if zero return |
| Near-expiry (< 5s) | `_evaluate` skips -- no time to fill reliably |
| Zero volatility (< 10 BTC ticks) | Conservative linear fallback: `p = 0.5 + pred_return * 10`, stays near 0.5 |
| Near-zero volatility (< 1e-10) | Same conservative fallback |
| Extreme volatility | CDF naturally compresses z -> P stays near 0.5 (high uncertainty) |
| Confidence = 0 | z = 0 -> P = 0.5 -> no edge -> no signal |
| Mid-price at 0.01 or 0.99 | Skipped (fully priced in, no meaningful edge) |
| Unknown token outcome | `market_ctx.token_outcome()` returns None -> skipped |
| No MarketContext | Token skipped entirely (static mode fallback) |
| Both YES and NO have positive edge | Impossible by construction (YES edge + NO edge = 0) |
| P(up) caching | Computed once per condition_id per evaluation (YES and NO share the same P) |

---

## Modification Guide

### How to Change the Probability Model

The probability model lives in `StrategyEngine._compute_p_up()` in `src/strategy.py`.

**To replace the normal CDF model:**

1. Modify `_compute_p_up()`. It receives `(prediction, market_ctx, btc_volatility)` and must return a `float` in `[0.01, 0.99]` representing P(BTC goes up).
2. The return value is used directly: YES fair value = P(up), NO fair value = 1 - P(up).

Example -- replacing with a logistic model:

```python
def _compute_p_up(self, prediction, market_ctx, btc_volatility):
    pred_return = prediction.predicted_return
    seconds_left = market_ctx.seconds_remaining()
    # Logistic: P = 1 / (1 + exp(-k * pred_return * confidence / vol))
    k = 100  # steepness
    vol = max(btc_volatility * sqrt(seconds_left), 1e-10)
    x = k * pred_return * prediction.confidence / vol
    p = 1.0 / (1.0 + exp(-x))
    return max(0.01, min(0.99, p))
```

### How to Change the Prediction Model

See `PredictionAggregator._extrapolate()` and `._generate_prediction()` in `src/prediction_sources.py`.

### How to Add a New Signal Filter

Add a check inside `StrategyEngine._evaluate()` (the `for token_id, book in self._books.items()` loop). For example, to filter by spread width:

```python
# After the edge threshold checks, before constructing the Signal:
if book.spread > 0.05:
    logger.debug("Spread too wide (%.4f) -- skipping", book.spread)
    continue
```

### How to Add a New Regime Type

1. Add the new value to `MarketRegime` enum in `src/models.py`:

```python
class MarketRegime(Enum):
    TRENDING_UP = auto()
    TRENDING_DOWN = auto()
    SIDEWAYS = auto()
    HIGH_VOLATILITY = auto()  # new
```

2. Add classification logic in `StrategyEngine._update_regime()` in `src/strategy.py`:

```python
if vol > 0.01:
    self._regime = MarketRegime.HIGH_VOLATILITY
elif vol < 0.001:
    self._regime = MarketRegime.SIDEWAYS
elif diff_pct > 0.0005:
    # ... existing logic
```

3. Add a sizing multiplier in `RiskManager._compute_size()` in `src/risk_manager.py`.

4. Add a config parameter in `RiskConfig` in `config.py`.

### How to Adjust Edge Thresholds

Edge thresholds are in **probability space**:

- `MIN_EDGE_THRESHOLD=0.02`: Require 2% probability advantage (conservative). Lower for more signals, raise for fewer but higher-conviction.
- `MAX_EDGE_THRESHOLD=0.30`: Filter edges above 30% (likely stale data). Near-expiry markets can legitimately show 10-25% edges, so this is intentionally generous.

The `_classify_strength()` method auto-calibrates to the min threshold (1.5x and 2.5x multiples).

### How to Adjust EMA Sensitivity

- **Faster detection:** Decrease `EMA_FAST_SPAN` (e.g., 6 instead of 12).
- **Smoother detection:** Increase `EMA_SLOW_SPAN` (e.g., 50 instead of 26).
- **Wider sideways zone:** Increase the `0.0005` diff_pct threshold in `_update_regime()` (requires code change).

### Queue Back-Pressure

All queues in the pipeline use a drop-oldest strategy when full:

```python
try:
    self._signal_queue.put_nowait(sig)
except asyncio.QueueFull:
    self._signal_queue.get_nowait()  # drop oldest
    self._signal_queue.put_nowait(sig)
```

Queue sizes: `price_queue=5000`, `prediction_queue=500`, `signal_queue=200`.

### Signal Analytics Fields

Each emitted `Signal` includes analytics fields for logging and post-trade analysis:

| Field               | Type    | Description                                          |
|---------------------|---------|------------------------------------------------------|
| `p_up`              | `float` | Estimated P(BTC goes up) from the CDF model          |
| `outcome`           | `str`   | `"YES"` or `"NO"` -- which token is being bought     |
| `seconds_to_expiry` | `float` | Time remaining when the signal was generated          |
| `btc_volatility`    | `float` | BTC return volatility used in the computation         |
