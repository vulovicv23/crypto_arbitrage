# Pivot Research — Post-Mortem & Path Forward

## Date: 2026-02-28

## What We Tested

Ran 18+ hours of matrix testing (6h + 12h) across 8 strategy profiles with the
corrected edge computation (edge vs execution price, not mid-price). Used **real
Polymarket WebSocket data** to ensure dry-run matches production.

## Results

| Metric | Value |
|--------|-------|
| Total evaluations | ~200,000 per profile |
| Markets discovered | ~3,000+ (rotating 5m/15m BTC Up/Down) |
| Time covered | 1:35 AM → 1:35 PM ET (overnight + full US morning) |
| Unique spread observed | **0.98 (100% of books)** |
| Signals generated | **0** |
| Trades executed | **0** |

Every single Polymarket BTC 5m/15m Up/Down market had identical books:

```
bid = 0.01    ask = 0.99    mid = 0.50    spread = 0.98 (98%)
```

No market maker is quoting these contracts. The books are placeholder liquidity.

## Why No Signals Were Possible

With correct edge computation (`edge = fair_value - best_ask`):

| Scenario | P(up) | Fair Value | Ask | Edge | Tradeable? |
|----------|-------|------------|-----|------|------------|
| Max confidence | 0.99 | 0.99 | 0.99 | 0.00 | No |
| Moderate | 0.60 | 0.60 | 0.99 | -0.39 | No |
| Uncertain | 0.50 | 0.50 | 0.99 | -0.49 | No |

Best edge achieved: exactly 0.0000. Never once positive.

## Root Cause

The latency-arbitrage strategy requires:
1. A market maker quoting real prices (e.g., bid=0.45, ask=0.55)
2. The bot detects when those prices are stale vs real BTC movement
3. The bot buys underpriced contracts before the market maker adjusts

**There is no market maker.** The strategy is correct but the market doesn't exist.

## Fixes That Were Validated

The edge computation bug (computing against mid instead of execution price) is now
fixed and confirmed working. The bot correctly refuses to trade illiquid markets.
These fixes are valuable for any future strategy:

1. ✅ Edge computed against `best_ask` (BUY) not `mid_price`
2. ✅ Max spread filter (`MAX_SPREAD` config)
3. ✅ Maker mode for limit orders inside spread
4. ✅ Dry-run uses real Polymarket WS data (identical to production)

## Infrastructure Health

| Component | Status |
|-----------|--------|
| Binance WS | ✅ Healthy (sub-second latency) |
| CryptoCompare WS | ⚠️ Reconnect thrashing (rate limit with 8 bots) |
| CoinGecko REST | ⚠️ Rate limited on free tier |
| Polymarket WS | ✅ Healthy (640+ tokens) |
| Market Discovery | ✅ Healthy (3,000+ markets discovered) |
| Matrix Test Framework | ✅ Fully functional |

---

## Three Paths Forward

### Path 1: Liquidity Scout — Do ANY Polymarket Markets Have Liquidity?
**Effort: 1 hour**

Before any pivot, scan ALL Polymarket markets (not just BTC 5m/15m) for real
order books. Check:
- Longer BTC durations (1h, 4h, daily)
- Other crypto assets (ETH, SOL)
- Non-crypto markets (elections, sports, events)
- Top markets by volume

**Outcome:** Map of which Polymarket markets are tradeable.

### Path 2: Flip to Market Maker
**Effort: 1-2 weeks**

Empty books = opportunity. We become the liquidity provider:
- Quote both sides: YES bid/ask + NO bid/ask
- Capture spread when retail traders come in
- Binary markets: YES + NO = $1.00 → accumulate both sides below $1 = guaranteed profit
- Use BTC prediction to center quotes with informational edge

**What transfers:** Price feeds, prediction model, Polymarket API, risk management.
**New pieces:** Two-sided quoting, inventory tracking, quote skewing, delta hedging.

**Key question:** Is there any retail flow on these markets? If nobody trades even
with quotes available, market making won't work either.

### Path 3: ML Prediction Engine → Apply Anywhere
**Effort: 2-3 weeks**

Build the ML pipeline from `ML_PLAN.md`, decouple from Polymarket. A high-quality
BTC direction predictor works on any venue: Polymarket, Kalshi, Deribit, funding
rate arb. Build the brain first, plug into whatever market has liquidity.

## Decision Framework

1. **Run Path 1 first** (1 hour) — determines if Polymarket is viable at all
2. If liquidity exists somewhere → adapt strategy for those markets
3. If nothing has liquidity → Path 2 (market making) to fill the gap
4. If Polymarket is dead entirely → Path 3 (ML for other venues)

## Reusable Assets

Regardless of pivot direction, we keep:
- Async pipeline architecture (queues, back-pressure, health monitoring)
- Binance real-time price feed
- CryptoCompare CCCAGG feed
- Risk management framework
- Matrix test framework for strategy comparison
- PostgreSQL infrastructure
- ML plan (ready for implementation)
