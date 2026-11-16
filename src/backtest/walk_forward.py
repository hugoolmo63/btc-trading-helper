"""Purged expanding-window walk-forward validation and portfolio simulation."""

from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Final

import numpy as np
import polars as pl
import shap
from lightgbm import LGBMClassifier
from sklearn.metrics import brier_score_loss, roc_auc_score

from src.models.hmm_regimes import (
    HMMRegimeModel,
    add_hmm_probabilities,
    fit_hmm,
    get_feature_names,
)
from src.execution.paper_trader import (
    COOLDOWN_HOURS,
    DEFAULT_TAX_RATE,
    FIFOTaxLotQueue,
    WEEKLY_DEPOSIT_EUR,
    allocation_for_probability,
    live_eurusdt_rate,
)
from src.execution.tax import SpanishIRPFLedger

LOGGER = logging.getLogger(__name__)
RISK_FREE_APY: Final[float] = float(os.getenv("RISK_FREE_APY", "0.025"))
DATASET_PATH: Final[Path] = Path("data/processed/dataset_ml.parquet")
PURGE_HOURS: Final[int] = 720
TEST_YEARS: Final[tuple[int, ...]] = tuple(range(2020, 2027))
HMM_PROBABILITY_COLUMNS: Final[list[str]] = [f"hmm_prob_{i}" for i in range(4)]


@dataclass
class WalkForwardResult:
    """Out-of-sample predictions, fold metrics, and explainability results."""

    oos_predictions: pl.DataFrame
    fold_metrics: list[dict[str, float | int]]
    portfolio_metrics: dict[str, float]
    shap_importance: dict[str, float]


def _classifier() -> LGBMClassifier:
    return LGBMClassifier(
        objective="binary",
        max_depth=4,
        num_leaves=15,
        min_child_samples=500,
        learning_rate=0.03,
        n_estimators=300,
        random_state=42,
        deterministic=True,
        force_col_wise=True,
        n_jobs=1,
        verbosity=-1,
    )


def _model_columns(frame: pl.DataFrame) -> list[str]:
    return get_feature_names(frame.columns)


