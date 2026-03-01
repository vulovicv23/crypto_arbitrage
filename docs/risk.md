# Risk Management

Source: `src/risk_manager.py`

Stateful risk gate that approves or rejects signals and computes position sizes. Enforces per-trade limits, daily loss limits, exposure caps, and regime-adaptive sizing.

## Class: RiskManager

### Constructor

```python
RiskManager(
    risk_config: RiskConfig,
    exec_config: ExecutionConfig,
    initial_capital: float,
)
```

### Risk Check Pipeline

`check_signal(signal)` returns `(approved: bool, position_size: float, reason: str)`.

Checks are evaluated in order — first failure short-circuits:

| # | Check | Failure Reason |
|---|-------|----------------|
| 1 | Day reset | (resets daily P&L if new trading day) |
| 2 | Halt state | `HALTED: {reason}` |
| 3 | Cooldown active | `Cooldown active ({X}s left)` |
| 4 | Daily loss limit | `Daily loss limit hit: {pnl}` — also sets halt |
| 5 | Max open positions | `Max open positions reached` |
| 6 | Total exposure limit | `Total exposure limit reached` |
| 7 | Latency budget | `Signal too stale ({X}ms)` |
| 8 | Position size > 0 | `Computed size <= 0` |
| 9 | Remaining budget | Caps size to remaining exposure headroom |

### Position Sizing

```
size = base * regime_multiplier * strength_multiplier * confidence_multiplier
```

| Factor | Formula |
|--------|---------|
| `base` | `capital * max_position_pct` (default: 0.5% = $50 on $10k) |
| `regime_multiplier` | SIDEWAYS: 0.4x, TRENDING: 1.0x |
| `strength_multiplier` | WEAK: 0.5x, MODERATE: 0.75x, STRONG: 1.0x |
| `confidence_multiplier` | prediction confidence (0.0–1.0) |

### Risk Parameters

| Parameter | Env Var | Default | Description |
|-----------|---------|---------|-------------|
| `max_position_pct` | `MAX_POSITION_PCT` | 0.005 (0.5%) | Max capital per trade |
| `max_daily_loss_pct` | `MAX_DAILY_LOSS_PCT` | 0.02 (2%) | Daily drawdown halt |
| `max_open_positions` | `MAX_OPEN_POSITIONS` | 20 | Max concurrent positions |
| `max_total_exposure_pct` | `MAX_TOTAL_EXPOSURE_PCT` | 0.10 (10%) | Max total exposure |
| `cooldown_after_losses` | `COOLDOWN_AFTER_LOSSES` | 5 | Consecutive losses before cooldown |
| `cooldown_duration_s` | `COOLDOWN_DURATION_S` | 30.0 | Cooldown duration (seconds) |
| `sideways_size_multiplier` | `SIDEWAYS_SIZE_MULTIPLIER` | 0.4 | Size reduction in sideways markets |
| `trend_size_multiplier` | `TREND_SIZE_MULTIPLIER` | 1.0 | Size multiplier in trending markets |

### State Management

**RiskState** tracks:
- `capital` — Starting capital
- `open_positions` — Dict of `token_id → Position`
- `daily_pnl` — `DailyPnL` tracker
- `consecutive_losses` — Loss streak counter
- `cooldown_until` — Monotonic time cooldown expires
- `halted` / `halt_reason` — Trading halt state

### Cooldown Logic

After `cooldown_after_losses` consecutive losing trades, a cooldown is triggered for `cooldown_duration_s` seconds. All signals are rejected during cooldown. A winning trade resets the streak.

### Daily Reset

At the start of each new trading day:
- Previous day's P&L is logged
- `DailyPnL` resets
- Halt state clears
- Consecutive loss counter resets

### Public Methods

| Method | Description |
|--------|-------------|
| `check_signal(signal)` | Evaluate signal against all risk checks |
| `record_fill(position)` | Register a new open position |
| `record_close(token_id, pnl, volume)` | Record a closed trade, update P&L |

### Key Properties

- `state` — Full `RiskState`
- `is_halted` — Whether trading is halted
- `daily_pnl` — Current day's `DailyPnL`
