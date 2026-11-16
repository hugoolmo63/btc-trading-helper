"""Download and normalize Coin Metrics daily Bitcoin on-chain metrics."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Final

import polars as pl
import requests

LOGGER = logging.getLogger(__name__)
URL: Final[str] = "https://raw.githubusercontent.com/coinmetrics/data/master/csv/btc.csv"
ACTIVE_URL: Final[str] = os.getenv(
    "ONCHAIN_ACTIVE_URL",
    "https://bitcoin-data.com/v1/mvrv",
)
OUTPUT_PATH: Final[Path] = Path("data/processed/onchain_daily.parquet")
RAW_PATH: Final[Path] = Path("data/raw/onchain/btc.csv")
METRICS: Final[list[str]] = [
    "CapMVRVCur",
    "CapRealUSD",
    "PriceUSD",
    "AdrActCnt",
    "HashRate",
    "FeeTotUSD",
    "TxCnt",
]


def _active_frame(payload: bytes) -> pl.DataFrame:
    """Normalize a current public-provider JSON response when available."""
    parsed = requests.models.complexjson.loads(payload)
    if isinstance(parsed, dict):
        parsed = parsed.get("data", parsed.get("results", []))
    if not isinstance(parsed, list) or not parsed:
        raise ValueError("Active on-chain response has no rows")
    frame = pl.DataFrame(parsed, infer_schema_length=None)
    aliases = {
        "date": "time",
        "timestamp": "time",
        "mvrv": "CapMVRVCur",
        "mvrv_ratio": "CapMVRVCur",
        "price": "PriceUSD",
        "price_usd": "PriceUSD",
        "hashrate": "HashRate",
        "tx_count": "TxCnt",
        "active_addresses": "AdrActCnt",
    }
    for source, target in aliases.items():
        if source in frame.columns and target not in frame.columns:
            frame = frame.rename({source: target})
    if "time" not in frame.columns or "CapMVRVCur" not in frame.columns:
        raise ValueError("Active on-chain response lacks time and MVRV fields")
    for metric in METRICS:
        if metric not in frame.columns:
            frame = frame.with_columns(pl.lit(None, dtype=pl.Float64).alias(metric))
    return (
        frame.select(["time", *METRICS])
        .with_columns(
            pl.col("time").cast(pl.String).str.to_datetime(
                time_unit="us", time_zone="UTC", strict=False
            ).alias("timestamp"),
            *[
                pl.col(metric).cast(pl.Float64, strict=False)
                for metric in METRICS
            ],
        )
        .select(["timestamp", *METRICS])
    )


def _download_active(client: requests.Session) -> pl.DataFrame | None:
    try:
        response = client.get(ACTIVE_URL, timeout=60)
        response.raise_for_status()
        return _active_frame(response.content)
    except (OSError, requests.RequestException, ValueError) as error:
        LOGGER.warning("Active on-chain provider unavailable: %s", error)
        return None


def download_onchain(
    output_path: Path = OUTPUT_PATH,
    session: requests.Session | None = None,
    refresh: bool = False,
) -> pl.DataFrame:
    """Download, type, deduplicate, and write daily on-chain metrics."""
    client = session or requests.Session()
    if RAW_PATH.exists() and not refresh:
        payload = RAW_PATH.read_bytes()
        LOGGER.info("Using cached Coin Metrics data")
    else:
        response = client.get(URL, timeout=300)
        response.raise_for_status()
        payload = response.content
        RAW_PATH.parent.mkdir(parents=True, exist_ok=True)
        RAW_PATH.write_bytes(payload)
        LOGGER.info("Downloaded Coin Metrics data")
    raw = pl.read_csv(
        payload,
        infer_schema=False,
        null_values=["", "NaN", "nan"],
    )
    source_columns = [
        "time",
        "CapMVRVCur",
        "CapMrktCurUSD",
        "PriceUSD",
        "AdrActCnt",
        "HashRate",
        "FeeTotNtv",
        "TxCnt",
    ]
    missing = [column for column in source_columns if column not in raw.columns]
    if missing:
        raise ValueError(f"Coin Metrics response is missing columns: {missing}")

    historical = (
        raw.select(source_columns)
        .with_columns(
            pl.col("time")
            .str.to_datetime(time_unit="us", time_zone="UTC")
            .alias("timestamp"),
            *[
                pl.col(column).cast(pl.Float64, strict=False)
                for column in source_columns
                if column != "time"
            ],
        )
        .with_columns(
            (pl.col("CapMrktCurUSD") / pl.col("CapMVRVCur")).alias("CapRealUSD"),
            (pl.col("FeeTotNtv") * pl.col("PriceUSD")).alias("FeeTotUSD"),
        )
        .select(["timestamp", *METRICS])
        .unique(subset=["timestamp"], keep="last")
        .sort("timestamp")
    )
    active = _download_active(client)
    result = historical
    if active is not None and not active.is_empty():
        result = (
            pl.concat([historical, active], how="diagonal_relaxed")
            .unique(subset=["timestamp"], keep="last")
            .sort("timestamp")
        )
    if result.is_empty() or result["timestamp"].null_count():
        raise ValueError("On-chain data is empty or has invalid timestamps")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    result.write_parquet(output_path, compression="zstd")
    LOGGER.info("Wrote %d on-chain rows", result.height)
    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    download_onchain()
