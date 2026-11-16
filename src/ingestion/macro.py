"""Download and normalize daily FRED liquidity and macro series."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Final

import polars as pl
import requests

LOGGER = logging.getLogger(__name__)
OUTPUT_PATH: Final[Path] = Path("data/processed/macro_daily.parquet")
RAW_DIR: Final[Path] = Path("data/raw/macro")
SERIES: Final[dict[str, str]] = {
    "walcl": "WALCL",
    "dtwexbgs": "DTWEXBGS",
    "dgs10": "DGS10",
    "eurusd": "DEXUSEU",
}


def download_macro(
    output_path: Path = OUTPUT_PATH,
    session: requests.Session | None = None,
    refresh: bool = False,
) -> pl.DataFrame:
    """Fetch FRED series and write one UTC-indexed daily table."""
    client = session or requests.Session()
    frames: list[pl.DataFrame] = []
    for column, series_id in SERIES.items():
        raw_path = RAW_DIR / f"{series_id}.csv"
        if raw_path.exists() and not refresh:
            payload = raw_path.read_bytes()
            LOGGER.info("Using cached FRED %s data", series_id)
        else:
            response = client.get(
                f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}",
                timeout=120,
            )
            response.raise_for_status()
            payload = response.content
            RAW_DIR.mkdir(parents=True, exist_ok=True)
            raw_path.write_bytes(payload)
            LOGGER.info("Downloaded FRED %s data", series_id)
        frame = (
            pl.read_csv(payload, null_values=["", ".", "NaN", "nan"])
            .rename({"observation_date": "timestamp", series_id: column})
            .with_columns(
                pl.col("timestamp")
                .str.to_datetime(time_unit="us", time_zone="UTC"),
                pl.col(column).cast(pl.Float64, strict=False),
            )
            .select(["timestamp", column])
        )
        frames.append(frame)

    result = frames[0]
    for frame in frames[1:]:
        result = result.join(frame, on="timestamp", how="full", coalesce=True)
    result = result.sort("timestamp")
    if result.is_empty() or result["timestamp"].null_count():
        raise ValueError("Macro data is empty or has invalid timestamps")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    result.write_parquet(output_path, compression="zstd")
    LOGGER.info("Wrote %d macro rows", result.height)
    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    download_macro()
