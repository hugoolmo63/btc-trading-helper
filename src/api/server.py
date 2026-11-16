"""REST API and static frontend server for the paper-trading system."""

from __future__ import annotations

import sqlite3
import logging
import math
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, Literal

import joblib
import numpy as np
import polars as pl
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, StrictBool, StrictInt, field_validator
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from src.execution.paper_trader import (
    PaperTrader,
    live_eurusdt_rate,
    model_mtime_ns,
    run_once,
)
from src.backtest.walk_forward import run_walk_forward, simulate_recurring_profiles
from src.ops.maintenance import latest_backup_timestamp

ROOT: Final[Path] = Path(__file__).resolve().parents[2]
DB_PATH: Final[Path] = ROOT / "data/paper_trades.db"
DATASET_PATH: Final[Path] = ROOT / "data/processed/dataset_ml.parquet"
MODEL_PATH: Final[Path] = ROOT / "models/btc_cycle_bundle.joblib"
WEB_PATH: Final[Path] = ROOT / "web"

app = FastAPI(title="BTC Cycle Trading API", version="1.0.0")
LOGGER = logging.getLogger(__name__)


class DepositRequest(BaseModel):
    amount_eur: float


class DCAScheduleRequest(BaseModel):
    enabled: StrictBool
    frequency: Literal["weekly", "monthly"]
    day_of_week: StrictInt = Field(ge=0, le=6)
    day_of_month: StrictInt = Field(ge=1, le=28)
    amount_eur: float = Field(gt=0)

    @field_validator("amount_eur")
    @classmethod
    def validate_finite_amount(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("amount_eur must be finite")
        return value


def _utc_string(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    elif isinstance(value, datetime):
        parsed = value
    else:
        return str(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _json_value(value: object) -> object:
    if isinstance(value, datetime):
        return _utc_string(value)
    if isinstance(value, float) and not np.isfinite(value):
        return None
    if isinstance(value, np.generic):
        return _json_value(value.item())
    return value


def _rows_as_json(rows: list[sqlite3.Row]) -> list[dict[str, object]]:
    result = []
    for row in rows:
        item = {key: _json_value(row[key]) for key in row.keys()}
        if "timestamp" in item:
            item["timestamp"] = _utc_string(item["timestamp"])
        result.append(item)
    return result


def _trade_eur_value(
    row: sqlite3.Row,
    eur_column: str,
    legacy_column: str,
    fx_rate: float,
) -> float:
    """Prefer persisted EUR trade values, falling back to legacy USD data."""
    columns = row.keys()
    value = row[eur_column] if eur_column in columns else None
    if value is not None and float(value) != 0.0:
        return float(value)
    return float(row[legacy_column] or 0.0) / fx_rate


def ledger_payload() -> dict[str, object]:
    fx_rate = live_eurusdt_rate()
    profiles = {
        "dca_holder": {
            "profile": "dca_holder",
            "contributed_eur": 0.0,
            "equity_eur": 0.0,
            "net_profit_eur": 0.0,
            "roi_pct": None,
            "btc_balance": 0.0,
            "net_btc_equivalent": 0.0,
            "cash_eur": 0.0,
            "tax_escrow_eur": 0.0,
        },
        "cycle_ml_trader": {
            "profile": "cycle_ml_trader",
            "contributed_eur": 0.0,
            "equity_eur": 0.0,
            "net_profit_eur": 0.0,
            "roi_pct": None,
            "btc_balance": 0.0,
            "net_btc_equivalent": 0.0,
            "cash_eur": 0.0,
            "tax_escrow_eur": 0.0,
        },
    }
    equity_rows: list[sqlite3.Row] = []
    trade_rows: list[sqlite3.Row] = []
    state_rows: list[sqlite3.Row] = []
    if DB_PATH.exists():
        try:
            trader = PaperTrader(DB_PATH)
            dca_schedule = trader.dca_schedule_payload()
            with sqlite3.connect(DB_PATH) as connection:
                connection.row_factory = sqlite3.Row
                equity_rows = connection.execute(
                    "SELECT * FROM equity_curve ORDER BY timestamp"
                ).fetchall()
                trade_rows = connection.execute(
                    "SELECT * FROM trades ORDER BY timestamp DESC"
                ).fetchall()
                state_rows = connection.execute(
                    "SELECT * FROM portfolio_state ORDER BY profile"
                ).fetchall()
        except sqlite3.Error as error:
            raise HTTPException(status_code=503, detail=f"Ledger unavailable: {error}") from error
    else:
        trader = PaperTrader(DB_PATH)
        dca_schedule = trader.dca_schedule_payload()

    for row in equity_rows:
        profile = row["profile"]
        if profile in profiles:
            contributed_eur = row["total_contributed_usd"]
            equity_eur = row["total_equity_usd"] / fx_rate
            profiles[profile] = {
                "profile": profile,
                "contributed_eur": contributed_eur,
                "equity_eur": equity_eur,
                "net_profit_eur": equity_eur - contributed_eur,
                "roi_pct": (
                    (equity_eur - contributed_eur) / contributed_eur * 100.0
                    if contributed_eur
                    else None
                ),
                "btc_balance": row["btc_balance"],
                "net_btc_equivalent": row["net_btc_equivalent"],
                "cash_eur": row["usdt_balance"] / fx_rate,
                "cash_yield_eur": row["usdt_yield_usd"] / fx_rate,
                "fees_paid_eur": row["fees_paid_usd"] / fx_rate,
                "tax_escrow_eur": row["tax_escrow_eur"],
                "btc_weight": (
                    row["btc_balance"] * (row["btc_price"] / fx_rate) / equity_eur
                    if equity_eur
                    else 0.0
                ),
            }
    for row in state_rows:
        if row["profile"] in profiles:
            profiles[row["profile"]]["contributed_eur"] = row[
                "total_contributed_usd"
            ]

    latest = equity_rows[-1] if equity_rows else None
    dca_equity = profiles["dca_holder"]["equity_eur"]
    cycle_equity = profiles["cycle_ml_trader"]["equity_eur"]
    bundle_mtime_ns = model_mtime_ns()
    latest_payload = None
    if latest:
        latest_payload = {
            "profile": latest["profile"],
            "timestamp": _utc_string(latest["timestamp"]),
            "btc_price_eur": latest["btc_price"] / fx_rate,
            "prob_up": latest["prob_up"],
            "hmm_regime": latest["hmm_regime"],
            "actual_weight": latest["actual_weight"],
            "btc_balance": latest["btc_balance"],
            "net_btc_equivalent": latest["net_btc_equivalent"],
            "equity_eur": latest["total_equity_usd"] / fx_rate,
            "cash_eur": latest["usdt_balance"] / fx_rate,
        }
    return {
        "dca_schedule": dca_schedule,
        "profiles": profiles,
        "alpha_eur": cycle_equity - dca_equity,
        "operational": {
            "last_backup_timestamp": latest_backup_timestamp(),
            "model_mtime_ns": bundle_mtime_ns,
            "model_mtime_utc": (
                datetime.fromtimestamp(
                    bundle_mtime_ns / 1_000_000_000, timezone.utc
                ).isoformat().replace("+00:00", "Z")
                if bundle_mtime_ns is not None
                else None
            ),
        },
        "latest": latest_payload,
        "equity_curve": [
            {
                "profile": row["profile"],
                "timestamp": _utc_string(row["timestamp"]),
                "btc_price_eur": row["btc_price"] / fx_rate,
                "btc_balance": row["btc_balance"],
                "net_btc_equivalent": row["net_btc_equivalent"],
                "contributed_eur": row["total_contributed_usd"],
                "equity_eur": row["total_equity_usd"] / fx_rate,
                "cash_eur": row["usdt_balance"] / fx_rate,
                "cash_yield_eur": row["usdt_yield_usd"] / fx_rate,
                "fees_paid_eur": row["fees_paid_usd"] / fx_rate,
                "tax_escrow_eur": row["tax_escrow_eur"],
                "btc_weight": (
                    row["btc_balance"] * (row["btc_price"] / fx_rate)
                    / (row["total_equity_usd"] / fx_rate)
                    if row["total_equity_usd"]
                    else 0.0
                ),
                "prob_up": row["prob_up"],
                "hmm_regime": row["hmm_regime"],
            }
            for row in equity_rows
        ],
        "trades": [
            {
                "profile": row["profile"],
                "timestamp": _utc_string(row["timestamp"]),
                "side": row["side"],
                "price_eur": _trade_eur_value(row, "price_eur", "price", fx_rate),
                "btc_amount": row["btc_quantity"],
                "notional_eur": _trade_eur_value(
                    row, "notional_eur", "usd_notional", fx_rate
                ),
                "fee_eur": _trade_eur_value(row, "fee_eur", "fee_usd", fx_rate),
                "tax_eur": row["tax_eur"],
            }
            for row in trade_rows
        ],
    }


def _seed_empty_ledger() -> None:
    """Evaluate one latest closed candle when the ledger has no history."""
    try:
        with sqlite3.connect(DB_PATH) as connection:
            has_rows = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'equity_curve'"
            ).fetchone()
            if has_rows and connection.execute(
                "SELECT 1 FROM equity_curve LIMIT 1"
            ).fetchone():
                return
        run_once(dry_run=False)
    except (OSError, sqlite3.Error, ValueError, RuntimeError):
        LOGGER.exception("Initial paper-trader seed evaluation failed")


@app.on_event("startup")
def seed_empty_ledger_on_startup() -> None:
    """Start seeding without blocking API startup on market-data refresh."""
    threading.Thread(target=_seed_empty_ledger, name="ledger-seed", daemon=True).start()


def _cycle_rows(max_points: int, start: str | None, end: str | None) -> list[dict[str, object]]:
    if not DATASET_PATH.exists() or not MODEL_PATH.exists():
        raise HTTPException(status_code=503, detail="Dataset or model bundle is missing")
    dataset = pl.read_parquet(DATASET_PATH).sort("timestamp")
    if start:
        start_value = datetime.fromisoformat(start.replace("Z", "+00:00"))
        dataset = dataset.filter(pl.col("timestamp") >= pl.lit(start_value))
    if end:
        end_value = datetime.fromisoformat(end.replace("Z", "+00:00"))
        dataset = dataset.filter(pl.col("timestamp") <= pl.lit(end_value))
    bundle = joblib.load(MODEL_PATH)
    fx_rate = live_eurusdt_rate()
    values = dataset.select(bundle["hmm_feature_names"]).to_numpy().astype(np.float64)
    medians = np.asarray(bundle["hmm_medians"], dtype=np.float64)
    invalid = ~np.isfinite(values)
    values[invalid] = np.take(medians, np.where(invalid)[1])
    probabilities = bundle["hmm"].predict_proba(
        bundle["hmm_scaler"].transform(values)
    )
    dataset = dataset.with_columns(
        pl.Series("hmm_regime", np.argmax(probabilities, axis=1))
    )
    if dataset.height > max_points:
        indices = np.linspace(0, dataset.height - 1, max_points).astype(int)
        dataset = dataset[indices]
    return [
        {
            "timestamp": _utc_string(row["timestamp"]),
            "btc_price_eur": _json_value(row["close"]) / fx_rate,
            "hmm_regime": row["hmm_regime"],
            "cycle_cos": _json_value(row["cycle_cos"]),
            "dist_200w_sma": _json_value(row["dist_200w_sma"]),
            "mvrv_zscore_365d": _json_value(row["mvrv_zscore_365d"]),
        }
        for row in dataset.select(
            [
                "timestamp",
                "close",
                "hmm_regime",
                "cycle_cos",
                "dist_200w_sma",
                "mvrv_zscore_365d",
            ]
        ).to_dicts()
    ]


@app.get("/api/ledger")
def get_ledger() -> dict[str, object]:
    return ledger_payload()


@app.get("/api/cycles")
def get_cycles(
    max_points: int = Query(default=5000, ge=100, le=50_000),
    start: str | None = None,
    end: str | None = None,
) -> dict[str, object]:
    return {"rows": _cycle_rows(max_points, start, end)}


@app.get("/api/backtest/recurring")
def get_recurring_backtest(max_points: int = Query(default=2000, ge=100, le=10_000)) -> dict[str, object]:
    result = run_walk_forward(collect_shap=False)
    curve, metrics = simulate_recurring_profiles(result.oos_predictions)
    if curve.height > max_points:
        indices = np.linspace(0, curve.height - 1, max_points).astype(int)
        curve = curve[indices]
    return {
        "metrics": metrics,
        "curve": [
            {
                key: _utc_string(value) if key == "timestamp" else _json_value(value)
                for key, value in row.items()
            }
            for row in curve.to_dicts()
        ],
    }


@app.post("/api/refresh")
def refresh(dry_run: bool = False) -> dict[str, object]:
    run_once(dry_run=dry_run)
    return ledger_payload()


@app.post("/api/deposit")
def deposit(request: DepositRequest) -> dict[str, object]:
    if not math.isfinite(request.amount_eur) or request.amount_eur <= 0:
        raise HTTPException(
            status_code=422,
            detail="amount_eur must be a finite value greater than zero",
        )
    try:
        result = PaperTrader(DB_PATH).deposit_capital(request.amount_eur)
        return {
            "amount_eur": request.amount_eur,
            "result": result,
            "ledger": ledger_payload(),
        }
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except (OSError, RuntimeError, sqlite3.Error) as error:
        LOGGER.exception("Manual capital deposit failed")
        raise HTTPException(status_code=503, detail=f"Deposit unavailable: {error}") from error


@app.post("/api/dca-schedule")
def update_dca_schedule(request: DCAScheduleRequest) -> dict[str, object]:
    try:
        trader = PaperTrader(DB_PATH)
        trader.update_dca_schedule(request.model_dump())
        return trader.dca_schedule_payload()
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except (OSError, RuntimeError, sqlite3.Error) as error:
        LOGGER.exception("DCA schedule update failed")
        raise HTTPException(status_code=503, detail=f"Schedule unavailable: {error}") from error


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_PATH / "index.html")


app.mount("/", StaticFiles(directory=WEB_PATH), name="web")
