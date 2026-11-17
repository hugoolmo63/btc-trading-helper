---
title: "Month-End Clamping for Monthly DCA Schedules"
status: accepted
date: 2026-11-17
authors: [@hugoolmo63]
reviewers: [@Copilot]
tags: [architecture, decision, schema, recurring-investment]
related: [[adr-0010-configurable-recurring-dca-schedule]]
supersedes:
---

# ADR: Month-End Clamping for Monthly DCA Schedules

## Status

accepted

## Context

Monthly DCA schedules currently restrict the selected day to `1..28` to avoid
invalid calendar dates. That prevents schedules intended for the end of each
month from firing on time, and means a 29th/30th/31st schedule cannot be
represented directly.

## Decision

Allow persisted and API `day_of_month` values from `1..31`. For each target
month, calculate its scheduled calendar day as the lesser of the configured
day and that month's final day (`calendar.monthrange`). Matching, next-run
calculation, and API validation must use the same value. Preserve the existing
Madrid-midnight trigger and one-contribution-per-calendar-month idempotency.

## Consequences

- A configured 31st fires on February 28/29 and on the 30th in 30-day months.
- A schedule never skips a month because its configured day does not exist.
- Existing SQLite rows need no migration: the stored integer field already
  accommodates values through 31.
- Weekly scheduling and portfolio/tax accounting remain unchanged.