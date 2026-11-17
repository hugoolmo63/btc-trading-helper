---
title: "Live ML Signal Explainability and Action-Zone Context"
status: accepted
date: 2026-11-17
authors: [@hugoolmo63]
reviewers: [@Copilot]
tags: [architecture, decision, model, explainability, dashboard]
related: [[adr-0002-profile-b-hysteresis-allocation-floor-and-yield]], [[adr-0005-tax-aware-asymmetric-rebalancing]]
supersedes:
---

# ADR: Live ML Signal Explainability and Action-Zone Context

## Status

accepted

## Context

The dashboard displays the current LightGBM probability for a BTC return above
10% over 30 days, but does not show which current features influence the
prediction or how it relates to the Profile B deposit/active-allocation zones.
This is difficult to audit and can lead users to confuse signal probability,
execution thresholds, and portfolio advice.

## Decision

- Explain only the latest closed-candle prediction and expose a canonical,
  language-independent `signal_explanation` object from the ledger API.
- The explanation action zones are `<30%` cash preservation, `30%.. <60%`
  smart DCA, and `>=60%` full deployment. They describe the existing strategy
  decision context; they do not introduce or modify trading rules.
- Prefer LightGBM `pred_contrib=True` local raw-margin contributions. Convert
  margins to additive probability percentage-point estimates with the common
  secant slope from base probability to current probability. If contributions
  are unavailable or invalid, use clearly labeled per-feature median
  perturbations as an approximate probability-space fallback. Contributions
  are explanatory, not causal.
- Report five stable semantic driver keys: `dist_sma_200` (the existing
  `dist_200w_sma` feature, explicitly shown as 200-week), `volatility_30d`,
  `momentum_rsi`, `hmm_regime` (aggregate the four HMM posterior contributions),
  and `volume_trend` (`vol_zscore_365d`). Keep raw values and canonical keys in
  API responses; all human-readable Spanish/English labels stay in the web UI.
- If no evaluated candle or usable model inputs are available, return a safe
  explanation with no fabricated driver attributions. An unavailable signal
  has null probability/action zone; known probability with missing attribution
  retains its probability/action zone but has an empty driver list. Ledger reads
  must not trigger data downloads, trades, or model updates.
- The explainability card shows 30% and 60% progress markers and localized
  feature names, values, action-zone copy, and signed contribution estimates.
  It is a compact disclosure, not financial advice, and never changes account
  state or strategy execution.

## Consequences

- The ledger endpoint may read the existing processed dataset and model bundle
  to explain the timestamp of its latest persisted evaluation. Missing, stale,
  or incompatible inference inputs degrade to the safe neutral object.
- Threshold visualization intentionally uses the high-level 30%/60% zones;
  Profile B's existing hysteresis thresholds and minimum-BTC invariant remain
  unchanged.
- Contributions depend on the deployed model and imputed feature values and
  should not be interpreted as causal effects or guarantees.