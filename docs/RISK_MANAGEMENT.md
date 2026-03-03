# Risk Management

> **Source file:** `src/risk_manager.py`
> **Classes:** `RiskManager`, `RiskState`
> **Config:** `RiskConfig`, `ExecutionConfig` in `config.py`
> **Models:** `DailyPnL`, `Position`, `Signal`, `MarketRegime`, `SignalStrength` in `src/models.py`

The risk manager is a stateful gate that sits between the strategy engine and order execution. Every signal must pass through `RiskManager.check_signal()` before an order is built. It enforces position limits, daily loss caps, cooldowns, latency budgets, and computes regime-adaptive position sizes.

---

## Table of Contents

1. [Risk Check Pipeline](#risk-check-pipeline)
2. [Position Sizing Formula](#position-sizing-formula)
3. [Daily P&L Tracking](#daily-pnl-tracking)
4. [Cooldown Logic](#cooldown-logic)
5. [Daily Loss Halt](#daily-loss-halt)
6. [Regime Multipliers](#regime-multipliers)
7. [Strength Multipliers](#strength-multipliers)
8. [Risk State](#risk-state)
9. [Configuration Reference](#configuration-reference)

---

## Risk Check Pipeline

All checks are executed sequentially in `RiskManager.check_signal()`. The method returns `(approved: bool, position_size: float, reason: str)`.

```python
def check_signal(self, signal: Signal) -> tuple[bool, float, str]:
```

### Check Order

| # | Check                     | Rejection Reason                                  | Config Key / Threshold                 |
|---|---------------------------|---------------------------------------------------|----------------------------------------|
| 0 | Day rollover              | _(resets state, never rejects)_                   | Automatic at midnight                  |
| 1 | Halted?                   | `"HALTED: {reason}"`                              | Set by check #3 or manual halt         |
| 2 | Cooldown active?          | `"Cooldown active ({N}s left)"`                   | `COOLDOWN_AFTER_LOSSES`, `COOLDOWN_DURATION_S` |
| 3 | Daily loss limit          | `"Daily loss limit hit: {pnl}"`                   | `MAX_DAILY_LOSS_PCT` (default 2%)      |
| 4 | Max open positions        | `"Max open positions reached"`                    | `MAX_OPEN_POSITIONS` (default 20)      |
| 5 | Total exposure            | `"Total exposure limit reached"`                  | `MAX_TOTAL_EXPOSURE_PCT` (default 10%) |
| 6 | Latency budget            | `"Signal too stale ({N}ms)"`                      | `MAX_LATENCY_MS` (default 100ms)       |
| 7 | Compute position size     | `"Computed size <= 0"`                            | _(sizing formula result)_              |
| 8 | Clamp to remaining budget | _(size = min(size, remaining_exposure_budget))_   | _(derived from check #5)_             |

**Important:** Check #3 (daily loss) triggers a **permanent halt** for the rest of the trading day. The `halted` flag is only reset on the next calendar day.

### Latency Check Detail

The latency check compares the signal's creation timestamp against the current time:

```python
age_ms = (time.time_ns() - signal.timestamp_ns) / 1_000_000
if age_ms > self._exec_cfg.max_latency_ms:
    return False, 0.0, f"Signal too stale ({age_ms:.1f}ms)"
```

This ensures the bot does not act on stale signals that have been sitting in the queue.

---

## Position Sizing Formula

Defined in `RiskManager._compute_size()` (`src/risk_manager.py`).

```
position_size = base * regime_mult * strength_mult * confidence_mult
```

Where:

| Component         | Formula / Source                                | Default Example                 |
|-------------------|------------------------------------------------|----------------------------------|
| `base`            | `capital * max_position_pct`                   | `$10,000 * 0.005 = $50`         |
| `regime_mult`     | Depends on `signal.regime` (see table below)   | 0.4 (sideways) or 1.0 (trending)|
| `strength_mult`   | Depends on `signal.strength` (see table below) | 0.5 / 0.75 / 1.0                |
| `confidence_mult` | `signal.prediction.confidence`                 | 0.0 to 1.0                      |

**Full example** (default config, $10,000 capital):

```
Signal: STRONG strength, TRENDING_UP regime, 0.8 confidence

base         = 10000 * 0.005     = $50.00
regime_mult  = 1.0               (trending)
strength_mult = 1.0              (strong)
confidence   = 0.8

position_size = 50.0 * 1.0 * 1.0 * 0.8 = $40.00
```

```
Signal: WEAK strength, SIDEWAYS regime, 0.6 confidence

base         = 10000 * 0.005     = $50.00
regime_mult  = 0.4               (sideways)
strength_mult = 0.5              (weak)
confidence   = 0.6

position_size = 50.0 * 0.4 * 0.5 * 0.6 = $6.00
```

The computed size is floored at `0.0` (but never negative). After sizing, it is further clamped to the remaining exposure budget:

```python
remaining_budget = max_exposure - current_total_exposure
size = min(size, remaining_budget)
```

---

## Daily P&L Tracking

Defined in `DailyPnL` dataclass (`src/models.py`).

```python
@dataclass
class DailyPnL:
    date: str = ""
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    total_volume: float = 0.0
    max_drawdown: float = 0.0
    peak_pnl: float = 0.0
```

### Key Properties

| Property    | Calculation                                          |
|-------------|------------------------------------------------------|
| `win_rate`  | `winning_trades / total_trades` (0.0 if no trades)   |

### Trade Recording

Called by `RiskManager.record_close()`:

```python
def record_trade(self, pnl: float, volume: float) -> None:
    self.realized_pnl += pnl
    self.total_volume += volume
    self.total_trades += 1
    if pnl > 0:
        self.winning_trades += 1
    else:
        self.losing_trades += 1
    # Track peak and drawdown
    if self.realized_pnl > self.peak_pnl:
        self.peak_pnl = self.realized_pnl
    dd = self.peak_pnl - self.realized_pnl
    if dd > self.max_drawdown:
        self.max_drawdown = dd
```

### Day Reset

At the start of each `check_signal()` call, the manager checks if the date has changed:

```python
def _maybe_reset_day(self) -> None:
    today = str(date.today())
    if self._state.daily_pnl.date != today:
        # Log previous day stats, then reset
        self._state.daily_pnl = DailyPnL(date=today)
        self._state.halted = False
        self._state.halt_reason = ""
        self._state.consecutive_losses = 0
```

This means all daily limits, halts, and cooldowns reset at midnight local time.

---

## Cooldown Logic

**Trigger:** `COOLDOWN_AFTER_LOSSES` (default: 5) consecutive losing trades.

**Duration:** `COOLDOWN_DURATION_S` (default: 30 seconds).

**Implementation in `record_close()`:**

```python
if pnl < 0:
    self._state.consecutive_losses += 1
    if self._state.consecutive_losses >= self._cfg.cooldown_after_losses:
        self._state.cooldown_until = time.monotonic() + self._cfg.cooldown_duration_s
else:
    self._state.consecutive_losses = 0  # reset on any win
```

**Check in `check_signal()`:**

```python
if time.monotonic() < self._state.cooldown_until:
    remaining = self._state.cooldown_until - time.monotonic()
    return False, 0.0, f"Cooldown active ({remaining:.1f}s left)"
```

Key behavior:
- A single winning trade resets the consecutive loss counter to 0.
- The cooldown is based on `time.monotonic()`, not wall clock time.
- The consecutive loss counter is reset on day rollover.

---

## Daily Loss Halt

**Trigger:** Realized P&L drops below `-capital * MAX_DAILY_LOSS_PCT`.

**Default:** 2% of capital (e.g., -$200 on $10,000 capital).

```python
def _daily_loss_exceeded(self) -> bool:
    max_loss = self._state.capital * self._cfg.max_daily_loss_pct
    return self._state.daily_pnl.realized_pnl < -max_loss
```

When triggered:
1. `self._state.halted = True`
2. `self._state.halt_reason` is set to describe the loss amount.
3. The halt is logged at `CRITICAL` level.
4. All subsequent signals are rejected with `"HALTED: {reason}"` until the next calendar day.

---

## Regime Multipliers

Applied in `_compute_size()` based on the `signal.regime` field:

| Regime             | Multiplier | Config Key                  | Default |
|--------------------|------------|-----------------------------|---------|
| `SIDEWAYS`         | Reduced    | `SIDEWAYS_SIZE_MULTIPLIER`  | `0.4`   |
| `TRENDING_UP`      | Full       | `TREND_SIZE_MULTIPLIER`     | `1.0`   |
| `TRENDING_DOWN`    | Full       | `TREND_SIZE_MULTIPLIER`     | `1.0`   |

```python
if signal.regime == MarketRegime.SIDEWAYS:
    regime_mult = self._cfg.sideways_size_multiplier  # 0.4
else:
    regime_mult = self._cfg.trend_size_multiplier     # 1.0
```

Both `TRENDING_UP` and `TRENDING_DOWN` use the same trend multiplier. To differentiate, modify the `_compute_size()` method.

---

## Strength Multipliers

Applied in `_compute_size()` based on the `signal.strength` field:

| Strength   | Multiplier | Edge Range (with default threshold)  |
|------------|------------|--------------------------------------|
| `WEAK`     | `0.5`      | `0.02 <= edge < 0.03`               |
| `MODERATE` | `0.75`     | `0.03 <= edge < 0.05`               |
| `STRONG`   | `1.0`      | `0.05 <= edge < 0.30`               |

Edge values are in **probability space** (not return space). A `WEAK` signal means a 2–3% probability advantage over the market. A `STRONG` signal means a 5%+ advantage, which is common near market expiry as the CDF sharpens.

```python
strength_map = {
    SignalStrength.WEAK: 0.5,
    SignalStrength.MODERATE: 0.75,
    SignalStrength.STRONG: 1.0,
}
strength_mult = strength_map.get(signal.strength, 0.5)
```

These multipliers are **hardcoded** in `_compute_size()`. To make them configurable, add fields to `RiskConfig` and reference them here.

---

## Risk State

The `RiskState` dataclass holds all mutable risk state:

```python
@dataclass
class RiskState:
    capital: float = 0.0
    open_positions: dict[str, Position] = field(default_factory=dict)
    daily_pnl: DailyPnL = field(default_factory=DailyPnL)
    consecutive_losses: int = 0
    cooldown_until: float = 0.0       # time.monotonic() value
    halted: bool = False
    halt_reason: str = ""
```

`open_positions` is keyed by `order_id`. Each position is uniquely identified by its order ID, allowing multiple positions on the same token. Positions are added via `record_fill()` and removed via `record_close()`.

### Total Exposure Calculation

```python
total_exposure = sum(position.size for position in open_positions.values())
max_exposure = capital * MAX_TOTAL_EXPOSURE_PCT  # default 10%
```

---

## Configuration Reference

All risk-related environment variables:

| Variable                    | Type    | Default  | Description                                                |
|-----------------------------|---------|----------|------------------------------------------------------------|
| `MAX_POSITION_PCT`          | `float` | `0.005`  | Max fraction of capital per single trade (0.5%)            |
| `MAX_DAILY_LOSS_PCT`        | `float` | `0.02`   | Max daily drawdown before halting (2%)                     |
| `MAX_OPEN_POSITIONS`        | `int`   | `20`     | Max concurrent open positions                              |
| `MAX_TOTAL_EXPOSURE_PCT`    | `float` | `0.10`   | Max total exposure as fraction of capital (10%)            |
| `COOLDOWN_AFTER_LOSSES`     | `int`   | `5`      | Consecutive losses before cooldown triggers                |
| `COOLDOWN_DURATION_S`       | `float` | `30.0`   | Cooldown pause duration in seconds                         |
| `SIDEWAYS_SIZE_MULTIPLIER`  | `float` | `0.4`    | Position size multiplier in sideways regime                |
| `TREND_SIZE_MULTIPLIER`     | `float` | `1.0`    | Position size multiplier in trending regime                |
| `MAX_LATENCY_MS`            | `int`   | `100`    | Max allowed signal age in milliseconds (from ExecutionConfig)|
