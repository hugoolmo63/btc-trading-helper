---
title: "Progressive IRPF Reporting and Read-Only Withdrawal Simulation"
status: accepted
date: 2026-11-17
authors: [@hugoolmo63]
reviewers: [@Copilot]
tags: [architecture, decision, tax, liquidity, reporting]
related: [[adr-0003-fifo-capital-gains-tax-accounting]], [[adr-0006-spanish-irpf-progressive-tax-and-multicurrency-btc-kpi]]
supersedes:
---

# ADR: Progressive IRPF Reporting and Read-Only Withdrawal Simulation

## Status

accepted

## Context

Users need an auditable EUR fiscal snapshot and an estimate of realizable EUR
cash after FIFO disposals, commissions, and progressive savings-base tax. The
previous tax helper did not expose bracket headroom, and the trade ledger did
not retain per-lot disposal matches for an export report.

## Decision

Use annual progressive Spanish IRPF *Base del Ahorro* rates on net realized EUR
gains, allowing same-year loss offsets:

| Annual taxable base | Marginal rate |
| --- | ---: |
| €0–€6,000 | 19% |
| €6,000–€50,000 | 21% |
| €50,000–€200,000 | 23% |
| €200,000–€300,000 | 27% |
| €300,000–€500,000 | 28% |
| Above €500,000 | 30% |

Persist each realized sale's FIFO lot matches as EUR acquisition basis, disposal
proceeds, fee, realized gain, and applicable marginal bracket. Provide profile
scoped fiscal summaries and CSV exports. The withdrawal what-if endpoint reads
the current ledger and calculates cash-first withdrawal proceeds, fee, FIFO
matches, and incremental tax in memory only; it must never modify SQLite.
Tax escrow remains ring-fenced and is not spendable cash.

## Consequences

- Reports can reproduce realized disposals lot-by-lot and calculate current
  annual progressive liability and marginal bracket headroom.
- A schema migration adds a durable FIFO disposal-match table.
- A simulation result is an estimate at the current stored EUR/BTC valuation;
  it does not reserve funds or perform a real transaction.
- The rate table is a project assumption and must be checked against current
  Agencia Tributaria rules for the relevant taxpayer and tax year.

## References

- [[adr-0003-fifo-capital-gains-tax-accounting]]
- [[adr-0006-spanish-irpf-progressive-tax-and-multicurrency-btc-kpi]]
- [Agencia Tributaria](https://sede.agenciatributaria.gob.es/)