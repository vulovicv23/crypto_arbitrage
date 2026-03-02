# Crypto Arbitrage Bot — Issues Audit

**Date:** 2026-03-02
**Last Updated:** 2026-03-02 (Round 2 — Nice-to-haves)

---

## Fix Status Summary

| # | Issue | Status | Relevance to 7-Day Test |
|---|-------|--------|------------------------|
| 1 | Train/inference feature mismatch | **FIXED** (Round 1) | YES — #1 priority. Smoking gun for ML underperformance |
| 2 | Capital never updates | **FIXED** (Round 1) | YES — sizes degrade over time |
| 8 | Stale books accumulate | **FIXED** (Round 1) | YES — memory leak over 7 days |
| 6 | Price source death is silent | **FIXED** (Round 1) | YES — could lose price feeds mid-run |
| 14 | Token unsubscribe misses expired markets | **FIXED** (Round 2) | Nice to have — subscription leak |
| 15 | Config validation is minimal | **FIXED** (Round 2) | Nice to have — catches bad config |
| 17 | `_filled_orders` grows unbounded | **FIXED** (Round 2) | Nice to have — memory over 7 days |
| 22 | Cooldown resets on break-even trades | **FIXED** (Round 2) | Nice to have — risk accuracy |
| 28 | Full history recompute | **Won't fix** | Not a real bottleneck (<2ms in numpy) |
| 3 | `_track_order` assumes filled | Not fixed | Live-trading only |
| 4 | Position limits bypassable | Not fixed | Live-trading only |
| 5 | No per-market concentration limit | Not fixed | Live-trading only |
| 7 | Dict mutation during iteration | N/A | False alarm (no yield points in loop) |
| 9 | No maker order cancellation | Not fixed | Live-trading only |
| 10 | Fire-and-forget tracking tasks | Not fixed | Live-trading only |
| 11 | Market expiry mid-trade | Not fixed | Live-trading only |
| 13 | close_position() assumes fill | Not fixed | Live-trading only |

---

## FIXED Issues — Details

### Round 1: Priority Fixes for 7-Day Dry-Run Test

#### 1. Train/Inference Feature Mismatch (ML) — FIXED
- **Files changed:** `src/ml/features.py`, `src/ml/predictor.py`
- **Root cause:** `FeatureEngine` stored raw ticks where `open=close=high=low=price`, zeroing out all candlestick microstructure features (45-50) and neutralizing volume imbalance (37-38). Training used proper 1-second Binance OHLCV klines.
- **Fix:** Added 1-second bar aggregation inside `FeatureEngine`. Ticks are now aggregated into OHLCV bars matching the training data format:
  - Within each 1-second window: `open=first tick`, `high=max`, `low=min`, `close=last tick`, `volume=sum`, `trades=count`
  - Bar finalizes when a tick arrives with a new second
  - Added `_opens` deque so `compute()` passes distinct opens vs closes to `compute_batch()`
  - `compute()` includes the pending in-progress bar non-destructively (no state mutation)
- **Tests:** All 40 feature tests pass including batch-streaming parity

#### 2. Capital Never Updates — Risk Sizing Degrades — FIXED
- **File changed:** `src/risk_manager.py`
- **Root cause:** `self._state.capital` set once at init, never updated with realized PnL.
- **Fix:** Added `self._state.capital += pnl` in `record_close()`. Position sizing now tracks realized gains/losses. Daily loss limit also automatically reflects current capital.

#### 8. Stale Books Accumulate Forever — FIXED
- **Files changed:** `src/strategy.py`, `main.py`
- **Root cause:** Expired market tokens never removed from `_books` dict in strategy, or `_market_contexts`/`_reference_prices` in PositionResolver.
- **Fix:**
  - `StrategyEngine.set_market_contexts()` now prunes `_books` entries for tokens no longer in the active market set
  - `PositionResolver._prune_stale_contexts()` removes contexts and reference prices for expired conditions with no open positions, called from `_check_expired()`

#### 6. Price Source Death Is Silent — FIXED
- **File changed:** `src/prediction_sources.py`
- **Root cause:** `PriceSource.start()` caught exceptions, logged once, and exited permanently.
- **Fix:** Added retry loop with exponential backoff (1s → 2s → 4s → ... → 60s max). Source now auto-restarts after crashes instead of dying silently. Clean cancellation (`CancelledError`) still exits immediately.

---

### Round 2: Nice-to-Have Fixes

#### 14. Token Unsubscribe Misses Already-Expired Markets — FIXED
- **File changed:** `main.py`
- **Root cause:** The unsubscribe block (line 626) looked up expired tokens from `token_mapping`, which is built from `self._discovery.active_markets`. By the time the callback fires, expired markets have already been removed from `active_markets`, so their tokens were never found — leaving stale WS subscriptions.
- **Fix:** Added a persistent `_token_to_condition` dict to the `Bot` class that maps `token_id → condition_id` for all tokens we've ever subscribed to. The unsubscribe block now uses this persistent mapping instead of `token_mapping`. Tokens are cleaned from the mapping when unsubscribed.

#### 15. Config Validation Is Minimal — FIXED
- **File changed:** `config.py`
- **Root cause:** `_validate()` only checked for required API keys and condition IDs. No semantic constraint checks.
- **Fix:** Added comprehensive validation in `_validate()`:
  - Strategy: `min_edge > 0`, `max_edge > min_edge`, `max_spread ∈ (0, 1)`
  - Risk: all percentages in `(0, 1)`, `max_open_positions ≥ 1`, `cooldown_after_losses ≥ 1`
  - Execution: `max_latency_ms > 0`, `max_orders_per_second ≥ 1`
  - Fees: `taker_fee_pct ∈ [0, 1)`, `maker_fee_pct ∈ [0, 1)`
  - ML: `feature_window ≥ 100`, `prediction_interval > 0`, `min_confidence ∈ [0, 1]`
  - Expiry buckets: `near_expiry_s < far_expiry_s`

