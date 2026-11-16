from datetime import datetime, timedelta, timezone

import polars as pl
from polars.testing import assert_frame_equal

from src.features.build_dataset import _join_daily, build_dataset_from_frames


def _sources() -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    start = datetime(2020, 5, 1, tzinfo=timezone.utc)
    timestamps = [start + timedelta(hours=index) for index in range(48)]
    prices = pl.DataFrame(
        {
            "timestamp": timestamps,
            "open": [100.0 + index for index in range(48)],
            "high": [101.0 + index for index in range(48)],
            "low": [99.0 + index for index in range(48)],
            "close": [100.0 + index for index in range(48)],
            "volume": [10.0 + index for index in range(48)],
            "source": ["binance"] * 48,
        }
    ).with_columns(pl.col("timestamp").cast(pl.Datetime("us", "UTC")))
    daily_timestamps = [start - timedelta(days=2), start + timedelta(days=1)]
    onchain = pl.DataFrame(
        {
            "timestamp": daily_timestamps,
            "CapMVRVCur": [1.0, 2.0],
            "CapRealUSD": [100.0, 200.0],
            "PriceUSD": [100.0, 200.0],
            "AdrActCnt": [1.0, 2.0],
            "HashRate": [10.0, 20.0],
            "FeeTotUSD": [1.0, 2.0],
            "TxCnt": [10.0, 20.0],
        }
    ).with_columns(pl.col("timestamp").cast(pl.Datetime("us", "UTC")))
    macro = pl.DataFrame(
        {
            "timestamp": daily_timestamps,
            "walcl": [100.0, 200.0],
            "dtwexbgs": [100.0, 101.0],
            "dgs10": [1.0, 2.0],
        }
    ).with_columns(pl.col("timestamp").cast(pl.Datetime("us", "UTC")))
    return prices, onchain, macro


def test_future_source_changes_do_not_change_prior_features() -> None:
    prices, onchain, macro = _sources()
    baseline = build_dataset_from_frames(prices, onchain, macro)
    cutoff = prices["timestamp"][24]

    changed_prices = prices.with_columns(
        pl.when(pl.col("timestamp") > cutoff)
        .then(pl.col("close") * 100)
        .otherwise(pl.col("close"))
        .alias("close")
    )
    changed_onchain = onchain.with_columns(
        pl.when(pl.col("timestamp") > cutoff)
        .then(pl.col("CapMVRVCur") * 100)
        .otherwise(pl.col("CapMVRVCur"))
        .alias("CapMVRVCur")
    )
    changed_macro = macro.with_columns(
        pl.when(pl.col("timestamp") > cutoff)
        .then(pl.col("dgs10") * 100)
        .otherwise(pl.col("dgs10"))
        .alias("dgs10")
    )
    changed = build_dataset_from_frames(
        changed_prices, changed_onchain, changed_macro
    )

    feature_columns = [
        column
        for column in baseline.columns
        if not column.startswith("target_")
    ]
    assert_frame_equal(
        baseline.filter(pl.col("timestamp") <= cutoff).select(feature_columns),
        changed.filter(pl.col("timestamp") <= cutoff).select(feature_columns),
        check_exact=True,
    )


def test_daily_join_drops_observations_older_than_seven_days() -> None:
    timestamp = datetime(2026, 11, 16, 12, tzinfo=timezone.utc)
    prices = pl.DataFrame({"timestamp": [timestamp]}).with_columns(
        pl.col("timestamp").cast(pl.Datetime("us", "UTC"))
    )
    daily = pl.DataFrame(
        {
            "timestamp": [
                timestamp - timedelta(days=9),
            ],
            "CapMVRVCur": [1.0],
        }
    ).with_columns(pl.col("timestamp").cast(pl.Datetime("us", "UTC")))

    joined = _join_daily(prices, daily)
    assert joined["CapMVRVCur"][0] is None
