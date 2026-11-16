"""Four-state Gaussian HMM regime features."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

import numpy as np
import polars as pl
from hmmlearn.hmm import GaussianHMM
from sklearn.preprocessing import StandardScaler

STATIONARY_FEATURES: Final[tuple[str, ...]] = (
    "cycle_sin",
    "cycle_cos",
    "dist_200w_sma",
    "dist_50w_sma",
    "dist_30d_sma",
    "hashrate_change_30d",
    "dgs10_change_90d",
    "walcl_change_90d",
    "mvrv_zscore_365d",
    "vol_zscore_365d",
    "hashrate_change_30d_zscore_365d",
    "dgs10_change_90d_zscore_365d",
    "walcl_change_90d_zscore_365d",
    "log_return_1h",
    "log_return_24h",
    "log_return_7d",
    "log_return_30d",
    "realized_vol_30d",
    "rsi_14d",
)
HMM_FEATURES: Final[tuple[str, ...]] = (
    "realized_vol_30d",
    "log_return_1h",
    "log_return_24h",
    "log_return_7d",
    "log_return_30d",
    "mvrv_zscore_365d",
)
HMM_PROBABILITY_FEATURES: Final[tuple[str, ...]] = tuple(
    f"hmm_prob_{index}" for index in range(4)
)


def select_stationary_features(columns: list[str]) -> list[str]:
    """Select engineered stationary features and exclude identifiers/targets."""
    excluded = {
        "timestamp",
        "source",
        "sample_weight",
        "open",
        "high",
        "low",
        "close",
        "volume",
    }
    return [
        column
        for column in STATIONARY_FEATURES
        if column in columns
        and column not in excluded
        and not column.startswith("target_")
    ]


def get_feature_names(columns: list[str] | None = None) -> list[str]:
    """Return the stationary model features, including inferred HMM states."""
    available = set(columns) if columns is not None else None
    names = [
        name
        for name in STATIONARY_FEATURES
        if available is None or name in available
    ]
    names.extend(
        name
        for name in HMM_PROBABILITY_FEATURES
        if available is None or name in available
    )
    return names


@dataclass
class HMMRegimeModel:
    """Fitted scaler, imputation values, and Gaussian HMM."""

    feature_names: list[str]
    medians: np.ndarray
    scaler: StandardScaler
    model: GaussianHMM


def _matrix(
    frame: pl.DataFrame,
    feature_names: list[str],
    medians: np.ndarray,
) -> np.ndarray:
    values = frame.select(feature_names).to_numpy().astype(np.float64)
    invalid = ~np.isfinite(values)
    values[invalid] = np.take(medians, np.where(invalid)[1])
    return values


def fit_hmm(frame: pl.DataFrame) -> HMMRegimeModel:
    """Fit the four-state HMM only on the supplied training frame."""
    feature_names = [name for name in HMM_FEATURES if name in frame.columns]
    if not feature_names:
        raise ValueError("No HMM stationary features are available")
    raw = frame.select(feature_names).to_numpy().astype(np.float64)
    medians = np.nanmedian(np.where(np.isfinite(raw), raw, np.nan), axis=0)
    medians = np.nan_to_num(medians, nan=0.0)
    scaler = StandardScaler()
    train_matrix = scaler.fit_transform(_matrix(frame, feature_names, medians))
    model = GaussianHMM(
        n_components=4,
        covariance_type="diag",
        n_iter=200,
        random_state=42,
    )
    model.fit(train_matrix)
    return HMMRegimeModel(feature_names, medians, scaler, model)


def posterior_probabilities(
    frame: pl.DataFrame,
    fitted: HMMRegimeModel,
) -> np.ndarray:
    """Infer posterior state probabilities without changing the fitted model."""
    matrix = fitted.scaler.transform(
        _matrix(frame, fitted.feature_names, fitted.medians)
    )
    return fitted.model.predict_proba(matrix)


def add_hmm_probabilities(
    frame: pl.DataFrame,
    fitted: HMMRegimeModel,
) -> pl.DataFrame:
    """Add hmm_prob_0 through hmm_prob_3 to a frame."""
    probabilities = posterior_probabilities(frame, fitted)
    return frame.with_columns(
        [
            pl.Series(f"hmm_prob_{index}", probabilities[:, index])
            for index in range(probabilities.shape[1])
        ]
    )
