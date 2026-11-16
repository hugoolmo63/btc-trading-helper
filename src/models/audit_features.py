"""Audit feature stationarity, live values, and out-of-sample SHAP importance."""

from __future__ import annotations

from pathlib import Path
from typing import Final

import joblib
import numpy as np
import polars as pl

from src.backtest.walk_forward import run_walk_forward
from src.models.hmm_regimes import get_feature_names

DATASET_PATH: Final[Path] = Path("data/processed/dataset_ml.parquet")
MODEL_PATH: Final[Path] = Path("models/btc_cycle_bundle.joblib")


def _with_hmm_probabilities(
    dataset: pl.DataFrame,
    bundle: dict[str, object],
) -> pl.DataFrame:
    feature_names = bundle["hmm_feature_names"]
    values = dataset.select(feature_names).to_numpy().astype(np.float64)
    medians = np.asarray(bundle["hmm_medians"], dtype=np.float64)
    invalid = ~np.isfinite(values)
    values[invalid] = np.take(medians, np.where(invalid)[1])
    probabilities = bundle["hmm"].predict_proba(
        bundle["hmm_scaler"].transform(values)
    )
    return dataset.with_columns(
        [pl.Series(f"hmm_prob_{i}", probabilities[:, i]) for i in range(4)]
    )


def build_audit_table(
    dataset_path: Path = DATASET_PATH,
    model_path: Path = MODEL_PATH,
) -> pl.DataFrame:
    """Build the stationarity and SHAP audit table."""
    dataset = pl.read_parquet(dataset_path).sort("timestamp")
    bundle = joblib.load(model_path)
    feature_names = list(bundle["feature_names"])
    expected = get_feature_names()
    unexpected = [name for name in feature_names if name not in expected]
    if unexpected:
        raise ValueError(f"Bundle contains non-stationary features: {unexpected}")

    with_hmm = _with_hmm_probabilities(dataset, bundle)
    live = with_hmm.tail(1)
    early = with_hmm.filter(
        (pl.col("timestamp").dt.year() >= 2012)
        & (pl.col("timestamp").dt.year() <= 2017)
    )
    recent = with_hmm.filter(
        (pl.col("timestamp").dt.year() >= 2020)
        & (pl.col("timestamp").dt.year() <= 2026)
    )
    shap_importance = run_walk_forward(
        dataset_path=dataset_path,
        collect_shap=True,
    ).shap_importance
    rows: list[dict[str, object]] = []
    for feature in feature_names:
        rows.append(
            {
                "feature": feature,
                "live_value": live[feature][0],
                "mean_2012_2017": early[feature].mean(),
                "std_2012_2017": early[feature].std(),
                "mean_2020_2026": recent[feature].mean(),
                "std_2020_2026": recent[feature].std(),
                "mean_abs_shap_oos": shap_importance.get(feature, 0.0),
            }
        )
    return pl.DataFrame(rows).sort("mean_abs_shap_oos", descending=True)


def main() -> None:
    table = build_audit_table()
    print(
        "feature | live_value | mean_2012_2017 | std_2012_2017 | "
        "mean_2020_2026 | std_2020_2026 | mean_abs_shap_oos"
    )
    for row in table.iter_rows(named=True):
        values = [
            row["feature"],
            row["live_value"],
            row["mean_2012_2017"],
            row["std_2012_2017"],
            row["mean_2020_2026"],
            row["std_2020_2026"],
            row["mean_abs_shap_oos"],
        ]
        print(
            f"{values[0]} | "
            + " | ".join(
                "null" if value is None else f"{float(value):.8f}"
                for value in values[1:]
            )
        )


if __name__ == "__main__":
    main()
