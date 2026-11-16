---
title: "Tax-Aware Asymmetric Rebalancing for Profile B"
status: accepted
date: 2026-11-16
authors: [@hugoolmo63]
reviewers: [@Copilot]
tags: [arquitectura, decision, trading, impuestos]
related: [[adr-0003-fifo-capital-gains-tax-accounting]], [[adr-0006-spanish-irpf-progressive-tax-and-multicurrency-btc-kpi]]
supersedes:
---

# ADR: Tax-Aware Asymmetric Rebalancing for Profile B

## Status
accepted

## Context

Frequent mid-cycle sales create severe FIFO tax drag. Profile B can therefore
produce higher gross returns and lower drawdowns while still producing a lower
net CAGR than the DCA Holder.

## Decision

Profile B becomes a tax-aware asymmetric strategy:

- Increase `COOLDOWN_HOURS` from 24 to 168, aligned with the Sunday 00:00 UTC
  deposit cadence.
- For `0.30 <= prob_up < 0.60`, route all new weekly deposits to the cash
  reserve without selling existing BTC.
- For `prob_up >= 0.60`, deploy the entire new weekly deposit and all available
  spendable cash reserve into BTC.
- For `prob_up < 0.30` and unrealized gain above 40%, sell BTC down to the 20%
  BTC floor.

## Consequences

### Positive

- Reduces unnecessary taxable disposals.
- Preserves BTC exposure through neutral regimes.
- Concentrates deployment and distribution decisions at economically meaningful
  cycle thresholds.

### Negative

- Cash may remain undeployed during neutral regimes.
- Weekly cadence can delay reactions to fast market changes.
- The 20% floor still retains downside exposure.

## Alternatives Considered

### Alternative 1

Continue hourly or daily rebalancing with symmetric buy and sell thresholds.
This is simpler but creates tax churn and was rejected.

### Alternative 2

Use fully continuous probability-proportional sizing. This reduces discrete
steps but still causes frequent taxable sales and was rejected.

## Implementation Notes

The execution and backtest paths must share the same cooldown, deposit-routing,
cash-deployment, and unrealized-gain rules. Tax escrow is never considered
spendable cash.

## References

- [[adr-0003-fifo-capital-gains-tax-accounting]]
- [[adr-0006-spanish-irpf-progressive-tax-and-multicurrency-btc-kpi]]