#### 17. `_filled_orders` Grows Unbounded — FIXED
- **File changed:** `src/order_manager.py`
- **Root cause:** `self._filled_orders: list[Order] = []` grew without limit, one entry per fill.
- **Fix:** Changed to `deque[Order]` with `maxlen=5000`. Old filled orders are automatically evicted.

#### 22. Cooldown Resets on Break-Even Trades — FIXED
- **File changed:** `src/risk_manager.py`
- **Root cause:** In `record_close()`, the `else` branch reset `consecutive_losses = 0` for all non-negative PnL, including break-even (`pnl == 0`). A series of losses followed by a `$0` trade would wrongly reset the loss streak.
- **Fix:** Changed `else:` to `elif pnl > 0:` so only genuinely profitable trades reset the consecutive loss counter. Break-even trades leave the streak unchanged.

---

### Won't Fix

#### 28. Feature Engine Recomputes Entire History Every Cycle — Won't Fix
- **Investigation:** `compute_batch()` processes ~4000 1s bars every 250ms. Analyzed whether trimming input to `_MIN_TICKS=3661` bars would help.
- **Finding:** Trimming breaks multi-timeframe bar alignment (1m/5m bars are grouped from the beginning of the buffer). The actual cost of numpy vectorized operations over 4000 elements is <2ms — under 1% of the 250ms prediction interval. This is not a real bottleneck.
- **Decision:** Not worth the complexity. Buffer is already capped at 4000 by deque maxlen.

---

## Remaining Issues — Not Fixed (with rationale)

### Critical (Live-Trading Only)

#### 3. `_track_order` Assumes "Missing = Filled"
- **File:** `src/order_manager.py:216-218`
- **Issue:** When an order disappears from open orders, the code assumes it was filled. It could have been cancelled, rejected, or the market expired. This creates **phantom positions** in the risk manager that never resolve.
- **Why not fixed:** Only affects live trading, not dry-run. Dry-run mode uses `DryRunOrderManager` which simulates fills directly.

#### 4. Position Limits Bypassable via Async Race
- **Issue:** Multiple signals can pass `check_signal()` simultaneously before any fills are recorded. If you have 19/20 positions and 5 signals pass the check at once, you could end up with 24 open positions.
- **Why not fixed:** Only affects live trading. In dry-run, fills are instant (no async delay).

#### 5. No Per-Market Concentration Limit
- **Issue:** Risk manager only tracks total positions. All 20 positions could be on the same 5-minute expiry, creating catastrophic correlated loss.
- **Why not fixed:** Important for live trading risk management, but doesn't affect dry-run metrics.

### High Priority (Not Blocking 7-Day Test)

#### 7. `_books` Dict Mutation During Iteration — False Alarm
- **File:** `src/strategy.py:202`
- **Issue:** `_evaluate()` iterates `self._books.items()` while `on_book_update()` can insert new entries.
- **Why not fixed:** In Python's asyncio cooperative model, dict mutation during iteration requires a yield point within the iteration. `_evaluate()` is synchronous — no `await` inside the loop — so this is a **false alarm**. No yield points means no interleaving.

#### 9. No Maker Order Cancellation
- **Issue:** GTC limit orders sit on the book indefinitely. No stale-order reaper.
- **Why not fixed:** Live-trading only.

#### 10. Fire-and-Forget Order Tracking Tasks
- **File:** `src/order_manager.py:185`
- **Issue:** `asyncio.create_task(self._track_order(...))` tasks are never stored or awaited.
- **Why not fixed:** Live-trading only.

### Medium Priority

#### 11. Market Expiry Mid-Trade
- **Why not fixed:** Live-trading only (see #3).

#### 12. ML + Aggregator Compete on Same Queue
- **Issue:** Both emit to the same queue. Under contention, one starves the other.
- **Why not fixed:** Low practical impact — prediction interval (250ms) is fast enough that queue rarely fills.

#### 13. `close_position()` Assumes Immediate Fill
- **Why not fixed:** Live-trading only.

### Lower Priority

#### 16. Blocking I/O in Async Context
- `joblib.load()` at startup and trade log `open("a")` calls block the event loop.

#### 18. CryptoCompare Leaks API Key in WebSocket URL
- Visible in error tracebacks if logged at DEBUG level.

#### 19. HMAC Clock Skew Vulnerability
- No clock sync mechanism. Live-trading only.

#### 20. `assert` in Production Code
- Stripped with `python -O`. Should be explicit `if`/`raise`.

#### 21. Polymarket Price Tick Size Mismatch
- Inconsistent rounding between order_manager (2dp) and polymarket_client (4dp).

#### 23. No Model Staleness Detection (ML)
- Model loaded once, used forever. No accuracy monitoring.

#### 24. No Duplicate Signal Detection
- Strategy can emit multiple signals for the same token. Minor in dry-run.

#### 25. WebSocket Reconnect Can Lose Subscriptions
- No lock on `_subscriptions` during reconnect. Rare race condition.

#### 26. No Stale Book Detection
- Bot doesn't detect if WS is connected but not sending book data.

#### 27. Slug Pattern Matching Is Fragile
- Hardcoded slug patterns could break if Polymarket changes format.

#### 29. Calibrator Output Not Validated
- `calibrator.predict()` output not checked for [0, 1] range.

#### 30. Dry-Run Mode Bypasses Config Validation
- Failed config load falls back to raw `AppConfig()` defaults.
