---
title: "FIFO Capital-Gains Tax Accounting"
status: accepted
date: 2026-11-16
authors: [@hugoolmo63]
reviewers: [@Copilot]
tags: [architecture, decision, accounting]
related: [[adr-0002-profile-b-hysteresis-allocation-floor-and-yield]], [[adr-0004-recurring-dca-backtest-and-dashboard-diagnostics]]
supersedes:
---

# ADR: FIFO Capital-Gains Tax Accounting

## Status
accepted

## Context

Profile A defers taxes by holding BTC, while Profile B creates taxable events
on sales. Comparing gross balances therefore overstates Profile B's spendable
return.

## Decision

Live paper trading and recurring backtests use deterministic FIFO BTC tax lots.
Live lots are persisted in SQLite in the `tax_lots` table. On every sale, the
oldest lots are consumed first.

Positive realized gains are taxed at `TAX_RATE = 0.20`, configurable through the
`TAX_RATE` environment variable. Losses receive no tax credit.

## Consequences

### Positive

- Profile comparisons use after-tax spendable proceeds.
- Tax calculations are deterministic across restarts and backtest runs.
- Partial lot consumption preserves original cost basis.

### Negative

- The ledger schema and trade records are more complex.
- Tax liabilities can materially reduce Profile B returns.
- Tax jurisdiction-specific rules beyond FIFO are not modeled.

## Alternatives Considered

### Alternative 1

Ignore taxes and compare gross equity. This is easy to implement but creates an
unrealistic advantage for an active seller, so it was rejected.

### Alternative 2

Use average-cost accounting. It is simpler than FIFO but does not satisfy the
deterministic lot-order requirement, so it was rejected.

## Implementation Notes

Each lot is `(acquired_at, quantity, unit_cost)`. For a sale of quantity \(q\)
at price \(P\), consume lots in acquisition order:

\[
G=\sum_i q_i(P-c_i), \qquad
\text{tax}=\max(0,G)\cdot 0.20.
\]

Net proceeds equal gross proceeds minus the trading fee and tax. Partial lots
remain with their original unit cost. Tax totals are recorded per trade and
aggregated in recurring-profile metrics.

## References

- [[adr-0002-profile-b-hysteresis-allocation-floor-and-yield]]
- [IRS Publication 550](https://www.irs.gov/publications/p550)
