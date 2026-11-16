"""Online SQLite backups and atomic monthly model retraining."""

from __future__ import annotations

import argparse
import logging
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Final

import joblib
import numpy as np

LOGGER = logging.getLogger(__name__)
ROOT: Final[Path] = Path(__file__).resolve().parents[2]
DB_PATH: Final[Path] = ROOT / "data/paper_trades.db"
BACKUP_DIR: Final[Path] = ROOT / "data/backups"
MODEL_PATH: Final[Path] = ROOT / "models/btc_cycle_bundle.joblib"
BACKUP_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^paper_trades_(\d{8}_\d{6})\.db$"
)


def _backup_timestamp(path: Path) -> datetime | None:
    match = BACKUP_PATTERN.match(path.name)
    if not match:
        return None
    return datetime.strptime(match.group(1), "%Y%m%d_%H%M%S").replace(
        tzinfo=timezone.utc
    )


def _prune_backups(backup_dir: Path) -> None:
    dated = [
        (path, timestamp)
        for path in backup_dir.glob("paper_trades_*.db")
        if (timestamp := _backup_timestamp(path)) is not None
    ]
    monthly = sorted(
        ((path, timestamp) for path, timestamp in dated if timestamp.day == 1),
        key=lambda item: item[1],
        reverse=True,
    )
    daily = sorted(
        ((path, timestamp) for path, timestamp in dated if timestamp.day != 1),
        key=lambda item: item[1],
        reverse=True,
    )
    keep = {path for path, _ in monthly[:12]} | {
        path for path, _ in daily[:30]
    }
    for path, _ in dated:
        if path not in keep:
            path.unlink()


def latest_backup_timestamp(backup_dir: Path = BACKUP_DIR) -> str | None:
    timestamps = [
        timestamp
        for path in backup_dir.glob("paper_trades_*.db")
        if (timestamp := _backup_timestamp(path)) is not None
    ]
    return max(timestamps).isoformat().replace("+00:00", "Z") if timestamps else None


def backup_sqlite_db(
    db_path: Path = DB_PATH,
    backup_dir: Path = BACKUP_DIR,
) -> Path:
    """Create a consistent online backup and prune expired snapshots."""
    if not db_path.exists():
        raise FileNotFoundError(f"SQLite database does not exist: {db_path}")
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    destination = backup_dir / f"paper_trades_{timestamp}.db"
    with sqlite3.connect(db_path) as source:
        with sqlite3.connect(destination) as target:
            source.backup(target)
            target.commit()
    _prune_backups(backup_dir)
    LOGGER.info("Created SQLite backup: %s", destination)
    return destination


def _validate_bundle(bundle_path: Path) -> dict[str, object]:
    bundle = joblib.load(bundle_path)
    required = {
        "model",
        "hmm",
        "hmm_scaler",
        "hmm_medians",
        "hmm_feature_names",
        "feature_names",
        "model_feature_medians",
    }
    missing = required - bundle.keys()
    if missing:
        raise ValueError(f"Model bundle is missing keys: {sorted(missing)}")
    features = list(bundle["feature_names"])
    medians = bundle["model_feature_medians"]
    if not features or any(name not in medians for name in features):
        raise ValueError("Model bundle has incomplete feature medians")
    matrix = np.asarray([[float(medians[name]) for name in features]])
    probabilities = bundle["model"].predict_proba(matrix)
    if probabilities.shape != (1, 2) or not np.isfinite(probabilities).all():
        raise ValueError("Model quality gate produced invalid probabilities")
    hmm_features = list(bundle["hmm_feature_names"])
    hmm_medians = np.asarray(bundle["hmm_medians"], dtype=np.float64)
    if not hmm_features or hmm_medians.shape != (len(hmm_features),):
        raise ValueError("Model bundle has invalid HMM schema")
    hmm_values = bundle["hmm_scaler"].transform(
        np.zeros((1, len(hmm_features)), dtype=np.float64)
    )
    hmm_probabilities = bundle["hmm"].predict_proba(hmm_values)
    if not np.isfinite(hmm_probabilities).all():
        raise ValueError("HMM quality gate produced invalid probabilities")
    return bundle


def retrain_bundle_atomic(bundle_path: Path = MODEL_PATH) -> Path:
    """Refresh data, train a temporary bundle, validate, and atomically publish."""
    from src.execution.paper_trader import update_live_data
    from src.models.train_lgbm import train_production_bundle

    update_live_data()
    temporary = bundle_path.with_name(f"{bundle_path.name}.tmp")
    try:
        train_production_bundle(model_path=temporary)
        _validate_bundle(temporary)
        os.replace(temporary, bundle_path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    LOGGER.info("Published validated model bundle: %s", bundle_path)
    return bundle_path


def run_daemon() -> None:
    """Run daily backup and first-of-month retraining in UTC."""
    last_backup_date: datetime.date | None = None
    last_retrain_month: tuple[int, int] | None = None
    while True:
        now = datetime.now(timezone.utc)
        if last_backup_date != now.date() and now.hour >= 1:
            backup_sqlite_db()
            last_backup_date = now.date()
        month = (now.year, now.month)
        if (
            now.day == 1
            and (now.hour, now.minute) >= (2, 30)
            and last_retrain_month != month
        ):
            retrain_bundle_atomic()
            last_retrain_month = month
        time.sleep(30)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup-now", action="store_true")
    parser.add_argument("--retrain-now", action="store_true")
    parser.add_argument("--daemon", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if args.backup_now:
        backup_sqlite_db()
    if args.retrain_now:
        retrain_bundle_atomic()
    if args.daemon:
        run_daemon()
    if not (args.backup_now or args.retrain_now or args.daemon):
        parser.error("choose --backup-now, --retrain-now, or --daemon")


if __name__ == "__main__":
    main()
