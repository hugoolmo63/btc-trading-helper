---
title: "Recurring DCA Backtest and Dashboard Diagnostics"
status: accepted
date: 2026-11-16
authors: [@hugoolmo63]
reviewers: [@Copilot]
tags: [architecture, decision, backtesting]
related: [[adr-0001-onchain-data-staleness-and-source-migration]], [[adr-0002-profile-b-hysteresis-allocation-floor-and-yield]], [[adr-0003-fifo-capital-gains-tax-accounting]]
supersedes:
---

# ADR: Recurring DCA Backtest and Dashboard Diagnostics

## Status
accepted

## Context

The previous out-of-sample comparison emphasized lump-sum behavior, while the
product requirement is weekly recurring investment. The dashboard also lacked
drawdown and attribution visibility.

## Decision

Run `simulate_recurring_profiles()` with `$100` deposits at Sunday 00:00 UTC
across every purged 2020–2026 walk-forward fold. Expose results through
`GET /api/backtest/recurring`.

The frontend displays overlapping underwater drawdown curves and an
attribution chart decomposing Profile B's relative performance into market
timing alpha, USDT yield, trading fees, and FIFO taxes.

## Consequences

### Positive

- Evaluation matches the intended recurring-investor cash-flow pattern.
- Relative performance drivers are visible instead of hidden in equity curves.
- Risk reporting includes CAGR, Sharpe ratio, and maximum drawdown.

### Negative

- Recurring simulation is more computationally expensive than lump-sum
  evaluation.
- Attribution depends on consistent fee, yield, and tax bookkeeping.
- Dashboard data may be unavailable when a full backtest has not completed.

## Alternatives Considered

### Alternative 1

Keep only lump-sum backtesting. It is faster but does not represent weekly DCA
investors, so it was rejected.

### Alternative 2

Display only aggregate return and ROI. This is simpler but hides drawdown and
the causes of Profile B's spread, so it was rejected.

## Implementation Notes

For each profile, weekly contribution is added at the first matching closed
bar. Drawdown is:

\[
D_t=E_t/\max_{s\leq t}(E_s)-1.
\]

The API samples long curves for browser performance while preserving the full
calculation server-side. The dashboard handles empty or unavailable results
without crashing.

## References

- [[adr-0002-profile-b-hysteresis-allocation-floor-and-yield]]
- [[adr-0003-fifo-capital-gains-tax-accounting]]
- [Plotly JavaScript documentation](https://plotly.com/javascript/)
