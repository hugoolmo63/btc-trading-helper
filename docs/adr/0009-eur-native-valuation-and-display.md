---
title: "EUR-Native Valuation and Display"
status: accepted
date: 2026-11-16
authors: [@hugoolmo63]
reviewers: [@Copilot]
tags: [architecture, decision, valuation, currency, frontend]
related: [[adr-0006-spanish-irpf-progressive-tax-and-multicurrency-btc-kpi]], [[adr-0007-bilingual-i18n-dashboard-es-en]]
supersedes:
---

# ADR: EUR-Native Valuation and Display

## Status
accepted

## Context

The paper-trading system was displaying USD and stablecoin-denominated values
while the intended reporting currency is EUR. This made deposits, fees, cash
reserves, BTC prices, and dashboard comparisons inconsistent with the Spanish
tax and valuation requirements.

## Decision

Use EUR as the sole fiat reporting and deposit currency and BTC as the asset
unit. Convert EUR deposits to the execution cash unit using a live public
EURUSDT rate, falling back only to the latest non-null DEXUSEU value in the
processed macro dataset. Never use `1.0` as an FX fallback.

Expose EUR-primary API fields such as `contributed_eur`, `equity_eur`,
`alpha_eur`, `fees_paid_eur`, `cash_yield_eur`, and `btc_price_eur`.
The frontend displays only EUR and BTC, with eight decimals for BTC holdings
and six decimals for net BTC equivalent.

## Consequences

### Positive

- Deposits, valuations, fees, taxes, and charts share one fiat reporting unit.
- BTC accumulation and net BTC equivalent remain directly comparable.
- FX failures are explicit rather than silently producing incorrect values.

### Negative

- Historical execution cash remains stored in legacy SQLite columns for
  compatibility and is converted at API boundaries.
- Live FX availability is an operational dependency for EUR valuation.

## Alternatives Considered

### Alternative 1

Keep USD as the accounting currency and translate only in the frontend.
This was rejected because deposits, tax values, and backtest metrics would
remain semantically inconsistent.

### Alternative 2

Use a fixed EUR/USD conversion rate. This was rejected because it introduces
valuation drift and violates live multi-currency reporting requirements.

## Implementation Notes

Recurring deposits are `100 EUR` every Sunday at 00:00 UTC. Binance public
EURUSDT spot data is preferred, with the latest non-null processed DEXUSEU
observation as fallback. Existing seeded ledger balances are rebased to EUR
semantics after an online SQLite backup.

## References

- [[adr-0006-spanish-irpf-progressive-tax-and-multicurrency-btc-kpi]]
- [[adr-0007-bilingual-i18n-dashboard-es-en]]
- [FRED DEXUSEU](https://fred.stlouisfed.org/series/DEXUSEU)
