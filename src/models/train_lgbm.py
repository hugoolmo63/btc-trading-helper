"""Train the production LightGBM bundle and report walk-forward explainability."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Final

import joblib
import numpy as np
import polars as pl

from src.backtest.walk_forward import (
    DATASET_PATH,
    HMM_PROBABILITY_COLUMNS,
    _classifier,
    _impute_model_features,
    _model_columns,
    run_walk_forward,
)
from src.models.hmm_regimes import add_hmm_probabilities, fit_hmm, select_stationary_features

LOGGER = logging.getLogger(__name__)
MODEL_PATH: Final[Path] = Path("models/btc_cycle_bundle.joblib")


def train_production_bundle(
    dataset_path: Path = DATASET_PATH,
    model_path: Path = MODEL_PATH,
) -> dict[str, object]:
    """Fit HMM and LightGBM on every eligible labeled row and persist them."""
    dataset = pl.read_parquet(dataset_path).sort("timestamp")
    eligible = dataset.filter(pl.col("target_up_10pct_30d").is_not_null())
    hmm = fit_hmm(eligible)
    eligible_hmm = add_hmm_probabilities(eligible, hmm)
    feature_names = _model_columns(eligible_hmm)
    train, _ = _impute_model_features(
        eligible_hmm,
        eligible_hmm.head(0),
        feature_names,
    )
    model_medians: dict[str, float] = {}
    for column in feature_names:
        values = eligible_hmm[column].to_numpy()
        finite = values[np.isfinite(values)]
        model_medians[column] = float(np.median(finite)) if finite.size else 0.0
    model = _classifier()
    model.fit(
        train.select(feature_names).to_numpy(),
        train["target_up_10pct_30d"].to_numpy(),
        sample_weight=train["sample_weight"].to_numpy(),
    )
    bundle: dict[str, object] = {
        "model": model,
        "hmm": hmm.model,
        "hmm_scaler": hmm.scaler,
        "hmm_medians": hmm.medians,
        "hmm_feature_names": hmm.feature_names,
        "feature_names": feature_names,
        "model_feature_medians": model_medians,
        "target": "target_up_10pct_30d",
    }
    model_path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(bundle, model_path)
    return bundle


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    result = run_walk_forward()
    print("Top 15 mean absolute SHAP features:")
    for name, value in sorted(
        result.shap_importance.items(), key=lambda item: item[1], reverse=True
    )[:15]:
        print(f"{name}: {value:.8f}")
    print("Portfolio metrics:")
    for name, value in result.portfolio_metrics.items():
        print(f"{name}: {value:.6f}")
    train_production_bundle()
    print(f"Saved production bundle to {MODEL_PATH}")


if __name__ == "__main__":
    main()
