import csv
import io
import zipfile
from datetime import datetime, timezone

import polars as pl

from src.ingestion.binance_bulk import _read_month, _validate


def test_read_month_normalizes_millisecond_and_microsecond_epochs() -> None:
    rows = [
        [
            1501545600000,
            "4000.00",
            "4100.00",
            "3900.00",
            "4050.00",
            "1.25",
            1501549199999,
            "5000.00",
            120,
            "0.60",
            "2400.00",
            "0",
        ],
        [
            1735689600000000,
            "90000.00",
            "91000.00",
            "89000.00",
            "90500.00",
            "2.50",
            1735693199999999,
            "225000.00",
            240,
            "1.20",
            "108000.00",
            "0",
        ],
    ]
    raw = io.StringIO()
    csv.writer(raw).writerows(rows)
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zip_file:
        zip_file.writestr("BTCUSDT-1h-2017-08.csv", raw.getvalue())

    frame = _read_month(archive.getvalue(), datetime(2017, 8, 1, tzinfo=timezone.utc))
    _validate(frame)

    assert frame["timestamp"].to_list() == [
        datetime(2017, 8, 1, tzinfo=timezone.utc),
        datetime(2025, 1, 1, tzinfo=timezone.utc),
    ]
    assert frame["close_timestamp"].to_list() == [
        datetime(2017, 8, 1, 0, 59, 59, 999000, tzinfo=timezone.utc),
        datetime(2025, 1, 1, 0, 59, 59, 999999, tzinfo=timezone.utc),
    ]
    assert frame.schema["timestamp"] == pl.Datetime("us", "UTC")
    assert frame.schema["close"] == pl.Float64
