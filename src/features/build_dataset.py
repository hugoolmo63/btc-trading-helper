"""Build a leak-free hourly machine-learning dataset."""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Final

import polars as pl

LOGGER = logging.getLogger(__name__)

PRICE_PATH: Final[Path] = Path("data/processed/btc_1h.parquet")
ONCHAIN_PATH: Final[Path] = Path("data/processed/onchain_daily.parquet")
MACRO_PATH: Final[Path] = Path("data/processed/macro_daily.parquet")
OUTPUT_PATH: Final[Path] = Path("data/processed/dataset_ml.parquet")
JOIN_TOLERANCE: Final[str] = "7d"
HALVINGS: Final[tuple[datetime, ...]] = (
    datetime(2009, 1, 3, tzinfo=timezone.utc),
    datetime(2012, 11, 28, tzinfo=timezone.utc),
    datetime(2016, 7, 9, tzinfo=timezone.utc),
    datetime(2020, 5, 11, tzinfo=timezone.utc),
    datetime(2024, 4, 20, tzinfo=timezone.utc),
)


def _shift_daily(frame: pl.DataFrame) -> pl.DataFrame:
    return frame.with_columns(
        (pl.col("timestamp") + pl.duration(hours=24)).alias("timestamp")
    ).sort("timestamp")


def _join_daily(
    prices: pl.DataFrame,
    daily: pl.DataFrame,
) -> pl.DataFrame:
    return prices.join_asof(
        _shift_daily(daily),
        on="timestamp",
        strategy="backward",
        tolerance=JOIN_TOLERANCE,
    )


def _days_since_halving() -> pl.Expr:
    timestamp = pl.col("timestamp")
    days = pl.lit(0.0)
    for halving in HALVINGS:
        days = pl.when(timestamp >= pl.lit(halving)).then(
            (timestamp - pl.lit(halving)).dt.total_seconds() / 86_400.0
        ).otherwise(days)
    return days.alias("days_since_halving")


def _rolling_zscore(column: str, window: int) -> pl.Expr:
    value = pl.col(column)
    mean = value.rolling_mean(window_size=window, min_samples=1)
    std = value.rolling_std(window_size=window, min_samples=1)
    return (
        pl.when(std == 0)
        .then(pl.lit(None, dtype=pl.Float64))
        .otherwise((value - mean) / std)
        .alias(f"{column}_zscore_365d")
    )


