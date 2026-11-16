# AGENTS.md — Bitcoin 4-Year Cycle Quantitative Bot

## Project Overview
A modular Python quantitative research and paper-trading pipeline focused on Bitcoin's 4-year halving cycles, on-chain valuation metrics, and regime detection.

## Tech Stack
- **Data Processing & Storage:** `polars` (lazy evaluation preferred), `duckdb`, Apache Parquet (`zstd` compression).
- **Machine Learning:** `lightgbm`, `scikit-learn`, `hmmlearn`, `shap`.
- **Market Connectivity:** `ccxt` (public endpoints only for live paper trading), `requests` / `httpx`.
- **State & Logging:** `sqlite3` for paper-trading ledger and execution logs.

## Directory Structure
- `config/`: YAML configuration files (`settings.yaml`). Never hardcode parameters.
- `data/raw/`: Unmodified downloaded archives (ZIPs, CSVs). Git-ignored.
- `data/processed/`: Clean, typed, deduplicated `.parquet` tables. Git-ignored.
- `src/ingestion/`: Bulk downloaders (Binance Vision, Coin Metrics, FRED).
- `src/features/`: Stationary feature engineering & leak-free joins.
- `src/models/`: Regime classifiers (HMM) and signal models (LightGBM).
- `src/backtest/`: Purged walk-forward validation & fee-adjusted simulation.
- `src/execution/`: Live paper-trading engine and SQLite portfolio tracker.
- `tests/`: Pytest suite including mandatory data-leakage tests.
- `docs/adr/`: Accepted Architecture Decision Records for quantitative,
  allocation, tax, data-source, and schema changes.

## Strict Engineering Rules
1. **UTC Everywhere:** All timestamps must be timezone-aware UTC (`Datetime("us", "UTC")` in Polars).
2. **Zero Data Leakage:**
   - Never use standard `train_test_split` with shuffling. Always use chronological splits with an embargo/purge window matching the longest target horizon (e.g., 720 hours for a 30-day forward return).
   - When joining daily data (on-chain/macro) to hourly market data, shift daily timestamps forward by +24 hours before executing `join_asof(strategy="backward")` so an intraday candle only sees prior completed daily closes.
3. **Stationarity First:** Never feed raw price levels into tree-based models. Use log returns, rolling Z-scores, distance to moving averages, or trigonometric cyclical encodings (`sin`/`cos`).
4. **Code Standards:** Python 3.11+, strict type hints, modular functions, and `loguru` or standard `logging` instead of `print()`.
5. **ADR-Driven Quantitative Changes:** Every future quantitative, allocation,
   data-source, or schema change must have a corresponding accepted ADR in
   `docs/adr/` before implementation.
6. **Freshness and Tax Invariants:** Daily `join_asof` features must use a
   maximum `7d` tolerance after the +24h publication shift. Profile B must
   never allocate below 20% BTC, and every taxable sale must update deterministic
   FIFO tax lots and realized-tax accounting.
7. **Spanish Tax and Currency Invariants:** Apply the Spanish progressive IRPF
   savings-base brackets to annual EUR realized gains, allow same-year loss
   offsets, and ring-fence all accrued `tax_escrow_eur` so it cannot be
   reinvested. Maintain valuation parity across BTC, EUR, and USD, including
   `net_btc_equivalent`.
8. **Language-Agnostic API Invariant:** REST payloads must use canonical,
   language-independent keys and enums. All ES/EN localization belongs in
   `web/i18n.js`, not in backend responses.
9. **Operational Backup Invariant:** Never copy `data/paper_trades.db` with a
   raw OS file-copy operation while live processes are running. Use
   `sqlite3.Connection.backup()` for every online backup.
10. **Atomic Model Deployment Invariant:** Model artifacts must be written to a
    temporary file, pass a validation and quality gate, and be published with
    `os.replace()` so readers never observe a partial model.