---
title: "Configurable Recurring DCA Schedule"
status: accepted
date: 2026-11-16
authors: [@hugoolmo63]
reviewers: [@Copilot]
tags: [architecture, decision, recurring-investment, execution]
related: [[adr-0002-profile-b-hysteresis-allocation-floor-and-yield]], [[adr-0003-fifo-capital-gains-tax-accounting]], [[adr-0006-spanish-irpf-progressive-tax-and-multicurrency-btc-kpi]], [[adr-0009-eur-native-valuation-and-display]]
supersedes:
---

# ADR: Configurable Recurring DCA Schedule

## Status

accepted

## Context

The live ledger supported only a fixed weekly contribution, while users need to
inspect and control their recurring EUR schedule without weakening the shared
portfolio, fee, FIFO, Spanish tax, or Profile B allocation invariants.

## Decision

Persist one recurring schedule in SQLite with enabled state, weekly or monthly
frequency, weekday or day-of-month, EUR amount, and a last-executed ISO period
key. A schedule fires at midnight on its configured day in the
`Europe/Madrid` calendar and at most once per ISO week or calendar month. The
deposit and period-key update are committed in the same transaction as both
profile updates. Scheduled contributions use the same engine deposit accounting
as manual contributions and preserve existing execution fees, FIFO lots, tax
escrow, cash yield, and the Profile B 20% BTC allocation floor.

The language-neutral REST API exposes canonical schedule fields and both UTC
and Madrid next-run timestamps. Dashboard localization remains client-side.

## Consequences

- Schedule edits survive process restarts and are auditable in the existing
  ledger database.
- Repeated hourly evaluations cannot duplicate a contribution within a period.
- Madrid local midnight follows daylight-saving transitions; UTC run times may
  therefore differ seasonally.
- The existing fixed-frequency recurring backtest remains a separate
  diagnostic and is not a live schedule simulator.

## References

- [[adr-0002-profile-b-hysteresis-allocation-floor-and-yield]]
- [[adr-0003-fifo-capital-gains-tax-accounting]]
- [[adr-0006-spanish-irpf-progressive-tax-and-multicurrency-btc-kpi]]
- [[adr-0009-eur-native-valuation-and-display]]