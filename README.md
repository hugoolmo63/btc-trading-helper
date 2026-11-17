# Bitcoin 4-Year Cycle Quantitative Bot

**A BTC/EUR quantitative research and paper-trading dashboard.** The system combines leak-aware market/on-chain feature engineering, a four-state Hidden Markov Model (HMM), a LightGBM probability model, walk-forward evaluation, and an SQLite paper-trading ledger. It is for research and paper trading; it does not place live orders.

**ES:** Bot cuantitativo de investigación y paper trading BTC/EUR. Combina ingeniería de variables sin filtración de información, un modelo HMM de cuatro estados, LightGBM, evaluación walk-forward y un libro de operaciones en SQLite. Está diseñado para investigación y paper trading; no ejecuta órdenes reales.

## Features / Funcionalidades

- BTC-denominated portfolio strategies with EUR-first reporting.
- Profile A passive DCA and Profile B stateful Cycle-ML allocation.
- Deterministic FIFO lots and progressive Spanish IRPF *Base del Ahorro* tax accounting.
- Configurable weekly/monthly recurring contributions with persistent idempotency state.
- FastAPI REST service and bilingual Spanish/English dashboard.
- Reproducible purged walk-forward evaluation and recurring-investment diagnostics.

**ES:** Dos perfiles de estrategia, valoración prioritaria en EUR, lotes fiscales FIFO, IRPF progresivo, aportaciones recurrentes configurables y panel bilingüe con diagnóstico walk-forward.

## Architecture

```text
Public market/data sources
  Binance Vision / Binance public spot (ccxt) · FRED · on-chain provider
                         │
              src/ingestion/
                         │
    UTC Parquet → Polars feature engineering
                         │
      HMM regimes + LightGBM probability model
                         │
        purged walk-forward backtesting
                         │
   SQLite paper-trading engine + FIFO/IRPF ledger
                         │
           FastAPI JSON API (:8501)
                         │
       ES/EN HTML/CSS/JavaScript dashboard
```

- **Runtime:** Docker Compose runs the FastAPI dashboard/API, a scheduled hourly paper-trader, and a maintenance/retraining/backup daemon. The dashboard is served at port `8501`.
- **Data and storage:** Polars performs typed, timezone-aware UTC transformations and writes compressed Parquet datasets. SQLite persists portfolio state, trades, tax lots, DCA configuration, and execution history. DuckDB is part of the intended analytics stack for local analytical queries; the runtime storage contract is Parquet plus SQLite.
- **Market connectivity:** `ccxt` accesses public Binance spot data, including EUR/USDT FX where available. Macro inputs include FRED series, and on-chain data is collected by the ingestion module. Exchange credentials are not required for the public paper-trading feed.
- **Models:** A four-state Gaussian HMM classifies market regimes. A LightGBM classifier estimates the forward BTC return probability using stationary features (returns, rolling statistics, distances, and cyclical encodings), not raw price levels.
- **Leakage controls:** Walk-forward validation is chronological and purged/embargoed to match the target horizon. Daily features are shifted to respect publication time and joined with a bounded freshness tolerance.
- **API/UI boundary:** API responses use language-neutral keys and enums. All Spanish/English presentation strings live in `web/i18n.js`.

**ES — Arquitectura:** Docker Compose ejecuta FastAPI, el evaluador horario de paper trading y el proceso de mantenimiento. Polars procesa datos UTC en Parquet; SQLite conserva cartera, operaciones, lotes y configuración. El HMM clasifica cuatro regímenes y LightGBM estima probabilidades con variables estacionarias. La API es independiente del idioma y la localización se realiza en el navegador.

## Strategy Profiles / Perfiles de estrategia

### Profile A — Passive DCA / DCA pasivo

Each contribution purchases BTC immediately, less the configured `0.10%` execution fee. The engine records the acquisition in the EUR FIFO tax-lot ledger. The profile does not actively sell BTC as part of its passive strategy.

**ES:** Cada aportación compra BTC inmediatamente, descontando una comisión de ejecución del `0,10%`, y registra el lote de adquisición en EUR mediante FIFO. El perfil no vende BTC de forma activa.

