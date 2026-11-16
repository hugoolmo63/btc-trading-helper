from datetime import datetime

import polars as pl
from src.ingestion.bitstamp_backfill import BOUNDARY, stitch_price_history


def _price_frame(timestamps: list[datetime], source: str) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "timestamp": pl.Series(timestamps, dtype=pl.Datetime("us", "UTC")),
            "open": [1.0] * len(timestamps),
            "high": [1.0] * len(timestamps),
            "low": [1.0] * len(timestamps),
            "close": [1.0] * len(timestamps),
            "volume": [1.0] * len(timestamps),
            "close_timestamp": pl.Series(
                timestamps, dtype=pl.Datetime("us", "UTC")
            ),
            "quote_asset_volume": [None] * len(timestamps),
            "number_of_trades": [None] * len(timestamps),
            "taker_buy_base_volume": [None] * len(timestamps),
            "taker_buy_quote_volume": [None] * len(timestamps),
            "source": [source] * len(timestamps),
        }
    )


def test_stitch_has_zero_overlap_at_boundary() -> None:
    before = BOUNDARY.replace(hour=3)
    bitstamp = _price_frame([before, BOUNDARY], "bitstamp")
    binance = _price_frame([BOUNDARY, BOUNDARY.replace(hour=5)], "binance")

    stitched = stitch_price_history(bitstamp, binance)

    assert stitched["timestamp"].to_list() == [before, BOUNDARY, BOUNDARY.replace(hour=5)]
    assert stitched["source"].to_list() == ["bitstamp", "binance", "binance"]
    assert stitched["timestamp"].is_duplicated().sum() == 0