def _impute_model_features(
    train: pl.DataFrame,
    test: pl.DataFrame,
    columns: list[str],
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Impute both splits with medians calculated from train only."""
    medians: dict[str, float] = {}
    for column in columns:
        values = train[column].to_numpy()
        finite = values[np.isfinite(values)]
        medians[column] = float(np.median(finite)) if finite.size else 0.0

    def impute(frame: pl.DataFrame) -> pl.DataFrame:
        return frame.with_columns(
            [
                pl.when(pl.col(column).is_finite())
                .then(pl.col(column))
                .otherwise(pl.lit(medians[column]))
                .alias(column)
                for column in columns
            ]
        )

    return impute(train), impute(test)


def _fold_bounds(year: int) -> tuple[datetime, datetime, datetime]:
    test_start = datetime(year, 1, 1, tzinfo=timezone.utc)
    test_end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    train_end = test_start - timedelta(hours=PURGE_HOURS)
    return train_end, test_start, test_end


def _shap_values(model: LGBMClassifier, features: np.ndarray) -> np.ndarray:
    values = shap.TreeExplainer(model)(features).values
    if values.ndim == 3:
        values = values[:, :, 1]
    return np.asarray(values)


def simulate_portfolio(predictions: pl.DataFrame) -> dict[str, float]:
    """Simulate capped-daily rebalancing with 10 bps traded-notional fees."""
    frame = predictions.sort("timestamp").with_columns(
        (pl.col("close").shift(-1) / pl.col("close") - 1.0).alias("next_return")
    )
    equity = 1.0
    buy_hold = 1.0
    allocation = 0.0
    last_rebalance: datetime | None = None
    strategy_returns: list[float] = []
    buy_hold_returns: list[float] = []
    equity_curve: list[float] = []
    for row in frame.iter_rows(named=True):
        timestamp = row["timestamp"]
        if (
            last_rebalance is None
            or timestamp - last_rebalance >= timedelta(hours=24)
        ):
            target = allocation_for_probability(
                float(row["predicted_probability"]), allocation
            )
            equity -= abs(target - allocation) * equity * 0.001
            allocation = target
            last_rebalance = timestamp
        next_return = row["next_return"]
        if next_return is None or not math.isfinite(next_return):
            continue
        strategy_return = allocation * next_return
        equity *= 1.0 + strategy_return
        buy_hold *= 1.0 + next_return
        strategy_returns.append(strategy_return)
        buy_hold_returns.append(next_return)
        equity_curve.append(equity)

    if not equity_curve:
        raise ValueError("No valid out-of-sample returns for portfolio simulation")
    years = max(
        (frame["timestamp"][-1] - frame["timestamp"][0]).total_seconds()
        / (365.25 * 24 * 3600),
        1 / 365.25,
    )
    returns = np.asarray(strategy_returns)
    sharpe = (
        float(returns.mean() / returns.std(ddof=1) * math.sqrt(8760))
        if returns.size > 1 and returns.std(ddof=1) > 0
        else 0.0
    )
    curve = np.asarray(equity_curve)
    drawdown = curve / np.maximum.accumulate(curve) - 1.0
    return {
        "total_return": equity - 1.0,
        "cagr": equity ** (1.0 / years) - 1.0,
        "annualized_sharpe": sharpe,
        "max_drawdown": float(drawdown.min()),
        "buy_hold_total_return": buy_hold - 1.0,
        "buy_hold_cagr": buy_hold ** (1.0 / years) - 1.0,
        "buy_hold_max_drawdown": float(
            (np.cumprod(1.0 + np.asarray(buy_hold_returns))
             / np.maximum.accumulate(np.cumprod(1.0 + np.asarray(buy_hold_returns)))
             - 1.0).min()
        ),
    }


def simulate_recurring_profiles(
    predictions: pl.DataFrame,
    weekly_deposit_eur: float = WEEKLY_DEPOSIT_EUR,
    fee_rate: float = 0.001,
    tax_rate: float = DEFAULT_TAX_RATE,
) -> tuple[pl.DataFrame, dict[str, dict[str, float]]]:
    """Compare weekly DCA and hysteretic after-tax cycle trading."""
    if "eurusd" not in predictions.columns:
        predictions = predictions.with_columns(
            pl.lit(live_eurusdt_rate()).alias("eurusd")
        )
    else:
        predictions = predictions.with_columns(
            pl.col("eurusd").fill_null(strategy="forward").fill_null(strategy="backward")
        )
        if predictions["eurusd"].null_count():
            predictions = predictions.with_columns(
                pl.col("eurusd").fill_null(live_eurusdt_rate())
            )
    rows: list[dict[str, object]] = []
    state = {
        "dca_holder": {"btc": 0.0, "usdt": 0.0, "contributed": 0.0, "fees": 0.0},
        "cycle_ml_trader": {
            "btc": 0.0, "usdt": 0.0, "contributed": 0.0, "fees": 0.0,
            "taxes": 0.0, "yield": 0.0, "target": 0.2,
            "tax_escrow_eur": 0.0, "ytd_gain_eur": 0.0,
            "trade_count": 0.0, "cash_deposited": 0.0, "cash_deployed": 0.0,
        },
    }
    last_rebalance: datetime | None = None
    last_timestamp: datetime | None = None
    lots = FIFOTaxLotQueue()
    tax_ledger = SpanishIRPFLedger()
    for row in predictions.sort("timestamp").iter_rows(named=True):
        timestamp = row["timestamp"]
        price = float(row["close"])
        probability = float(row["predicted_probability"])
        eurusd = float(row.get("eurusd") or 0.0)
        if eurusd <= 0:
            raise ValueError(f"Missing EURUSDT FX rate at {timestamp}")
        if last_timestamp is not None and state["cycle_ml_trader"]["usdt"] > 0:
            hours = max(0.0, (timestamp - last_timestamp).total_seconds() / 3600.0)
            cash = state["cycle_ml_trader"]["usdt"]
            accrued = cash * ((1.0 + RISK_FREE_APY) ** (hours / (365.25 * 24)) - 1.0)
            state["cycle_ml_trader"]["usdt"] += accrued
            state["cycle_ml_trader"]["yield"] += accrued
        if timestamp.weekday() == 6 and timestamp.hour == 0:
            for profile in state:
                state[profile]["contributed"] += weekly_deposit_eur
            deposit_usdt = weekly_deposit_eur * eurusd
            dca_fee = deposit_usdt * fee_rate / (1.0 + fee_rate)
            state["dca_holder"]["btc"] += (
                deposit_usdt - dca_fee
            ) / price
            state["dca_holder"]["fees"] += dca_fee
            state["cycle_ml_trader"]["usdt"] += deposit_usdt
            state["cycle_ml_trader"]["cash_deposited"] += weekly_deposit_eur

        cycle = state["cycle_ml_trader"]
        cycle_equity = cycle["btc"] * price + cycle["usdt"]
        cycle_weight = cycle["btc"] * price / cycle_equity if cycle_equity else 0.0
        target = cycle["target"]
        if probability >= 0.60:
            target = 1.0
        elif probability < 0.30 and cycle["btc"] > 0:
            cost = sum(quantity * unit_cost for _, quantity, unit_cost in lots.lots)
            weighted_avg_cost = cost / cycle["btc"] if cost > 0 else 0.0
            if weighted_avg_cost > 0 and price >= weighted_avg_cost * 1.15:
                target = 0.20
        if (
            cycle_equity
            and (
                probability >= 0.60
                or (probability < 0.30 and target < cycle_weight)
            )
            and (last_rebalance is None or timestamp - last_rebalance >= timedelta(hours=COOLDOWN_HOURS))
            and abs(target - cycle_weight) >= 0.02
        ):
            if target > cycle_weight:
                notional = (target * cycle_equity - cycle["btc"] * price) / (
                    1.0 + target * fee_rate
                )
                cycle["btc"] += notional / price
                cycle["usdt"] -= notional * (1.0 + fee_rate)
                quantity = notional / price
                lots.buy(timestamp, quantity, (notional * (1.0 + fee_rate)) / quantity)
                cycle["fees"] += notional * fee_rate
                cycle["trade_count"] += 1.0
                cycle["cash_deployed"] += notional
            else:
                notional = (cycle["btc"] * price - target * cycle_equity) / (
                    1.0 - fee_rate
                )
                quantity = notional / price
                gain_usd, _ = lots.sell(quantity, price, tax_rate)
                gain_eur = gain_usd / eurusd
                tax_delta = tax_ledger.realize(timestamp.year, gain_eur)
                tax = max(0.0, tax_delta)
                cycle["btc"] -= quantity
                cycle["usdt"] += notional * (1.0 - fee_rate) - tax * eurusd
                cycle["fees"] += notional * fee_rate
                cycle["taxes"] += tax
                cycle["tax_escrow_eur"] = tax_ledger.tax_escrow_eur
                cycle["ytd_gain_eur"] = tax_ledger.ytd_gains_eur[timestamp.year]
                cycle["trade_count"] += 1.0
            cycle["target"] = target
            last_rebalance = timestamp

        for profile, values in state.items():
            equity = values["btc"] * price + values["usdt"]
            contributed = values["contributed"]
            rows.append(
                {
                    "profile": profile,
                    "timestamp": timestamp,
                    "btc_price_eur": price / eurusd,
                    "contributed_eur": contributed,
                    "equity_eur": equity / eurusd,
                    "net_profit_eur": equity / eurusd - contributed,
                    "fees_paid_eur": values.get("fees", 0.0) / eurusd,
                    "spanish_irpf_taxes_eur": values.get("taxes", 0.0),
                    "cash_yield_eur": values.get("yield", 0.0) / eurusd,
                    "btc_balance": values["btc"],
                    "net_btc_equivalent": values["btc"] + max(
                        0.0,
                        values["usdt"] - values.get("tax_escrow_eur", 0.0) * eurusd,
                    ) / price,
                    "tax_escrow_eur": values.get("tax_escrow_eur", 0.0),
                    "roi_pct": (equity / eurusd - contributed) / contributed * 100.0
                    if contributed
                    else None,
                    "btc_weight": values["btc"] * price / equity if equity else 0.0,
                }
            )
        last_timestamp = timestamp

    curve = pl.DataFrame(rows, infer_schema_length=None)
    if curve.is_empty():
        raise ValueError("No predictions available for recurring-profile simulation")
    metrics: dict[str, dict[str, float]] = {}
    for profile in ("dca_holder", "cycle_ml_trader"):
        profile_state = state[profile]
        final = curve.filter(pl.col("profile") == profile).tail(1)
        metrics[profile] = {
            "contributed_eur": float(final["contributed_eur"][0]),
            "equity_eur": float(final["equity_eur"][0]),
            "net_profit_eur": float(final["net_profit_eur"][0]),
            "roi_pct": float(final["roi_pct"][0]),
            "fees_paid_eur": float(final["fees_paid_eur"][0]),
            "spanish_irpf_taxes_eur": float(final["spanish_irpf_taxes_eur"][0]),
            "cash_yield_eur": float(final["cash_yield_eur"][0]),
            "final_btc_held": float(final["btc_balance"][0]),
            "net_btc_equivalent": float(final["net_btc_equivalent"][0]),
            "final_equity_eur": float(final["equity_eur"][0]),
            "tax_escrow_eur": float(final["tax_escrow_eur"][0]),
            "trade_count": float(profile_state.get("trade_count", 0.0)),
            "cash_deployment_rate": (
                float(profile_state.get("cash_deployed", 0.0))
                / float(profile_state.get("cash_deposited", 1.0))
                if profile_state.get("cash_deposited", 0.0)
                else 0.0
            ),
        }
        metrics[profile]["average_btc_weight"] = float(
            curve.filter(pl.col("profile") == profile)["btc_weight"].mean()
        )
        values = curve.filter(pl.col("profile") == profile)["equity_eur"].to_numpy()
        returns = np.divide(
            np.diff(values),
            values[:-1],
            out=np.full(values[:-1].shape, np.nan, dtype=float),
            where=values[:-1] > 0,
        )
        returns = returns[np.isfinite(returns)]
        peak = np.maximum.accumulate(values)
        drawdown = np.divide(
            values,
            peak,
            out=np.ones(values.shape, dtype=float),
            where=peak > 0,
        ) - 1.0
        metrics[profile]["max_drawdown"] = float(np.min(drawdown))
        years = max(
            (predictions["timestamp"][-1] - predictions["timestamp"][0]).total_seconds()
            / (365.25 * 24 * 3600),
            1 / 365.25,
        )
        metrics[profile]["net_cagr"] = (
            (metrics[profile]["equity_eur"] / metrics[profile]["contributed_eur"])
            ** (1.0 / years) - 1.0
            if metrics[profile]["contributed_eur"] else 0.0
        )
        metrics[profile]["sharpe_ratio"] = float(
            returns.mean() / returns.std(ddof=1) * math.sqrt(8760)
        ) if returns.size > 1 and returns.std(ddof=1) > 0 else 0.0
        metrics[profile]["gross_return_pct"] = (
            (
                metrics[profile]["net_profit_eur"]
                + metrics[profile]["fees_paid_eur"]
                + metrics[profile]["spanish_irpf_taxes_eur"]
                - metrics[profile]["cash_yield_eur"]
            )
            / metrics[profile]["contributed_eur"]
            * 100.0
            if metrics[profile]["contributed_eur"]
            else 0.0
        )
    dca = metrics["dca_holder"]
    cycle = metrics["cycle_ml_trader"]
    cycle["market_timing_alpha_eur"] = (
        cycle["net_profit_eur"] - dca["net_profit_eur"]
        - cycle["cash_yield_eur"] + cycle["fees_paid_eur"]
        + cycle["spanish_irpf_taxes_eur"]
    )
    return curve, metrics


def run_walk_forward(
    dataset_path: Path = DATASET_PATH,
    collect_shap: bool = True,
) -> WalkForwardResult:
    """Run annual purged expanding folds from 2020 through 2026."""
    dataset = pl.read_parquet(dataset_path).sort("timestamp")
    stationary = get_feature_names(dataset.columns)
    if not stationary:
        raise ValueError("No stationary engineered features found")
    predictions: list[pl.DataFrame] = []
    metrics: list[dict[str, float | int]] = []
    shap_sums: dict[str, float] = {}
    shap_counts: dict[str, int] = {}

    for year in TEST_YEARS:
        train_end, test_start, test_end = _fold_bounds(year)
        train = dataset.filter(
            (pl.col("timestamp") < train_end)
            & pl.col("target_up_10pct_30d").is_not_null()
        )
        test = dataset.filter(
            (pl.col("timestamp") >= test_start)
            & (pl.col("timestamp") < test_end)
            & pl.col("target_up_10pct_30d").is_not_null()
        )
        if train.is_empty() or test.is_empty():
            LOGGER.warning("Skipping %d: train or test split is empty", year)
            continue

        hmm = fit_hmm(train)
        train_hmm = add_hmm_probabilities(train, hmm)
        test_hmm = add_hmm_probabilities(test, hmm)
        columns = _model_columns(train_hmm)
        train_model, test_model = _impute_model_features(
            train_hmm.filter(pl.col("target_up_10pct_30d").is_not_null()),
            test_hmm.filter(pl.col("target_up_10pct_30d").is_not_null()),
            columns,
        )

        model = _classifier()
        model.fit(
            train_model.select(columns).to_numpy(),
            train_model["target_up_10pct_30d"].to_numpy(),
            sample_weight=train_model["sample_weight"].to_numpy(),
        )
        test_matrix = test_model.select(columns).to_numpy()
        probability = model.predict_proba(test_matrix)[:, 1]
        actual = test_model["target_up_10pct_30d"].to_numpy()
        fold_auc = roc_auc_score(actual, probability)
        fold_brier = brier_score_loss(actual, probability)
        metrics.append({"year": year, "roc_auc": fold_auc, "brier": fold_brier})
        LOGGER.info("Fold %d: ROC-AUC=%.4f Brier=%.4f", year, fold_auc, fold_brier)
        if collect_shap:
            values = np.abs(_shap_values(model, test_matrix)).mean(axis=0)
            for column, value in zip(columns, values, strict=True):
                shap_sums[column] = shap_sums.get(column, 0.0) + float(value)
                shap_counts[column] = shap_counts.get(column, 0) + 1
        prediction_columns = ["timestamp", "close", "target_up_10pct_30d"]
        if "eurusd" in test_model.columns:
            prediction_columns.append("eurusd")
        predictions.append(
            test_model.select(prediction_columns).with_columns(
                pl.Series("predicted_probability", probability)
            )
        )

    if not predictions:
        raise ValueError("No walk-forward folds produced predictions")
    oos = pl.concat(predictions).sort("timestamp")
    importance = {
        column: shap_sums[column] / shap_counts[column] for column in shap_sums
    }
    return WalkForwardResult(
        oos,
        metrics,
        simulate_portfolio(oos),
        importance,
    )


if __name__ == "__main__":
    result = run_walk_forward()
    LOGGER.info("Portfolio metrics: %s", result.portfolio_metrics)
    recurring_curve, recurring_metrics = simulate_recurring_profiles(
        result.oos_predictions
    )
    LOGGER.info("Recurring-profile metrics: %s", recurring_metrics)
    print(
        "profile | gross return | fees | FIFO taxes | cash yield | "
        "net CAGR | Sharpe | max drawdown"
    )
    for profile, values in recurring_metrics.items():
        print(
            f"{profile} | {values['gross_return_pct']:.2f}% | "
            f"€{values['fees_paid_eur']:.2f} | "
            f"€{values['spanish_irpf_taxes_eur']:.2f} | "
            f"€{values['cash_yield_eur']:.2f} | {values['net_cagr']:.2%} | "
            f"{values['sharpe_ratio']:.3f} | {values['max_drawdown']:.2%}"
        )
    print(result.oos_predictions.head())
