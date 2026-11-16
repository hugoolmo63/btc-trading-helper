"""Backfill pre-Binance BTC/USD hourly candles from Bitstamp minute data."""

from __future__ import annotations

import gzip
import io
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Final

import polars as pl
import requests

from src.ingestion.binance_bulk import OUTPUT_PATH as BINANCE_OUTPUT_PATH

LOGGER = logging.getLogger(__name__)

URL: Final[str] = (
    "https://raw.githubusercontent.com/ff137/bitstamp-btcusd-minute-data/main/"
    "data/historical/btcusd_bitstamp_1min_2012-2025.csv.gz"
)
BOUNDARY: Final[datetime] = datetime(2017, 8, 17, 4, tzinfo=timezone.utc)
OUTPUT_PATH: Final[Path] = BINANCE_OUTPUT_PATH
RAW_PATH: Final[Path] = Path("data/raw/bitstamp/btcusd_bitstamp_1min_2012-2025.csv.gz")
PRICE_COLUMNS: Final[list[str]] = [
    "timestamp",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_timestamp",
    "quote_asset_volume",
    "number_of_trades",
    "taker_buy_base_volume",
    "taker_buy_quote_volume",
    "source",
]


def _read_bitstamp(payload: bytes) -> pl.DataFrame:
    """Parse and resample the compressed minute dataset in memory."""
    with gzip.GzipFile(fileobj=io.BytesIO(payload)) as compressed:
        minute_data = pl.read_csv(
            compressed,
            schema_overrides={
                "timestamp": pl.Int64,
                "open": pl.Float64,
                "high": pl.Float64,
                "low": pl.Float64,
                "close": pl.Float64,
                "volume": pl.Float64,
            },
        )

    return (
        minute_data.with_columns(
            pl.from_epoch("timestamp", time_unit="s")
            .dt.replace_time_zone("UTC")
            .cast(pl.Datetime("us", "UTC"))
            .alias("timestamp")
        )
        .sort("timestamp")
        .group_by_dynamic(
            "timestamp",
            every="1h",
            period="1h",
            closed="left",
            label="left",
        )
        .agg(
            pl.col("open").first(),
            pl.col("high").max(),
            pl.col("low").min(),
            pl.col("close").last(),
            pl.col("volume").sum(),
        )
        .with_columns(
            (pl.col("timestamp") + pl.duration(hours=1)).alias("close_timestamp"),
            pl.lit(None, dtype=pl.Float64).alias("quote_asset_volume"),
            pl.lit(None, dtype=pl.Int64).alias("number_of_trades"),
            pl.lit(None, dtype=pl.Float64).alias("taker_buy_base_volume"),
            pl.lit(None, dtype=pl.Float64).alias("taker_buy_quote_volume"),
            pl.lit("bitstamp").alias("source"),
        )
        .select(PRICE_COLUMNS)
    )


def stitch_price_history(
    bitstamp: pl.DataFrame,
    binance: pl.DataFrame,
    boundary: datetime = BOUNDARY,
) -> pl.DataFrame:
    """Prepend Bitstamp bars before the Binance stitch boundary."""
    bitstamp_pre_boundary = bitstamp.filter(pl.col("timestamp") < boundary)
    binance_from_boundary = binance.filter(pl.col("timestamp") >= boundary)
    if bitstamp_pre_boundary.select("timestamp").join(
        binance_from_boundary.select("timestamp"), on="timestamp", how="inner"
    ).height:
        raise ValueError("Bitstamp and Binance data overlap at the stitch boundary")

    result = (
        pl.concat([bitstamp_pre_boundary, binance_from_boundary], how="vertical_relaxed")
        .unique(subset=["timestamp"], keep="last")
        .sort("timestamp")
    )
    if result["timestamp"].is_empty() or result["timestamp"].is_duplicated().any():
        raise ValueError("Stitched price history is empty or contains duplicates")
    return result


def download_backfill(
    output_path: Path = OUTPUT_PATH,
    session: requests.Session | None = None,
) -> pl.DataFrame:
    """Download Bitstamp data, stitch it to Binance, and write the result."""
    client = session or requests.Session()
    if RAW_PATH.exists():
        payload = RAW_PATH.read_bytes()
        LOGGER.info("Using cached Bitstamp archive")
    else:
        response = client.get(URL, timeout=300)
        response.raise_for_status()
        payload = response.content
        RAW_PATH.parent.mkdir(parents=True, exist_ok=True)
        RAW_PATH.write_bytes(payload)
        LOGGER.info("Downloaded Bitstamp archive")
    bitstamp = _read_bitstamp(payload)
    if not output_path.exists():
        raise FileNotFoundError(f"Binance parquet not found: {output_path}")
    binance = pl.read_parquet(output_path)
    result = stitch_price_history(bitstamp, binance)
    result.write_parquet(output_path, compression="zstd")
    LOGGER.info(
        "Wrote %d stitched rows from %s through %s",
        result.height,
        result["timestamp"][0],
        result["timestamp"][-1],
    )
    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    download_backfill()
