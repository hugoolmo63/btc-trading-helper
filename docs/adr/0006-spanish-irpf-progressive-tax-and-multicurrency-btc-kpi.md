---
title: "Spanish IRPF Progressive Tax and Multi-Currency BTC KPIs"
status: accepted
date: 2026-11-16
authors: [@hugoolmo63]
reviewers: [@Copilot]
tags: [arquitectura, decision, impuestos, multicurrency, kpi]
related: [[adr-0003-fifo-capital-gains-tax-accounting]], [[adr-0005-tax-aware-asymmetric-rebalancing]], [[adr-0011-tax-reporting-and-withdrawal-simulation]]
supersedes:
---

# ADR: Spanish IRPF Progressive Tax and Multi-Currency BTC KPIs

## Status
accepted

## Context

Profile B is intended for paper-trading validation under Spanish tax
jurisdiction. Every BTC disposal into EUR or stablecoins is a FIFO-taxable
event under progressive Base del Ahorro brackets. The primary investment
objective is accumulating more net BTC than Profile A.

## Decision

Apply annual year-to-date Spanish IRPF savings-base brackets to realized gains
in EUR:

- EUR 0–6,000: 19%
- EUR 6,000.01–50,000: 21%
- EUR 50,000.01–200,000: 23%
- EUR 200,000.01–300,000: 27%
- EUR 300,000–500,000: 28%
- Above EUR 500,000: 30%

ADR 0011 adds the EUR 300,000–500,000 marginal band at 28% and specifies the
tax reporting and read-only withdrawal simulation APIs.

Same-year losses may offset same-year gains. Accrued tax is ring-fenced in a
dedicated `tax_escrow_eur` balance and can never be reinvested.

The ledger and backtest maintain BTC, EUR, and USD valuation. They report:

`net_btc_equivalent = btc_balance + spendable_cash_usd / btc_price_usd`

where spendable cash excludes tax escrow.

## Consequences

### Positive

- Reports after-tax wealth in the owner's tax jurisdiction.
- Makes net BTC accumulation the primary cross-profile comparison.
- Preserves currency-specific balances and auditable tax escrow.

### Negative

- Requires FX data and historical valuation maintenance.
- Progressive annual accounting is more complex than a flat tax rate.
- FX and BTC price source outages can temporarily prevent complete valuation.

## Alternatives Considered

### Alternative 1

Use a flat 20% tax rate. This is easy to implement but does not represent the
specified Spanish progressive brackets, so it was rejected.

### Alternative 2

Report only USD equity. This hides EUR tax liability and net BTC accumulation,
so it was rejected.

## Implementation Notes

Every disposal records EUR proceeds, EUR cost basis, realized gain/loss, tax,
and calendar year. Tax escrow is excluded from all purchase and deployment
calculations. FX rates must be UTC timestamped and sourced from FRED `DEXUSEU`
or a public EURUSDT market.

The API remains language-agnostic and exposes canonical numeric fields:
`btc_balance`, `net_btc_equivalent`, `equity_eur`, `equity_usd`, and
`tax_escrow_eur`.

## References

- [[adr-0003-fifo-capital-gains-tax-accounting]]
- [Agencia Tributaria: Savings income tax](https://sede.agenciatributaria.gob.es/)
