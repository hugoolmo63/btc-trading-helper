"""Download and consolidate Binance monthly BTCUSDT 1-hour klines."""

from __future__ import annotations

import io
import logging
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Final

import polars as pl
import requests

LOGGER = logging.getLogger(__name__)

BASE_URL: Final[str] = (
    "https://data.binance.vision/data/spot/monthly/klines/BTCUSDT/1h"
)
OUTPUT_PATH: Final[Path] = Path("data/processed/btc_1h.parquet")
RAW_DIR: Final[Path] = Path("data/raw/binance")
START_YEAR: Final[int] = 2017
START_MONTH: Final[int] = 8
KLINE_COLUMNS: Final[list[str]] = [
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time",
    "quote_asset_volume",
    "number_of_trades",
    "taker_buy_base_volume",
    "taker_buy_quote_volume",
    "ignore",
]
FLOAT_COLUMNS: Final[list[str]] = [
    "open",
    "high",
    "low",
    "close",
    "volume",
    "quote_asset_volume",
    "taker_buy_base_volume",
    "taker_buy_quote_volume",
]
MICROSECOND_EPOCH_THRESHOLD: Final[int] = 100_000_000_000_000


def _epoch_to_utc(column: str) -> pl.Expr:
    """Normalize millisecond or microsecond epochs to UTC microseconds."""
    raw_epoch = pl.col(column)
    return (
        pl.when(raw_epoch >= MICROSECOND_EPOCH_THRESHOLD)
        .then(pl.from_epoch(raw_epoch, time_unit="us"))
        .otherwise(pl.from_epoch(raw_epoch, time_unit="ms"))
        .dt.replace_time_zone("UTC")
        .cast(pl.Datetime("us", "UTC"))
    )


def _month_starts(
    start_year: int = START_YEAR,
    start_month: int = START_MONTH,
    now: datetime | None = None,
) -> list[datetime]:
    """Return UTC month starts through the most recently completed month."""
    current = now or datetime.now(timezone.utc)
    end_year, end_month = current.year, current.month - 1
    if end_month == 0:
        end_year, end_month = end_year - 1, 12

    month = datetime(start_year, start_month, 1, tzinfo=timezone.utc)
    end = datetime(end_year, end_month, 1, tzinfo=timezone.utc)
    months: list[datetime] = []
    while month <= end:
        months.append(month)
        if month.month == 12:
            month = datetime(month.year + 1, 1, 1, tzinfo=timezone.utc)
        else:
            month = datetime(month.year, month.month + 1, 1, tzinfo=timezone.utc)
    return months


def _read_month(payload: bytes, month: datetime) -> pl.DataFrame:
    """Read one Binance ZIP archive directly from memory."""
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        csv_names = [name for name in archive.namelist() if name.endswith(".csv")]
        if len(csv_names) != 1:
            raise ValueError(f"Expected one CSV in {month:%Y-%m}, found {csv_names}")
        with archive.open(csv_names[0]) as csv_file:
            frame = pl.read_csv(
                csv_file,
                has_header=False,
                new_columns=KLINE_COLUMNS,
                schema_overrides={
                    "open_time": pl.Int64,
                    "close_time": pl.Int64,
                    "number_of_trades": pl.Int64,
                    **{column: pl.Float64 for column in FLOAT_COLUMNS},
                    "ignore": pl.String,
                },
            )

    return (
        frame.with_columns(
            _epoch_to_utc("open_time").alias("timestamp"),
            _epoch_to_utc("close_time").alias("close_timestamp"),
            pl.lit("binance").alias("source"),
        )
        .drop(["open_time", "close_time", "ignore"])
        .select(
            [
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
        )
    )


def download_klines(
    output_path: Path = OUTPUT_PATH,
    session: requests.Session | None = None,
) -> pl.DataFrame:
    """Download, validate, deduplicate, and write all available monthly klines."""
    client = session or requests.Session()
    frames: list[pl.DataFrame] = []
    for month in _month_starts():
        url = f"{BASE_URL}/BTCUSDT-1h-{month:%Y-%m}.zip"
        raw_path = RAW_DIR / f"BTCUSDT-1h-{month:%Y-%m}.zip"
        if raw_path.exists():
            payload = raw_path.read_bytes()
            LOGGER.info("Using cached %s", month.strftime("%Y-%m"))
        else:
            response = client.get(url, timeout=60)
            if response.status_code == requests.codes.not_found:
                LOGGER.warning("Monthly archive not found: %s", url)
                continue
            response.raise_for_status()
            payload = response.content
            RAW_DIR.mkdir(parents=True, exist_ok=True)
            raw_path.write_bytes(payload)
            LOGGER.info("Downloaded %s", month.strftime("%Y-%m"))
        frames.append(_read_month(payload, month))

    if not frames:
        raise RuntimeError("No Binance monthly kline archives were downloaded")

    result = (
        pl.concat(frames, how="vertical_relaxed")
        .unique(subset=["timestamp"], keep="last")
        .sort("timestamp")
    )
    _validate(result)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    result.write_parquet(output_path, compression="zstd")
    return result


def _validate(frame: pl.DataFrame) -> None:
    """Perform basic schema, ordering, uniqueness, and UTC checks."""
    expected = {
        "timestamp": pl.Datetime("us", "UTC"),
        "open": pl.Float64,
        "high": pl.Float64,
        "low": pl.Float64,
        "close": pl.Float64,
        "volume": pl.Float64,
        "close_timestamp": pl.Datetime("us", "UTC"),
        "quote_asset_volume": pl.Float64,
        "number_of_trades": pl.Int64,
        "taker_buy_base_volume": pl.Float64,
        "taker_buy_quote_volume": pl.Float64,
        "source": pl.String,
    }
    if frame.schema != expected:
        raise ValueError(f"Unexpected output schema: {frame.schema}")
    if frame.is_empty():
        raise ValueError("Consolidated kline data is empty")
    if frame["timestamp"].n_unique() != frame.height:
        raise ValueError("Duplicate kline timestamps remain")
    if not frame["timestamp"].is_sorted():
        raise ValueError("Kline timestamps are not sorted")
    if frame["timestamp"].null_count() or frame["close"].null_count():
        raise ValueError("Required kline values contain nulls")

    timestamps = frame["timestamp"]
    if timestamps[0] < datetime(START_YEAR, START_MONTH, 1, tzinfo=timezone.utc):
        raise ValueError("Data starts before the configured start month")
    latest = timestamps[-1]
    if latest.minute != 0 or latest.second != 0 or latest.microsecond != 0:
        raise ValueError("Kline timestamps are not aligned to whole hours")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    data = download_klines()
    LOGGER.info(
        "Validated %d rows from %s through %s",
        data.height,
        data["timestamp"][0],
        data["timestamp"][-1],
    )
