---
title: "On-Chain Data Staleness and Source Migration"
status: accepted
date: 2026-11-16
authors: [@hugoolmo63]
reviewers: [@Copilot]
tags: [architecture, decision, data-quality]
related: [[adr-0002-profile-b-hysteresis-allocation-floor-and-yield]], [[adr-0004-recurring-dca-backtest-and-dashboard-diagnostics]]
supersedes:
---

# ADR: On-Chain Data Staleness and Source Migration

## Status
accepted

## Context

The public Coin Metrics CSV stopped updating in May 2026. A backward
`join_asof` with a long tolerance could silently forward-fill stale MVRV and
network metrics into live inference. This violates the system's data-freshness
and live-signal reliability requirements.

## Decision

Daily on-chain joins use a maximum `7d` (`168h`) tolerance after the mandatory
24-hour publication shift. Missing or stale values remain null and are handled
by train-only median imputation.

The ingestion layer supplements the historical Coin Metrics archive with an
active free/public provider adapter, while retaining Coin Metrics as historical
fallback.

## Consequences

### Positive

- Live inference cannot silently use on-chain values older than seven days.
- Historical coverage remains available when the active provider is unavailable.
- Provider data is normalized to the existing on-chain schema.

### Negative

- Recent feature rows may contain nulls during provider outages.
- Train-only median imputation can reduce signal quality during extended gaps.
- Multiple providers require normalization and monitoring.

## Alternatives Considered

### Alternative 1

Continue using Coin Metrics with a 14-day tolerance. This preserves current
behavior but allows stale features to remain active, so it was rejected.

### Alternative 2

Fail the entire pipeline whenever the active provider is unavailable. This
maximizes freshness guarantees but prevents resilient paper-trading refreshes,
so it was rejected in favor of null propagation and explicit imputation.

## Implementation Notes

For market timestamp \(t\), a daily observation at source timestamp \(d\) is
eligible only when:

\[
0 \leq t - (d + 24h) \leq 7d.
\]

Provider adapters normalize timestamps to timezone-aware UTC and output
`CapMVRVCur`, `CapRealUSD`, `PriceUSD`, `AdrActCnt`, `HashRate`, `FeeTotUSD`,
and `TxCnt`.

## References

- [[adr-0004-recurring-dca-backtest-and-dashboard-diagnostics]]
- [Coin Metrics community data](https://github.com/coinmetrics/data)
- [Bitcoin Data API](https://bitcoin-data.com/)