def build_dataset_from_frames(
    prices: pl.DataFrame,
    onchain: pl.DataFrame,
    macro: pl.DataFrame,
) -> pl.DataFrame:
    """Build features from frames; useful for deterministic leakage tests."""
    data = prices.sort("timestamp")
    data = _join_daily(data, onchain)
    data = _join_daily(data, macro)
    data = data.sort("timestamp")

    data = data.with_columns(_days_since_halving())
    data = data.with_columns(
        (2.0 * math.pi * pl.col("days_since_halving") / 1460.0)
        .sin()
        .alias("cycle_sin"),
        (2.0 * math.pi * pl.col("days_since_halving") / 1460.0)
        .cos()
        .alias("cycle_cos"),
    )

    data = data.with_columns(
        (pl.col("close") / pl.col("close").rolling_mean(33600, min_samples=1) - 1)
        .alias("dist_200w_sma"),
        (pl.col("close") / pl.col("close").rolling_mean(8400, min_samples=1) - 1)
        .alias("dist_50w_sma"),
        (pl.col("close") / pl.col("close").rolling_mean(720, min_samples=1) - 1)
        .alias("dist_30d_sma"),
        (pl.col("HashRate") / pl.col("HashRate").shift(720) - 1)
        .alias("hashrate_change_30d"),
        (pl.col("dgs10") / pl.col("dgs10").shift(2160) - 1)
        .alias("dgs10_change_90d"),
        (pl.col("walcl") / pl.col("walcl").shift(2160) - 1)
        .alias("walcl_change_90d"),
    )
    data = data.with_columns(
        _rolling_zscore("CapMVRVCur", 8760).alias("mvrv_zscore_365d"),
        _rolling_zscore("volume", 8760).alias("vol_zscore_365d"),
        pl.col("hashrate_change_30d")
        .rolling_mean(8760, min_samples=1)
        .alias("_hashrate_mean"),
        pl.col("hashrate_change_30d")
        .rolling_std(8760, min_samples=1)
        .alias("_hashrate_std"),
        pl.col("dgs10_change_90d")
        .rolling_mean(8760, min_samples=1)
        .alias("_dgs10_mean"),
        pl.col("dgs10_change_90d")
        .rolling_std(8760, min_samples=1)
        .alias("_dgs10_std"),
        pl.col("walcl_change_90d")
        .rolling_mean(8760, min_samples=1)
        .alias("_walcl_mean"),
        pl.col("walcl_change_90d")
        .rolling_std(8760, min_samples=1)
        .alias("_walcl_std"),
    ).with_columns(
        pl.when(pl.col("_hashrate_std") == 0)
        .then(pl.lit(None, dtype=pl.Float64))
        .otherwise(
            (pl.col("hashrate_change_30d") - pl.col("_hashrate_mean"))
            / pl.col("_hashrate_std")
        )
        .alias("hashrate_change_30d_zscore_365d"),
        pl.when(pl.col("_dgs10_std") == 0)
        .then(pl.lit(None, dtype=pl.Float64))
        .otherwise(
            (pl.col("dgs10_change_90d") - pl.col("_dgs10_mean"))
            / pl.col("_dgs10_std")
        )
        .alias("dgs10_change_90d_zscore_365d"),
        pl.when(pl.col("_walcl_std") == 0)
        .then(pl.lit(None, dtype=pl.Float64))
        .otherwise(
            (pl.col("walcl_change_90d") - pl.col("_walcl_mean"))
            / pl.col("_walcl_std")
        )
        .alias("walcl_change_90d_zscore_365d"),
    ).drop(
        [
            "_hashrate_mean",
            "_hashrate_std",
            "_dgs10_mean",
            "_dgs10_std",
            "_walcl_mean",
            "_walcl_std",
        ]
    )

    data = data.with_columns(
        *[
            (pl.col("close") / pl.col("close").shift(periods) - 1)
            .log1p()
            .alias(f"log_return_{label}")
            for label, periods in [("1h", 1), ("24h", 24), ("7d", 168), ("30d", 720)]
        ],
    )
    data = data.with_columns(
        pl.col("log_return_1h")
        .rolling_std(720, min_samples=2)
        .alias("realized_vol_30d"),
    )

    gains = pl.col("log_return_1h").clip(lower_bound=0.0)
    losses = (-pl.col("log_return_1h").clip(upper_bound=0.0))
    average_gain = gains.rolling_mean(336, min_samples=1)
    average_loss = losses.rolling_mean(336, min_samples=1)
    data = data.with_columns(
        pl.when(average_loss == 0)
        .then(100.0)
        .otherwise(100.0 - 100.0 / (1.0 + average_gain / average_loss))
        .alias("rsi_14d"),
        pl.when(pl.col("source") == "bitstamp")
        .then(0.35)
        .otherwise(1.0)
        .alias("sample_weight"),
    )

    forward_return = (pl.col("close").shift(-720) / pl.col("close") - 1.0).alias(
        "target_fwd_ret_30d"
    )
    data = data.with_columns(forward_return).with_columns(
        pl.when(pl.col("target_fwd_ret_30d").is_null())
        .then(pl.lit(None, dtype=pl.Int8))
        .otherwise((pl.col("target_fwd_ret_30d") > 0.10).cast(pl.Int8))
        .alias("target_up_10pct_30d")
    )
    return data


def build_dataset(
    price_path: Path = PRICE_PATH,
    onchain_path: Path = ONCHAIN_PATH,
    macro_path: Path = MACRO_PATH,
    output_path: Path = OUTPUT_PATH,
) -> pl.DataFrame:
    """Load processed sources, build features, and write the ML dataset."""
    result = build_dataset_from_frames(
        pl.read_parquet(price_path),
        pl.read_parquet(onchain_path),
        pl.read_parquet(macro_path),
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    result.write_parquet(output_path, compression="zstd")
    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    dataset = build_dataset()
    LOGGER.info(
        "Wrote %d rows, %d columns from %s through %s",
        dataset.height,
        len(dataset.columns),
        dataset["timestamp"][0],
        dataset["timestamp"][-1],
    )
    print(dataset.select(dataset.columns[-15:]).describe())