### Profile B — Active Cycle-ML Trader / Trader Cycle-ML

Contributions are added to yield-bearing cash. Cash accrues at `2.5% TAE` by default. A scheduled contribution is deployed into BTC when the signal probability is at least `0.60`; below that threshold it remains in cash without a deposit-triggered purchase fee. Subsequent allocation uses the persisted hysteresis strategy and preserves a minimum `20%` BTC allocation.

**ES:** Las aportaciones se incorporan a liquidez remunerada, con un `2,5% TAE` predeterminado. La aportación programada se invierte en BTC si la probabilidad de señal es igual o superior a `0,60`; de lo contrario permanece en efectivo sin comisión de compra asociada al depósito. La asignación posterior aplica la estrategia con histéresis y mantiene un mínimo del `20%` en BTC.

### Spanish tax accounting / Fiscalidad española

The tax ledger consumes BTC lots deterministically in FIFO order and accounts for annual EUR realized gains under the progressive savings-base (*Base del Ahorro*) brackets:

| Annual taxable savings-base amount | Marginal rate |
| --- | ---: |
| €0–€6,000 | 19% |
| €6,000–€50,000 | 21% |
| €50,000–€200,000 | 23% |
| €200,000–€300,000 | 27% |
| €300,000–€500,000 | 28% |
| Above €500,000 | 30% |

Same-year losses offset gains as implemented by the tax ledger. Accrued `tax_escrow_eur` is ring-fenced and excluded from spendable cash and reinvestment. Figures are a software model and **not tax or investment advice**; users should validate personal filings with a qualified professional.

**ES:** El motor fiscal consume los lotes BTC en orden FIFO y aplica los tramos progresivos anuales de la base del ahorro indicados arriba. Las pérdidas del mismo ejercicio compensan ganancias según el libro fiscal. `tax_escrow_eur` queda segregado y no puede reinvertirse. El cálculo no constituye asesoramiento fiscal ni financiero.

## Recurring DCA planner / Planificador DCA recurrente

