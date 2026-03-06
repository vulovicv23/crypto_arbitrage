# Production Checklist

Status as of 2026-03-06. The 15m regression model (v6) shows +12.8% return / 63% WR over 233 dry-run trades in ~7 hours — promising but unvalidated.

---

## Must-Have (before real money)

- [ ] **Validate the 15m edge is real**
  - [ ] Complete the current 12h dry-run matrix
  - [ ] Run backtest on historical data with the 15m model (`tools/backtest.py`)
  - [ ] Run 2–3 more multi-day dry runs across different market conditions
  - [ ] Confirm edge persists across trending-up, trending-down, and sideways regimes

- [ ] **Fix libgomp dependency** — currently extracted to `/tmp` which gets wiped on reboot. Need `sudo apt install libgomp1` or bake it into the system.

- [ ] **Process management** — replace `nohup` with systemd service or supervisor. Bot must auto-restart on crash/reboot.

- [ ] **Real fill dynamics** — dry-run simulates instant fills at order price. Quantify expected slippage, partial fill rates, and order rejection rates on live Polymarket CLOB.

- [ ] **Fee impact analysis** — calculate whether the edge survives after taker fees (2% on profit). Current dry-run PnL does not account for fees.

- [ ] **Funded wallet** — USDC on Polygon (chain ID 137), API keys configured in `.env`, token allowances approved on Polymarket contracts.

---

## Should-Have (before scaling up)

- [ ] **Monitoring and alerting** — get notified (email, Telegram, Slack) if the bot halts, loses connectivity, or hits daily loss limit.

- [ ] **Graceful state persistence** — if the bot restarts, open positions aren't tracked (orphaned on Polymarket). Need to reconcile positions on startup via the CLOB API.

- [ ] **Out-of-sample backtest** — run the 15m model on held-out historical data to estimate realistic Sharpe ratio, max drawdown, and expected daily PnL.

---

## Nice-to-Have (before full capital deployment)

- [ ] **Extended paper trading** — dry-run against live order books for 7+ days to observe behavior across weekends, low-liquidity periods, and news events.

- [ ] **Gradual capital ramp** — start live with minimal capital ($100–500), validate real fills match dry-run expectations, then scale up incrementally.
