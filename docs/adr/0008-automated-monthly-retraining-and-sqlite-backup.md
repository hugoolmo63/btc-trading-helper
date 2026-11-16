---
title: "Automated Monthly Retraining and SQLite Backup"
status: accepted
date: 2026-11-16
authors: [@hugoolmo63]
reviewers: [@Copilot]
tags: [architecture, decision, operations, mlops, backup]
related: [[adr-0003-fifo-capital-gains-tax-accounting]], [[adr-0006-spanish-irpf-progressive-tax-and-multicurrency-btc-kpi]]
supersedes:
---

# ADR: Automated Monthly Retraining and SQLite Backup

## Status
accepted

## Context

The live paper-trading ledger must be backed up without interrupting active
SQLite readers or writers. The model bundle must also be refreshed regularly
without restarting the API or paper-trader containers, while preventing an
incomplete artifact from becoming live.

## Decision

Use SQLite's online `Connection.backup()` API to create timestamped database
backups under `data/backups/`. Never copy the live database file with a raw OS
file-copy operation. Retain the last 30 daily backups and 12 monthly backups.

Run maintenance daily at 01:00 UTC for backups and on the first day of each
month at 02:30 UTC for retraining. Retraining refreshes source data, rebuilds
the ML dataset, trains the HMM and LightGBM bundle, validates its schema and
sanity constraints, writes to `btc_cycle_bundle.joblib.tmp`, and atomically
publishes it with `os.replace()`.

The paper trader and API cache the model bundle together with its
`st_mtime_ns`. When the mtime changes, the bundle is reloaded in memory without
restarting the process.

## Consequences

### Positive

- Online backups remain consistent while the ledger is active.
- Retention prevents unbounded backup storage growth.
- Atomic replacement prevents partial model artifacts from being served.
- Model updates occur without API or trader downtime.

### Negative

- Maintenance requires enough disk space for retained backups and temporary
  model artifacts.
- Retraining can be resource-intensive and must not overlap with itself.
- A failed quality gate leaves the previous model active.

## Alternatives Considered

### Alternative 1

Copy `paper_trades.db` with `shutil.copy` or `cp`. This can produce an
inconsistent snapshot during a write transaction, so it was rejected.

### Alternative 2

Write directly over the production model path. This can expose a truncated or
partially written artifact to readers, so it was rejected.

### Alternative 3

Restart containers after each monthly training run. This introduces avoidable
downtime and loses in-memory state, so it was rejected.

## Implementation Notes

Backups use filenames `paper_trades_YYYYMMDD_HHMMSS.db`. Daily and monthly
retention classes are identified from backup timestamps; the newest snapshots
in each class are preserved.

The retraining quality gate must verify that the temporary bundle exists,
loads successfully, contains the expected model, HMM, scaler, feature names,
and medians, and can produce finite predictions before `os.replace()`.

The maintenance CLI supports `--backup-now`, `--retrain-now`, and `--daemon`.
The daemon schedules daily backup and monthly retraining in UTC.

## References

- [[adr-0003-fifo-capital-gains-tax-accounting]]
- [[adr-0006-spanish-irpf-progressive-tax-and-multicurrency-btc-kpi]]
- [SQLite Online Backup API](https://www.sqlite.org/backup.html)
- [Python `os.replace`](https://docs.python.org/3/library/os.html#os.replace)