The dashboard shows the saved contribution schedule and its next execution in `Europe/Madrid` local time, including CET/CEST seasonal changes. Edit the plan to activate or pause it, choose weekly or monthly frequency, choose a weekday (`0=Monday` to `6=Sunday`) or day of month (`1..31`, clamped to each month's final calendar day), and set an EUR amount. Defaults are active, weekly Sunday, €100. The ledger stores a period key (`YYYY-Www` or `YYYY-MM`) in the same transaction as scheduled portfolio updates to prevent repeated evaluations from double-crediting a period.

Profile A buys BTC and creates a EUR FIFO lot for each scheduled contribution. Profile B accrues cash yield and only deploys the contribution at a signal probability of `0.60` or higher. Both strategies preserve their existing fee, tax, FIFO, allocation-floor, and escrow accounting rules.

**ES:** El panel muestra la próxima aportación en hora local de `Europe/Madrid` (CET/CEST). Se puede activar o pausar el plan, elegir frecuencia semanal o mensual, día de semana (`0=lunes` a `6=domingo`) o día de mes (`1..31`, ajustado al último día disponible de cada mes) e importe EUR. El valor inicial es semanal, domingo, €100 y activo. Una clave de periodo guardada dentro de la misma transacción evita aportaciones duplicadas. Perfil A compra BTC y registra un lote FIFO; Perfil B mantiene liquidez remunerada y despliega el depósito únicamente con señal `>=0,60`.

## Dashboard / Panel

- **Language:** Spanish and English selector.
- **Themes:** Cyber-Emerald dark mode and Nordic-Daylight light mode.
- **Profile switcher:** Compare selected-profile KPIs and ledger activity for Profile A and Profile B.
- **Manual deposit:** Add EUR capital to both paper portfolios through the common deposit accounting pipeline.
- **Recurring DCA Scheduler:** View status, schedule rule, next Spain-local execution, and edit its settings.
- **Spanish IRPF tools:** Export the FIFO tax report as CSV and use the What-If liquidity simulator to estimate cash use, BTC sales, fees, taxes, and lot-level realized gain/loss before a withdrawal. The simulator is read-only and does not execute a sale.
- **Market and performance:** BTC/EUR price, regime/signal, equity and net-BTC curves, historical regime/feature views, trade ledger, and recurring backtest diagnostics.
- **Timezone:** Displayed market/execution timestamps are rendered in `Europe/Madrid`; persisted market timestamps and API timestamps are UTC.

**ES:** El panel incluye selector ES/EN, temas Cyber-Emerald y Nordic-Daylight, selector de perfil, depósito manual, editor DCA, precio BTC/EUR, señal y régimen, curvas de patrimonio/BTC neto, libro de operaciones y diagnósticos.

## Quickstart / Inicio rápido

Requirements: Docker Engine with Docker Compose. No exchange API key is needed for public market data.

```bash
docker compose up -d --build
```

Open **http://localhost:8501**. Follow service logs with:

```bash
docker compose logs -f web paper-trader maintenance
```

Run the test suite locally (Python 3.11+ and packages from `requirements.txt`):

```bash
pytest -q
```

Check JavaScript syntax:

```bash
node --check web/app.js && node --check web/i18n.js
```

**ES:** Requisitos: Docker Engine y Docker Compose. Ejecuta `docker compose up -d --build` y abre **http://localhost:8501**. La suite se ejecuta con `pytest -q` en Python 3.11+.

## Local development / Desarrollo local

Install dependencies and start the API:

```bash
python -m pip install -r requirements.txt
uvicorn src.api.server:app --reload --host 0.0.0.0 --port 8501
```

The `paper-trader` and `maintenance` Compose services are separate from the API. For a full scheduled paper-trading stack, use Compose. Never copy a live SQLite ledger with an OS file-copy command; online backups must use SQLite's backup API.

## Repository layout / Estructura

```text
src/ingestion/   Market, macro and on-chain data ingestion
src/features/    Leak-aware stationary feature construction
src/models/      Feature audits, HMM regimes and LightGBM training
src/backtest/    Purged walk-forward and recurring-profile simulations
src/execution/   Paper trader, SQLite ledger and Spanish tax engine
src/api/         FastAPI endpoints and ledger serialization
src/ops/         Model maintenance, retraining and SQLite backups
web/             Bilingual browser dashboard
docs/adr/        Accepted architecture decision records
tests/           Unit, integration, tax, leakage and UI contract tests
data/            Runtime market data and SQLite state (ignored by Git)
models/          Locally generated model artifacts (ignored by Git)
```

## API overview / API

- `GET /api/ledger` — EUR portfolio ledger, profiles, trades, and DCA schedule with UTC/Madrid next-execution times.
- `POST /api/dca-schedule` — validate and persist `{enabled, frequency, day_of_week, day_of_month, amount_eur}`.
- `POST /api/deposit` — deposit positive `amount_eur` into both profiles.
- `POST /api/refresh` — update and evaluate the latest closed candle.
- `GET /api/cycles` — sampled regime/features view.
- `GET /api/backtest/recurring` — recurring walk-forward curve and strategy diagnostics.

**ES:** La API expone `/api/ledger`, `/api/dca-schedule`, `/api/deposit`, `/api/refresh`, `/api/cycles` y `/api/backtest/recurring`. Los payloads usan claves canónicas; la presentación ES/EN corresponde al frontend.

## Data and operational safety / Seguridad operativa

Runtime data, SQLite databases and backups, Parquet datasets, credentials, Python caches, and serialized model artifacts are excluded by `.gitignore`. The repository intentionally does not ship those runtime artifacts; ingestion/training workflows or the operator must provision data and model artifacts. Model publication is atomic, and database backups use `sqlite3.Connection.backup()`.

This project is experimental paper-trading software. It does not guarantee model quality, data availability, tax outcomes, or investment returns. Never configure it with order-execution credentials without separately reviewing the execution architecture and security controls.

## Architecture decisions

See [`docs/adr/`](docs/adr/) for accepted decisions on data freshness, allocation and yield, FIFO and Spanish IRPF, recurring backtests, bilingual UI, backups, EUR valuation, and configurable DCA scheduling. Quantitative, tax, allocation, data-source, and schema changes require an accepted ADR.