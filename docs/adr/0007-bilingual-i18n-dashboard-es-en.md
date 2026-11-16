---
title: "Bilingual Dashboard Internationalization ES/EN"
status: accepted
date: 2026-11-16
authors: [@hugoolmo63]
reviewers: [@Copilot]
tags: [arquitectura, decision, frontend, i18n]
related: [[adr-0006-spanish-irpf-progressive-tax-and-multicurrency-btc-kpi]]
supersedes:
---

# ADR: Bilingual Dashboard Internationalization ES/EN

## Status
accepted

## Context

The dashboard is used by a Spanish-speaking owner while retaining English
quantitative engineering conventions. Language switching must be immediate,
must not duplicate backend logic, and must not reload API data.

## Decision

Implement a zero-dependency client-side dictionary in `web/i18n.js` with
Spanish (`es`) as the default and English (`en`) as the alternate language.

The header provides an `ES | EN` switcher. The selected language is persisted
in `localStorage` under `btc_helper_lang`, and the document's `<html lang>`
attribute is updated.

Static elements use `data-i18n` attributes. Dynamic KPI cards, currency and BTC
labels, Plotly titles and axes, legends, hover labels, HMM regimes, and trade
ledger labels are re-rendered from the dictionary without refetching API data.

REST responses remain language-agnostic and use canonical keys and enums such
as `BUY`, `SELL`, `dca_holder`, `cycle_ml_trader`, and `regime_0..3`.

## Consequences

### Positive

- Spanish and English users share one dashboard implementation.
- Language changes are instant and do not trigger network requests.
- Backend contracts remain stable and machine-readable.

### Negative

- Every user-visible key must exist in both dictionaries.
- Plotly configuration must be rebuilt when language changes.
- Translation quality requires review as new metrics are added.

## Alternatives Considered

### Alternative 1

Create separate Spanish and English pages. This duplicates UI logic and risks
feature drift, so it was rejected.

### Alternative 2

Localize responses in the backend. This couples API consumers to a user
language and complicates caching, so it was rejected.

## Implementation Notes

The dictionary is loaded once. Current API payloads are held in memory and
passed through rendering functions on toggle. A parity test must assert that
the `es` and `en` key sets are identical.

## References

- [[adr-0006-spanish-irpf-progressive-tax-and-multicurrency-btc-kpi]]
- [MDN: HTML `lang` attribute](https://developer.mozilla.org/en-US/docs/Web/HTML/Global_attributes/lang)
