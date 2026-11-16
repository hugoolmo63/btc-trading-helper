import sqlite3
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import joblib

from src.execution.paper_trader import PaperTrader, Signal
from src.ops.maintenance import (
    _prune_backups,
    backup_sqlite_db,
)


def test_online_backup_preserves_ledger_while_writer_is_open(tmp_path: Path) -> None:
    database = tmp_path / "paper_trades.db"
    writer = sqlite3.connect(database)
    writer.executescript(
        """
        CREATE TABLE portfolio_state (profile TEXT PRIMARY KEY, btc_balance REAL);
        CREATE TABLE tax_lots (profile TEXT, quantity REAL);
        INSERT INTO portfolio_state VALUES ('cycle_ml_trader', 0.5);
        INSERT INTO tax_lots VALUES ('cycle_ml_trader', 0.25);
        """
    )
    writer.commit()
    backup = backup_sqlite_db(database, tmp_path / "backups")
    with sqlite3.connect(backup) as connection:
        assert connection.execute("SELECT btc_balance FROM portfolio_state").fetchone() == (0.5,)
        assert connection.execute("SELECT quantity FROM tax_lots").fetchone() == (0.25,)
    writer.close()


def test_backup_retention_keeps_recent_daily_and_monthly_snapshots(tmp_path: Path) -> None:
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    for index in range(45):
        timestamp = start + timedelta(days=index + 2)
        (backup_dir / f"paper_trades_{timestamp:%Y%m%d_%H%M%S}.db").touch()
    for index in range(15):
        timestamp = datetime(
            2023 + index // 12, index % 12 + 1, 1, tzinfo=timezone.utc
        )
        (backup_dir / f"paper_trades_{timestamp:%Y%m%d_%H%M%S}.db").touch()

    _prune_backups(backup_dir)

    assert len(list(backup_dir.glob("paper_trades_*.db"))) == 42


def test_atomic_model_replacement_hot_reloads_without_losing_ledger_state(
    tmp_path: Path,
) -> None:
    model_path = tmp_path / "btc_cycle_bundle.joblib"
    joblib.dump({"version": 1}, model_path)
    trader = PaperTrader(tmp_path / "paper.db")
    first = trader.model_bundle(model_path)
    trader.process_signal(
        Signal(datetime(2026, 1, 4, tzinfo=timezone.utc), 100.0, 0.5, 1)
    )

    replacement = tmp_path / "bundle.tmp"
    joblib.dump({"version": 2}, replacement)
    os.replace(replacement, model_path)
    second = trader.model_bundle(model_path)

    assert first["version"] == 1
    assert second["version"] == 2
    with sqlite3.connect(tmp_path / "paper.db") as connection:
        assert connection.execute(
            "SELECT total_contributed_usd FROM portfolio_state WHERE profile = 'cycle_ml_trader'"
        ).fetchone() == (100.0,)
