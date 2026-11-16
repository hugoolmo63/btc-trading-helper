---
title: "Profile B Hysteresis, Allocation Floor, and Yield"
status: accepted
date: 2026-11-16
authors: [@hugoolmo63]
reviewers: [@Copilot]
tags: [architecture, decision, portfolio-management]
related: [[adr-0003-fifo-capital-gains-tax-accounting]], [[adr-0004-recurring-dca-backtest-and-dashboard-diagnostics]]
supersedes:
---

# ADR: Profile B Hysteresis, Allocation Floor, and Yield

## Status
accepted

## Context

The previous all-or-nothing allocation rule sold Profile B to 0% BTC below a
single threshold. Small probability changes around thresholds caused
transaction-cost whipsaw, missed sharp rebounds, and left USDT idle at zero
yield.

## Decision

Profile B uses a stateful hysteresis allocator with a 20% minimum BTC floor.
It enters 100% BTC at `prob_up >= 0.65`, steps down to 50% only below `0.55`,
and steps down to 20% only below `0.40`. A 20% position returns to 50% only at
`prob_up >= 0.48`; a 50% position returns to 100% only at `prob_up >= 0.65`.

USDT earns `RISK_FREE_APY = 0.045`, compounded hourly.

## Consequences

### Positive

- Threshold noise produces fewer unnecessary trades.
- The strategy retains exposure during bearish regimes and rebounds.
- Idle USDT earns deterministic risk-free yield.

### Negative

- The 20% floor creates unavoidable downside exposure.
- Stateful allocation requires persisted target state.
- Hysteresis makes signals less immediately responsive.

## Alternatives Considered

### Alternative 1

Keep the original 0% / 50% / 100% step function. It is simpler but creates
whipsaw and zero-yield cash, so it was rejected.

### Alternative 2

Use continuous probability-proportional sizing. It reduces discontinuities but
still trades on every small probability move and does not provide explicit
behavioral guarantees, so it was rejected.

## Implementation Notes

Let \(w_{t-1}\) be the previous target state and \(p_t\) the model probability:

| Previous state | Transition condition | New state |
|---|---:|---:|
| any | \(p_t \geq 0.65\) | 1.00 |
| 1.00 | \(p_t < 0.55\) | 0.50 |
| 0.50 | \(p_t < 0.40\) | 0.20 |
| 0.20 | \(p_t \geq 0.48\) | 0.50 |

Otherwise the state is unchanged and \(w_t \geq 0.20\). Hourly USDT accrual is:

\[
B_{t+1}=B_t(1+0.045)^{1/(365.25\cdot24)}.
\]

The live trader and walk-forward simulator share the same allocator.

## References

- [[adr-0003-fifo-capital-gains-tax-accounting]]
- [Federal Reserve risk-free rate data](https://fred.stlouisfed.org/)
