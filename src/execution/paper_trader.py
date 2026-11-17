"""Live paper-trading execution for DCA and cycle-ML profiles."""

from __future__ import annotations

import argparse
import calendar
import logging
import math
import os
import sqlite3
import threading
from uuid import uuid4
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Final
from zoneinfo import ZoneInfo

import joblib
import numpy as np
import polars as pl

from src.features.build_dataset import build_dataset
from src.ingestion.macro import download_macro
from src.ingestion.onchain import download_onchain
from src.execution.tax import (
    SpanishIRPFLedger,
    spanish_irpf_marginal_rate,
    spanish_irpf_next_bracket,
    spanish_irpf_tax,
)

LOGGER = logging.getLogger(__name__)
PRICE_PATH: Final[Path] = Path("data/processed/btc_1h.parquet")
DATASET_PATH: Final[Path] = Path("data/processed/dataset_ml.parquet")
MODEL_PATH: Final[Path] = Path("models/btc_cycle_bundle.joblib")
LEDGER_PATH: Final[Path] = Path("data/paper_trades.db")
PROFILES: Final[tuple[str, str]] = ("dca_holder", "cycle_ml_trader")
WEEKLY_DEPOSIT_EUR: Final[float] = 100.0
FEE_RATE: Final[float] = 0.001
RISK_FREE_APY: Final[float] = float(os.getenv("RISK_FREE_APY", "0.025"))
DEFAULT_TAX_RATE: Final[float] = 0.20
COOLDOWN_HOURS: Final[int] = 168
DEFAULT_DCA_SCHEDULE: Final[dict[str, object]] = {
    "enabled": True,
    "frequency": "weekly",
    "day_of_week": 6,
    "day_of_month": 1,
    "amount_eur": 100.0,
    "last_executed_period_key": None,
}
_BUNDLE_CACHE: dict[Path, tuple[int, dict[str, object]]] = {}
_BUNDLE_LOCK = threading.RLock()


def load_model_bundle(model_path: Path = MODEL_PATH) -> dict[str, object]:
    """Load a bundle once and hot-reload it after an atomic replacement."""
    mtime_ns = model_path.stat().st_mtime_ns
    with _BUNDLE_LOCK:
        cached = _BUNDLE_CACHE.get(model_path)
        if cached is None or cached[0] != mtime_ns:
            _BUNDLE_CACHE[model_path] = (mtime_ns, joblib.load(model_path))
        return _BUNDLE_CACHE[model_path][1]


def model_mtime_ns(model_path: Path = MODEL_PATH) -> int | None:
    """Return the current model artifact mtime for operational reporting."""
    try:
        return model_path.stat().st_mtime_ns
    except FileNotFoundError:
        return None


def net_btc_equivalent(
    btc_balance: float,
    cash_usd: float,
    btc_price_usd: float,
    tax_escrow_eur: float = 0.0,
    eurusd: float = 1.0,
) -> float:
    """Value BTC plus spendable cash, excluding ring-fenced tax escrow."""
    spendable_cash = max(0.0, cash_usd - tax_escrow_eur * eurusd)
    return btc_balance + spendable_cash / btc_price_usd


def live_eurusdt_rate(
    macro_path: Path = Path("data/processed/macro_daily.parquet"),
) -> float:
    """Return EURUSDT, preferring Binance public spot data."""
    try:
        import ccxt
        ticker = ccxt.binance({"enableRateLimit": True}).fetch_ticker("EUR/USDT")
        rate = float(ticker.get("last") or ticker.get("close") or 0.0)
        if rate > 0:
            return rate
    except ImportError:
        LOGGER.warning("ccxt unavailable; using latest FRED FX value")
    except (OSError, RuntimeError, ValueError, ccxt.BaseError):
        LOGGER.warning("Live EURUSDT ticker unavailable; using latest FRED FX value")
    if macro_path.exists():
        frame = pl.read_parquet(macro_path).select("eurusd").drop_nulls()
        if not frame.is_empty():
            rate = float(frame["eurusd"][-1])
            if rate > 0:
                return rate
    raise RuntimeError("No valid EURUSDT or DEXUSEU FX rate is available")


def allocation_for_probability(
    probability: float,
    previous_weight: float = 0.20,
) -> float:
    """Return a hysteretic target allocation with a hard 20% BTC floor."""
    previous_weight = max(0.20, min(1.0, previous_weight))
    if probability >= 0.65:
        return 1.0
    if previous_weight >= 1.0 and probability < 0.55:
        return 0.5
    if previous_weight >= 0.5 and probability < 0.40:
        return 0.20
    if previous_weight <= 0.20 and probability >= 0.48:
        return 0.5
    return previous_weight


@dataclass
class FIFOTaxLotQueue:
    """Deterministic FIFO BTC lots for realized-gain tax accounting."""

    lots: list[tuple[datetime, float, float]] = field(default_factory=list)
    unit_costs_eur: list[float] = field(default_factory=list)

    def buy(
        self,
        acquired_at: datetime,
        quantity: float,
        unit_cost: float,
        unit_cost_eur: float | None = None,
    ) -> None:
        if quantity > 0:
            self.lots.append((acquired_at, quantity, unit_cost))
            self.unit_costs_eur.append(unit_cost_eur if unit_cost_eur is not None else unit_cost)

    def sell(
        self,
        quantity: float,
        price: float,
        tax_rate: float = DEFAULT_TAX_RATE,
    ) -> tuple[float, float]:
        if quantity < 0 or quantity > sum(lot[1] for lot in self.lots) + 1e-12:
            raise ValueError("FIFO sale exceeds available tax-lot quantity")
        remaining = quantity
        gain = 0.0
        updated: list[tuple[datetime, float, float]] = []
        updated_eur: list[float] = []
        for index, (acquired_at, lot_quantity, unit_cost) in enumerate(self.lots):
            consumed = min(remaining, lot_quantity)
            gain += consumed * (price - unit_cost)
            lot_quantity -= consumed
            remaining -= consumed
            if lot_quantity > 1e-12:
                updated.append((acquired_at, lot_quantity, unit_cost))
                updated_eur.append(
                    self.unit_costs_eur[index]
                    if index < len(self.unit_costs_eur)
                    else unit_cost
                )
            if remaining <= 1e-12:
                updated.extend(self.lots[index + 1 :])
                updated_eur.extend(self.unit_costs_eur[index + 1 :])
                break
        self.lots = updated
        self.unit_costs_eur = updated_eur
        return gain, max(0.0, gain) * tax_rate


def _is_deposit_timestamp(timestamp: datetime) -> bool:
    return timestamp.weekday() == 6 and timestamp.hour == 0


def _deposit_timestamps(
    previous: datetime | None,
    current: datetime,
) -> list[datetime]:
    if previous is None:
        return [current] if _is_deposit_timestamp(current) else []
    timestamp = previous + timedelta(hours=1)
    deposits: list[datetime] = []
    while timestamp <= current:
        if _is_deposit_timestamp(timestamp):
            deposits.append(timestamp)
        timestamp += timedelta(hours=1)
    return deposits


def _schedule_period_key(schedule: sqlite3.Row, timestamp: datetime) -> str:
    local_timestamp = timestamp.astimezone(ZoneInfo("Europe/Madrid"))
    if schedule["frequency"] == "monthly":
        return local_timestamp.strftime("%Y-%m")
    iso = local_timestamp.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _monthly_schedule_day(year: int, month: int, requested_day: int) -> int:
    """Clamp the configured monthly DCA day to the target month's length."""
    return min(int(requested_day), calendar.monthrange(year, month)[1])


def _schedule_matches(schedule: sqlite3.Row, timestamp: datetime) -> bool:
    local_timestamp = timestamp.astimezone(ZoneInfo("Europe/Madrid"))
    if not schedule["enabled"]:
        return False
    if schedule["frequency"] == "monthly":
        scheduled_day = _monthly_schedule_day(
            local_timestamp.year,
            local_timestamp.month,
            int(schedule["day_of_month"]),
        )
        return local_timestamp.hour == 0 and local_timestamp.day == scheduled_day
    # Preserve the established Sunday 00:00 UTC candle boundary as well as
    # honoring a Madrid-local midnight for configurable weekdays.
    return (
        local_timestamp.hour == 0
        and local_timestamp.weekday() == schedule["day_of_week"]
    ) or (
        schedule["day_of_week"] == 6
        and timestamp.hour == 0
        and timestamp.weekday() == 6
    )


def next_dca_execution(
    schedule: dict[str, object],
    now: datetime | None = None,
) -> datetime | None:
    """Return the next Madrid-midnight execution converted to UTC."""
    if not schedule["enabled"]:
        return None
    madrid = ZoneInfo("Europe/Madrid")
    current_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    current = current_utc.astimezone(madrid)
    candidate_date = current.date()
    candidates: list[datetime] = []
    for _ in range(370):
        candidate = datetime(
            candidate_date.year,
            candidate_date.month,
            candidate_date.day,
            tzinfo=madrid,
        )
        if schedule["frequency"] == "weekly" and candidate_date.weekday() == schedule["day_of_week"]:
            candidate_utc = candidate.astimezone(timezone.utc)
            if candidate_utc > current_utc:
                candidates.append(candidate_utc)
        if schedule["frequency"] == "weekly" and schedule["day_of_week"] == 6 and candidate_date.weekday() == 6:
            sunday_utc = datetime(
                candidate_date.year,
                candidate_date.month,
                candidate_date.day,
                tzinfo=timezone.utc,
            )
            if sunday_utc > current_utc:
                candidates.append(sunday_utc)
        if schedule["frequency"] == "monthly" and candidate_date.day == _monthly_schedule_day(
            candidate_date.year,
            candidate_date.month,
            int(schedule["day_of_month"]),
        ):
            candidate_utc = candidate.astimezone(timezone.utc)
            if candidate_utc > current_utc:
                candidates.append(candidate_utc)
        if candidates:
            return min(candidates)
        candidate_date += timedelta(days=1)
    raise RuntimeError("Unable to calculate the next DCA execution")


def update_live_data(
    price_path: Path = PRICE_PATH,
    dataset_path: Path = DATASET_PATH,
) -> pl.DataFrame:
    """Fetch closed Binance candles, refresh daily sources, and rebuild features."""
    import ccxt

    existing = pl.read_parquet(price_path).sort("timestamp")
    last_timestamp = existing["timestamp"][-1]
    exchange = ccxt.binance({"enableRateLimit": True})
    exchange.load_markets()
    since = int(last_timestamp.timestamp() * 1000) + 3_600_000
    candles = exchange.fetch_ohlcv("BTC/USDT", timeframe="1h", since=since, limit=1000)
    latest_closed = datetime.now(timezone.utc).replace(
        minute=0, second=0, microsecond=0
    ) - timedelta(hours=1)
    if candles:
        additions = pl.DataFrame(
            candles,
            schema=["epoch_ms", "open", "high", "low", "close", "volume"],
            orient="row",
        ).with_columns(
            pl.from_epoch("epoch_ms", time_unit="ms")
            .dt.replace_time_zone("UTC")
            .cast(pl.Datetime("us", "UTC"))
            .alias("timestamp"),
            (
                pl.from_epoch("epoch_ms", time_unit="ms")
                + pl.duration(hours=1)
            )
            .dt.replace_time_zone("UTC")
            .cast(pl.Datetime("us", "UTC"))
            .alias("close_timestamp"),
            pl.lit(None, dtype=pl.Float64).alias("quote_asset_volume"),
            pl.lit(None, dtype=pl.Int64).alias("number_of_trades"),
            pl.lit(None, dtype=pl.Float64).alias("taker_buy_base_volume"),
            pl.lit(None, dtype=pl.Float64).alias("taker_buy_quote_volume"),
            pl.lit("binance").alias("source"),
        ).select(existing.columns)
        additions = additions.filter(
            (pl.col("timestamp") > last_timestamp)
            & (pl.col("timestamp") <= latest_closed)
        )
        existing = (
            pl.concat([existing, additions], how="vertical_relaxed")
            .unique(subset=["timestamp"], keep="last")
            .sort("timestamp")
        )
    existing.write_parquet(price_path, compression="zstd")
    download_onchain(refresh=True)
    download_macro(refresh=True)
    return build_dataset(price_path, output_path=dataset_path)


def _latest_signal(
    dataset_path: Path = DATASET_PATH,
    model_path: Path = MODEL_PATH,
) -> "Signal":
    dataset = pl.read_parquet(dataset_path).sort("timestamp")
    bundle = load_model_bundle(model_path)
    hmm_features = bundle["hmm_feature_names"]
    values = dataset.select(hmm_features).to_numpy().astype(np.float64)
    medians = np.asarray(bundle["hmm_medians"], dtype=np.float64)
    invalid = ~np.isfinite(values)
    values[invalid] = np.take(medians, np.where(invalid)[1])
    probabilities = bundle["hmm"].predict_proba(
        bundle["hmm_scaler"].transform(values)
    )
    dataset = dataset.with_columns(
        [pl.Series(f"hmm_prob_{i}", probabilities[:, i]) for i in range(4)]
    )
    latest = dataset.tail(1)
    feature_names = bundle["feature_names"]
    model_values = latest.select(feature_names).to_numpy().astype(np.float64)
    model_medians = np.asarray(
        [bundle["model_feature_medians"][name] for name in feature_names],
        dtype=np.float64,
    )
    invalid = ~np.isfinite(model_values)
    model_values[invalid] = np.take(model_medians, np.where(invalid)[1])
    return Signal(
        timestamp=latest["timestamp"][0],
        price=float(latest["close"][0]),
        probability=float(bundle["model"].predict_proba(model_values)[0, 1]),
        hmm_regime=int(np.argmax(probabilities[-1])),
        eurusd=float(latest["eurusd"][0])
        if "eurusd" in latest.columns and latest["eurusd"][0] is not None
        else live_eurusdt_rate(),
    )


@dataclass(frozen=True)
class Signal:
    timestamp: datetime
    price: float
    probability: float
    hmm_regime: int
    eurusd: float = 1.0

    @property
    def target_weight(self) -> float:
        return allocation_for_probability(self.probability)


class PaperTrader:
    """SQLite ledger for equal-contribution DCA and cycle-ML profiles."""

    def __init__(
        self,
        database_path: Path = LEDGER_PATH,
        fee_rate: float = FEE_RATE,
        tax_rate: float | None = None,
    ) -> None:
        self.database_path = database_path
        self.fee_rate = fee_rate
        self.tax_rate = (
            float(os.getenv("TAX_RATE", DEFAULT_TAX_RATE))
            if tax_rate is None
            else tax_rate
        )

    def get_dca_schedule(self) -> dict[str, object]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM dca_config WHERE id = 1"
            ).fetchone()
        return {
            "enabled": bool(row["enabled"]),
            "frequency": row["frequency"],
            "day_of_week": row["day_of_week"],
            "day_of_month": row["day_of_month"],
            "amount_eur": float(row["amount_eur"]),
            "last_executed_period_key": row["last_executed_period_key"],
        }

    def update_dca_schedule(self, schedule: dict[str, object]) -> dict[str, object]:
        if schedule["frequency"] not in {"weekly", "monthly"}:
            raise ValueError("frequency must be weekly or monthly")
        if not 0 <= int(schedule["day_of_week"]) <= 6:
            raise ValueError("day_of_week must be between 0 and 6")
        if not 1 <= int(schedule["day_of_month"]) <= 31:
            raise ValueError("day_of_month must be between 1 and 31")
        amount_eur = float(schedule["amount_eur"])
        if not math.isfinite(amount_eur) or amount_eur <= 0:
            raise ValueError("amount_eur must be a finite value greater than zero")
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE dca_config
                SET enabled = ?, frequency = ?, day_of_week = ?,
                    day_of_month = ?, amount_eur = ?
                WHERE id = 1
                """,
                (
                    int(bool(schedule["enabled"])),
                    schedule["frequency"],
                    int(schedule["day_of_week"]),
                    int(schedule["day_of_month"]),
                    amount_eur,
                ),
            )
        return self.get_dca_schedule()

    def dca_schedule_payload(
        self, now: datetime | None = None
    ) -> dict[str, object]:
        """Return persisted schedule values and the next scheduled execution."""
        schedule = self.get_dca_schedule()
        next_execution = next_dca_execution(schedule, now)
        schedule["next_execution_utc"] = (
            next_execution.isoformat().replace("+00:00", "Z")
            if next_execution
            else None
        )
        schedule["next_execution_madrid"] = (
            next_execution.astimezone(ZoneInfo("Europe/Madrid")).isoformat()
            if next_execution
            else None
        )
        return schedule

    def model_bundle(self, model_path: Path = MODEL_PATH) -> dict[str, object]:
        """Return the mtime-aware production bundle used for inference."""
        return load_model_bundle(model_path)

    def model_mtime_ns(self, model_path: Path = MODEL_PATH) -> int | None:
        return model_mtime_ns(model_path)

    def _connect(self) -> sqlite3.Connection:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS portfolio_state (
                profile TEXT PRIMARY KEY,
                btc_balance REAL NOT NULL DEFAULT 0.0,
                usdt_balance REAL NOT NULL DEFAULT 0.0,
                total_contributed_usd REAL NOT NULL DEFAULT 0.0,
                last_evaluation_timestamp TEXT,
                target_weight REAL NOT NULL DEFAULT 0.2,
                usdt_yield_usd REAL NOT NULL DEFAULT 0.0,
                fees_paid_usd REAL NOT NULL DEFAULT 0.0,
                taxes_paid_usd REAL NOT NULL DEFAULT 0.0,
                tax_escrow_eur REAL NOT NULL DEFAULT 0.0,
                ytd_realized_gain_eur REAL NOT NULL DEFAULT 0.0,
                tax_year INTEGER
            );
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                profile TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                side TEXT NOT NULL,
                price REAL NOT NULL,
                btc_quantity REAL NOT NULL,
                usd_notional REAL NOT NULL,
                fee_usd REAL NOT NULL,
                price_eur REAL NOT NULL DEFAULT 0.0,
                notional_eur REAL NOT NULL DEFAULT 0.0,
                fee_eur REAL NOT NULL DEFAULT 0.0,
                tax_usd REAL NOT NULL DEFAULT 0.0,
                realized_gain_eur REAL NOT NULL DEFAULT 0.0,
                tax_eur REAL NOT NULL DEFAULT 0.0,
                target_weight REAL NOT NULL,
                actual_weight REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tax_lots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                profile TEXT NOT NULL,
                acquired_at TEXT NOT NULL,
                quantity REAL NOT NULL,
                unit_cost REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS initial_deposits (
                seed_key TEXT PRIMARY KEY,
                amount_usd REAL NOT NULL,
                timestamp TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS equity_curve (
                profile TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                btc_price REAL NOT NULL,
                prob_up REAL NOT NULL,
                hmm_regime INTEGER NOT NULL,
                target_weight REAL NOT NULL,
                actual_weight REAL NOT NULL,
                btc_balance REAL NOT NULL,
                usdt_balance REAL NOT NULL,
                total_contributed_usd REAL NOT NULL,
                total_equity_usd REAL NOT NULL,
                net_profit_usd REAL NOT NULL,
                roi_pct REAL,
                fees_paid_usd REAL NOT NULL DEFAULT 0.0,
                fifo_taxes_usd REAL NOT NULL DEFAULT 0.0,
                usdt_yield_usd REAL NOT NULL DEFAULT 0.0,
                btc_equivalent REAL NOT NULL DEFAULT 0.0,
                net_btc_equivalent REAL NOT NULL DEFAULT 0.0,
                equity_eur REAL NOT NULL DEFAULT 0.0,
                equity_usd REAL NOT NULL DEFAULT 0.0,
                tax_escrow_eur REAL NOT NULL DEFAULT 0.0,
                PRIMARY KEY (profile, timestamp)
            );
            CREATE TABLE IF NOT EXISTS dca_config (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                enabled INTEGER NOT NULL DEFAULT 1,
                frequency TEXT NOT NULL DEFAULT 'weekly',
                day_of_week INTEGER NOT NULL DEFAULT 6,
                day_of_month INTEGER NOT NULL DEFAULT 1,
                amount_eur REAL NOT NULL DEFAULT 100.0,
                last_executed_period_key TEXT
            );
            CREATE TABLE IF NOT EXISTS fifo_disposals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_id INTEGER NOT NULL,
                profile TEXT NOT NULL,
                acquired_at TEXT NOT NULL,
                disposed_at TEXT NOT NULL,
                btc_quantity REAL NOT NULL,
                acquisition_cost_eur REAL NOT NULL,
                disposal_proceeds_eur REAL NOT NULL,
                fee_eur REAL NOT NULL,
                realized_gain_eur REAL NOT NULL,
                irpf_bracket_rate REAL NOT NULL
            );
            """
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO dca_config
            (id, enabled, frequency, day_of_week, day_of_month, amount_eur)
            VALUES (1, 1, 'weekly', 6, 1, 100.0)
            """
        )
        for table, column, definition in (
            ("portfolio_state", "target_weight", "REAL NOT NULL DEFAULT 0.2"),
            ("portfolio_state", "usdt_yield_usd", "REAL NOT NULL DEFAULT 0.0"),
            ("portfolio_state", "fees_paid_usd", "REAL NOT NULL DEFAULT 0.0"),
            ("portfolio_state", "taxes_paid_usd", "REAL NOT NULL DEFAULT 0.0"),
            ("portfolio_state", "tax_escrow_eur", "REAL NOT NULL DEFAULT 0.0"),
            ("portfolio_state", "ytd_realized_gain_eur", "REAL NOT NULL DEFAULT 0.0"),
            ("portfolio_state", "tax_year", "INTEGER"),
            ("trades", "tax_usd", "REAL NOT NULL DEFAULT 0.0"),
            ("trades", "price_eur", "REAL NOT NULL DEFAULT 0.0"),
            ("trades", "notional_eur", "REAL NOT NULL DEFAULT 0.0"),
            ("trades", "fee_eur", "REAL NOT NULL DEFAULT 0.0"),
            ("trades", "realized_gain_eur", "REAL NOT NULL DEFAULT 0.0"),
            ("trades", "tax_eur", "REAL NOT NULL DEFAULT 0.0"),
            ("tax_lots", "unit_cost_eur", "REAL NOT NULL DEFAULT 0.0"),
            ("equity_curve", "fees_paid_usd", "REAL NOT NULL DEFAULT 0.0"),
            ("equity_curve", "fifo_taxes_usd", "REAL NOT NULL DEFAULT 0.0"),
            ("equity_curve", "usdt_yield_usd", "REAL NOT NULL DEFAULT 0.0"),
            ("equity_curve", "btc_equivalent", "REAL NOT NULL DEFAULT 0.0"),
            ("equity_curve", "net_btc_equivalent", "REAL NOT NULL DEFAULT 0.0"),
            ("equity_curve", "equity_eur", "REAL NOT NULL DEFAULT 0.0"),
            ("equity_curve", "equity_usd", "REAL NOT NULL DEFAULT 0.0"),
            ("equity_curve", "tax_escrow_eur", "REAL NOT NULL DEFAULT 0.0"),
        ):
            columns = {
                row["name"]
                for row in connection.execute(f"PRAGMA table_info({table})")
            }
            if column not in columns:
                connection.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
                )
        connection.execute(
            """
            UPDATE equity_curve
            SET equity_usd = total_equity_usd,
                equity_eur = total_equity_usd,
                btc_equivalent = btc_balance,
                net_btc_equivalent = btc_balance + usdt_balance / btc_price
            WHERE equity_usd = 0.0 AND total_equity_usd != 0.0
            """
        )
        for profile in PROFILES:
            connection.execute(
                """
                INSERT OR IGNORE INTO portfolio_state
                (profile, btc_balance, usdt_balance, total_contributed_usd)
                VALUES (?, 0.0, 0.0, 0.0)
                """,
                (profile,),
            )
        connection.commit()
        return connection

    @staticmethod
    def _fifo_disposal_matches(
        lots: FIFOTaxLotQueue,
        quantity: float,
        price_eur: float,
        fee_eur: float,
        disposed_at: datetime,
        ytd_gain_eur: float,
        eurusd: float,
    ) -> list[dict[str, object]]:
        """Create FIFO lot-level EUR disposal rows without mutating lots."""
        remaining = quantity
        total_sold = quantity
        matches: list[dict[str, object]] = []
        gain_before = ytd_gain_eur
        for index, (acquired_at, lot_quantity, unit_cost_usd) in enumerate(lots.lots):
            if remaining <= 1e-12:
                break
            consumed = min(remaining, lot_quantity)
            unit_cost_eur = (
                lots.unit_costs_eur[index]
                if index < len(lots.unit_costs_eur)
                else unit_cost_usd / eurusd
            )
            acquisition_cost_eur = consumed * unit_cost_eur
            proceeds_eur = consumed * price_eur
            allocated_fee_eur = fee_eur * consumed / total_sold if total_sold else 0.0
            realized_gain_eur = proceeds_eur - allocated_fee_eur - acquisition_cost_eur
            next_rate = spanish_irpf_marginal_rate(max(0.0, gain_before + realized_gain_eur))
            matches.append(
                {
                    "acquired_at": acquired_at,
                    "disposed_at": disposed_at,
                    "btc_quantity": consumed,
                    "unit_cost_eur": unit_cost_eur,
                    "acquisition_cost_eur": acquisition_cost_eur,
                    "disposal_proceeds_eur": proceeds_eur,
                    "fee_eur": allocated_fee_eur,
                    "realized_gain_eur": realized_gain_eur,
                    "irpf_bracket_rate": next_rate,
                }
            )
            gain_before += realized_gain_eur
            remaining -= consumed
        if remaining > 1e-9:
            raise ValueError("FIFO disposal exceeds available tax-lot quantity")
        return matches

    def get_tax_summary(
        self,
        profile: str,
        eurusd: float = 1.0,
        now: datetime | None = None,
    ) -> dict[str, object]:
        """Return profile FIFO inventory, current EUR valuation, and annual IRPF."""
        if profile not in PROFILES:
            raise ValueError("profile must be dca_holder or cycle_ml_trader")
        current = now or datetime.now(timezone.utc)
        current_year = current.year
        with self._connect() as connection:
            lots = connection.execute(
                """SELECT id, acquired_at, quantity, unit_cost, unit_cost_eur
                   FROM tax_lots WHERE profile = ? ORDER BY acquired_at, id""",
                (profile,),
            ).fetchall()
            state = connection.execute(
                "SELECT * FROM portfolio_state WHERE profile = ?", (profile,)
            ).fetchone()
            latest = connection.execute(
                "SELECT btc_price FROM equity_curve WHERE profile = ? ORDER BY timestamp DESC LIMIT 1",
                (profile,),
            ).fetchone()
        btc_price_eur = float(latest["btc_price"] / eurusd) if latest and eurusd else 0.0
        open_lots: list[dict[str, object]] = []
        total_btc = total_cost = 0.0
        madrid = ZoneInfo("Europe/Madrid")
        for row in lots:
            quantity = float(row["quantity"])
            unit_cost_eur = float(
                row["unit_cost_eur"]
                if row["unit_cost_eur"]
                else row["unit_cost"] / eurusd
            )
            cost_basis = quantity * unit_cost_eur
            total_btc += quantity
            total_cost += cost_basis
            acquired = datetime.fromisoformat(row["acquired_at"])
            if acquired.tzinfo is None:
                acquired = acquired.replace(tzinfo=timezone.utc)
            open_lots.append({
                "lot_id": int(row["id"]),
                "acquired_at_utc": acquired.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                "acquired_at_madrid": acquired.astimezone(madrid).isoformat(),
                "btc_remaining": quantity,
                "unit_cost_eur": unit_cost_eur,
                "total_cost_basis_eur": cost_basis,
                "unrealized_pnl_eur": quantity * btc_price_eur - cost_basis,
            })
        ytd_gain = float(state["ytd_realized_gain_eur"] if state else 0.0)
        if state and state["tax_year"] not in (None, current_year):
            ytd_gain = 0.0
        taxable_base = max(0.0, ytd_gain)
        marginal_rate = spanish_irpf_marginal_rate(taxable_base)
        next_rate, headroom = spanish_irpf_next_bracket(ytd_gain)
        return {
            "profile": profile,
            "snapshot_utc": current.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "btc_price_eur": btc_price_eur,
            "open_lots": open_lots,
            "avg_cost_basis_eur": total_cost / total_btc if total_btc else 0.0,
            "ytd_realized_pnl_eur": ytd_gain,
            "irpf_marginal_rate": marginal_rate,
            "ytd_tax_liability_eur": spanish_irpf_tax(ytd_gain),
            "tax_escrow_eur": float(state["tax_escrow_eur"] if state else 0.0),
            "next_bracket_rate": next_rate,
            "eur_to_next_bracket": headroom,
        }

    def simulate_withdrawal(
        self,
        profile: str,
        target_net_eur: float,
        eurusd: float,
        now: datetime | None = None,
    ) -> dict[str, object]:
        """Simulate cash-first FIFO liquidation using a strictly read-only DB."""
        if profile not in PROFILES:
            raise ValueError("profile must be dca_holder or cycle_ml_trader")
        if not math.isfinite(target_net_eur) or target_net_eur <= 0:
            raise ValueError("target_net_eur must be a finite value greater than zero")
        if not math.isfinite(eurusd) or eurusd <= 0:
            raise ValueError("eurusd must be a finite value greater than zero")
        current = now or datetime.now(timezone.utc)
        uri = f"file:{self.database_path.resolve()}?mode=ro"
        with sqlite3.connect(uri, uri=True) as connection:
            connection.row_factory = sqlite3.Row
            state = connection.execute(
                "SELECT * FROM portfolio_state WHERE profile = ?", (profile,)
            ).fetchone()
            latest = connection.execute(
                "SELECT btc_price FROM equity_curve WHERE profile = ? ORDER BY timestamp DESC LIMIT 1",
                (profile,),
            ).fetchone()
            lot_rows = connection.execute(
                "SELECT acquired_at, quantity, unit_cost_eur, unit_cost FROM tax_lots WHERE profile = ? ORDER BY acquired_at, id",
                (profile,),
            ).fetchall()
        if state is None or latest is None:
            raise ValueError("No portfolio valuation is available for this profile")
        btc_price_eur = float(latest["btc_price"]) / eurusd
        escrow = float(state["tax_escrow_eur"])
        cash_before = max(0.0, float(state["usdt_balance"]) / eurusd - escrow)
        btc_before = float(state["btc_balance"])
        ytd_gain = float(state["ytd_realized_gain_eur"] or 0.0)
        if state["tax_year"] not in (None, current.year):
            ytd_gain = 0.0
        lots: list[dict[str, object]] = []
        for row in lot_rows:
            acquired_at = datetime.fromisoformat(row["acquired_at"])
            if acquired_at.tzinfo is None:
                acquired_at = acquired_at.replace(tzinfo=timezone.utc)
            lots.append({
                "acquired_at": acquired_at,
                "quantity": float(row["quantity"]),
                "unit_cost_eur": float(
                    row["unit_cost_eur"]
                    if row["unit_cost_eur"]
                    else row["unit_cost"] / eurusd
                ),
            })
        available_btc = min(btc_before, sum(float(lot["quantity"]) for lot in lots))
        cash_used = min(cash_before, target_net_eur)
        remaining_target = target_net_eur - cash_used
        fee_rate = self.fee_rate
        gains_before_withdrawal = 0.0

        def evaluate(
            quantity: float,
        ) -> tuple[float, float, float, list[dict[str, object]]]:
            gross = quantity * btc_price_eur
            fee = gross * fee_rate
            remaining = quantity
            gain = 0.0
            matches: list[dict[str, object]] = []
            for lot in lots:
                if remaining <= 1e-12:
                    break
                consumed = min(remaining, float(lot["quantity"]))
                proceeds = consumed * btc_price_eur
                allocated_fee = fee * consumed / quantity if quantity else 0.0
                cost = consumed * float(lot["unit_cost_eur"])
                lot_gain = proceeds - allocated_fee - cost
                gain += lot_gain
                gain_before_lot = gain
                marginal_rate = spanish_irpf_marginal_rate(
                    max(0.0, ytd_gain + gain_before_lot)
                )
                matches.append(
                    {
                        "acquired_at_utc": lot["acquired_at"]
                        .astimezone(timezone.utc)
                        .isoformat()
                        .replace("+00:00", "Z"),
                        "btc_sold": consumed,
                        "unit_cost_eur": float(lot["unit_cost_eur"]),
                        "acquisition_cost_eur": cost,
                        "disposal_proceeds_eur": proceeds,
                        "fee_eur": allocated_fee,
                        "realized_gain_eur": lot_gain,
                        "irpf_bracket_rate": marginal_rate,
                    }
                )
                remaining -= consumed
            incremental_tax = max(
                0.0,
                spanish_irpf_tax(ytd_gain + gain) - spanish_irpf_tax(ytd_gain),
            )
            return gross - fee - incremental_tax, gross, fee, matches

        needed_btc = 0.0
        gross = fee = estimated_tax = 0.0
        consumed_lots: list[dict[str, object]] = []
        if remaining_target > 0:
            if available_btc > 0 and evaluate(available_btc)[0] >= remaining_target:
                low, high = 0.0, available_btc
                for _ in range(80):
                    middle = (low + high) / 2.0
                    if evaluate(middle)[0] >= remaining_target:
                        high = middle
                    else:
                        low = middle
                needed_btc = high
            else:
                needed_btc = available_btc
            net_from_sale, gross, fee, consumed_lots = evaluate(needed_btc)
            total_net = cash_used + max(0.0, net_from_sale)
            remaining_after_cash = max(0.0, cash_before - cash_used)
            remaining_btc = max(0.0, btc_before - needed_btc)
            total_gain = sum(float(lot["realized_gain_eur"]) for lot in consumed_lots)
            gains_before_withdrawal = total_gain
            estimated_tax = max(
                0.0,
                spanish_irpf_tax(ytd_gain + total_gain) - spanish_irpf_tax(ytd_gain),
            )
        else:
            total_net = cash_used
            remaining_after_cash = max(0.0, cash_before - cash_used)
            remaining_btc = btc_before
        insufficient = total_net + 1e-8 < target_net_eur
        delivered = total_net if insufficient else target_net_eur
        if not insufficient:
            # Only liquidate the minimum BTC needed to satisfy the requested
            # bank amount. Cash is consumed first; simulated sale proceeds are
            # not left behind as extra cash after the withdrawal.
            remaining_cash_after_sale = max(0.0, total_net - delivered)
            remaining_after_cash = max(0.0, remaining_after_cash + remaining_cash_after_sale)
        return {
            "profile": profile,
            "target_net_eur": target_net_eur,
            "btc_price_eur": btc_price_eur,
            "available_cash_eur": cash_before,
            "cash_used_eur": cash_used,
            "cash_covers_target": remaining_target <= 1e-9,
            "btc_to_sell": needed_btc,
            "gross_sale_eur": gross,
            "fee_eur": fee,
            "estimated_irpf_eur": estimated_tax,
            "effective_tax_rate_pct": (
                estimated_tax / gains_before_withdrawal * 100.0
                if gains_before_withdrawal > 0
                else 0.0
            ),
            "fifo_lots_consumed": consumed_lots,
            "net_cash_out_eur": delivered,
            "remaining_btc": remaining_btc,
            "remaining_cash_eur": remaining_after_cash,
            "insufficient_funds": insufficient,
            "warning": "insufficient_funds" if insufficient else None,
        }

    def process_signal(
        self,
        signal: Signal,
        initial_deposit_eur: float = 0.0,
        seed_key: str | None = None,
        record_deposit_event: bool = False,
    ) -> dict[str, object]:
        """Process one closed bar idempotently for both profiles."""
        if initial_deposit_eur < 0 or (
            initial_deposit_eur and not seed_key
        ):
            raise ValueError("A positive initial deposit requires a seed key")
        timestamp = signal.timestamp.isoformat()
        results: dict[str, object] = {"executed": False, "profiles": {}}
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            schedule = connection.execute(
                "SELECT * FROM dca_config WHERE id = 1"
            ).fetchone()
            scheduled_period_key = (
                _schedule_period_key(schedule, signal.timestamp)
                if _schedule_matches(schedule, signal.timestamp)
                else None
            )
            scheduled_deposit_eur = (
                float(schedule["amount_eur"])
                if scheduled_period_key
                and schedule["last_executed_period_key"] != scheduled_period_key
                else 0.0
            )
            seed_pending = bool(initial_deposit_eur and not connection.execute(
                "SELECT 1 FROM initial_deposits WHERE seed_key = ?",
                (seed_key,),
            ).fetchone())
            if not seed_pending and all(
                connection.execute(
                    "SELECT 1 FROM equity_curve WHERE profile = ? AND timestamp = ?",
                    (profile, timestamp),
                ).fetchone()
                for profile in PROFILES
            ):
                return {"executed": False, "reason": "duplicate_timestamp"}

            for profile in PROFILES:
                state = connection.execute(
                    "SELECT * FROM portfolio_state WHERE profile = ?", (profile,)
                ).fetchone()
                previous = (
                    datetime.fromisoformat(state["last_evaluation_timestamp"])
                    if state["last_evaluation_timestamp"]
                    else None
                )
                btc = float(state["btc_balance"])
                usdt = float(state["usdt_balance"])
                contributed = float(state["total_contributed_usd"])
                target_weight = float(state["target_weight"])
                if profile == "dca_holder":
                    target_weight = 1.0
                yield_total = float(state["usdt_yield_usd"])
                fees_total = float(state["fees_paid_usd"])
                taxes_total = float(state["taxes_paid_usd"])
                tax_escrow_eur = float(state["tax_escrow_eur"])
                tax_year = state["tax_year"] or signal.timestamp.year
                ytd_gain_eur = float(state["ytd_realized_gain_eur"])
                if previous is not None and usdt > 0:
                    hours = max(
                        0.0,
                        (signal.timestamp - previous).total_seconds() / 3600.0,
                    )
                    accrued = usdt * (
                        (1.0 + RISK_FREE_APY) ** (hours / (365.25 * 24.0)) - 1.0
                    )
                    usdt += accrued
                    yield_total += accrued
                lots = self._load_lots(connection, profile, signal.eurusd)
                tax_ledger = SpanishIRPFLedger(
                    {tax_year: ytd_gain_eur}, tax_escrow_eur
                )
                trades: list[dict[str, object]] = []
                if record_deposit_event and initial_deposit_eur:
                    trades.append(
                        {
                            "side": "DEPOSIT",
                            "btc_quantity": 0.0,
                            "usd_notional": initial_deposit_eur * signal.eurusd,
                            "fee_usd": 0.0,
                            "price_eur": signal.price / signal.eurusd,
                            "notional_eur": initial_deposit_eur,
                            "fee_eur": 0.0,
                            "tax_usd": 0.0,
                            "realized_gain_eur": 0.0,
                            "tax_eur": 0.0,
                            "target_weight": target_weight,
                        }
                    )
                deposits = (
                    [(signal.timestamp, initial_deposit_eur)]
                    if seed_pending
                    else []
                )
                if scheduled_deposit_eur:
                    deposits.append((signal.timestamp, scheduled_deposit_eur))
                for deposit_timestamp, deposit_amount_eur in deposits:
                    deposit_amount_usdt = deposit_amount_eur * signal.eurusd
                    contributed += deposit_amount_eur
                    if profile == "dca_holder":
                        btc_quantity, fee = self._buy_with_cash(
                            signal.price, deposit_amount_usdt
                        )
                        btc += btc_quantity
                        lots.buy(
                            deposit_timestamp,
                            btc_quantity,
                            deposit_amount_usdt / btc_quantity,
                            deposit_amount_eur / btc_quantity,
                        )
                        fees_total += fee
                        trades.append(
                            {
                                "side": "DCA_BUY",
                                "btc_quantity": btc_quantity,
                                "usd_notional": deposit_amount_usdt - fee,
                                "fee_usd": fee,
                                "price_eur": signal.price / signal.eurusd,
                                "notional_eur": (
                                    deposit_amount_usdt - fee
                                ) / signal.eurusd,
                                "fee_eur": fee / signal.eurusd,
                                "tax_usd": 0.0,
                                "realized_gain_eur": 0.0,
                                "tax_eur": 0.0,
                                "target_weight": 1.0,
                            }
                        )
                    else:
                        usdt += deposit_amount_usdt
                        if (
                            scheduled_period_key
                            and deposit_amount_eur == scheduled_deposit_eur
                            and signal.probability >= 0.60
                        ):
                            btc_quantity, fee = self._buy_with_cash(
                                signal.price, deposit_amount_usdt
                            )
                            btc += btc_quantity
                            usdt -= deposit_amount_usdt
                            lots.buy(
                                signal.timestamp,
                                btc_quantity,
                                deposit_amount_usdt / btc_quantity,
                                deposit_amount_eur / btc_quantity,
                            )
                            fees_total += fee
                            trades.append(
                                {
                                    "side": "BUY",
                                    "btc_quantity": btc_quantity,
                                    "usd_notional": deposit_amount_usdt - fee,
                                    "fee_usd": fee,
                                    "price_eur": signal.price / signal.eurusd,
                                    "notional_eur": (deposit_amount_usdt - fee) / signal.eurusd,
                                    "fee_eur": fee / signal.eurusd,
                                    "tax_usd": 0.0,
                                    "realized_gain_eur": 0.0,
                                    "tax_eur": 0.0,
                                    "target_weight": max(0.20, target_weight),
                                }
                            )

                equity = btc * signal.price + usdt
                actual_weight = btc * signal.price / equity if equity else 0.0
                if profile == "cycle_ml_trader":
                    spendable_usdt = max(
                        0.0, usdt - tax_escrow_eur * signal.eurusd
                    )
                    allocation_equity = btc * signal.price + spendable_usdt
                    actual_weight = (
                        btc * signal.price / allocation_equity
                        if allocation_equity
                        else 0.0
                    )
                    last_trade = connection.execute(
                        """
                        SELECT timestamp FROM trades
                        WHERE profile = ? ORDER BY timestamp DESC LIMIT 1
                        """,
                        (profile,),
                    ).fetchone()
                    cooldown = (
                        last_trade is not None
                        and signal.timestamp
                        - datetime.fromisoformat(last_trade["timestamp"])
                        < timedelta(hours=COOLDOWN_HOURS)
                    )
                    signal_target = self._tax_aware_target(
                        signal, target_weight, lots, btc, signal.price
                    )
                    deployment_or_distribution = (
                        signal.probability >= 0.60
                        or (
                            signal.probability < 0.30
                            and signal_target < actual_weight
                        )
                    )
                    if (
                        deployment_or_distribution
                        and not cooldown
                        and not (
                            scheduled_period_key
                            and signal.probability < 0.60
                            and signal_target > actual_weight
                        )
                        and abs(signal_target - actual_weight) >= 0.02
                    ):
                        btc, usdt, trade = self._rebalance(
                            btc, usdt, signal, allocation_equity, actual_weight, lots,
                            signal_target, signal.eurusd, ytd_gain_eur, signal.timestamp,
                        )
                        target_weight = signal_target
                        trades.append(trade)
                        fees_total += float(trade["fee_usd"])
                        taxes_total += float(trade["tax_usd"])
                        realized_gain_eur = float(trade["realized_gain_eur"])
                        if realized_gain_eur:
                            if tax_year != signal.timestamp.year:
                                tax_year = signal.timestamp.year
                                tax_ledger.ytd_gains_eur[tax_year] = 0.0
                            tax_eur_delta = tax_ledger.realize(
                                tax_year, realized_gain_eur
                            )
                            tax_escrow_eur = tax_ledger.tax_escrow_eur
                            ytd_gain_eur = tax_ledger.ytd_gains_eur[tax_year]
                            trade["tax_eur"] = max(0.0, tax_eur_delta)
                            tax_usd = max(0.0, tax_eur_delta * signal.eurusd)
                            trade["tax_usd"] = tax_usd
                            usdt -= tax_usd
                            taxes_total += tax_usd
                        equity = btc * signal.price + usdt
                        actual_weight = btc * signal.price / equity if equity else 0.0
                if scheduled_deposit_eur:
                    trades.append(
                        {
                            "side": "SCHEDULED_DCA",
                            "btc_quantity": 0.0,
                            "usd_notional": scheduled_deposit_eur * signal.eurusd,
                            "fee_usd": 0.0,
                            "price_eur": signal.price / signal.eurusd,
                            "notional_eur": scheduled_deposit_eur,
                            "fee_eur": 0.0,
                            "tax_usd": 0.0,
                            "realized_gain_eur": 0.0,
                            "tax_eur": 0.0,
                            "target_weight": target_weight,
                        }
                    )

                for trade in trades:
                    cursor = connection.execute(
                        """
                        INSERT INTO trades
                        (profile, timestamp, side, price, btc_quantity, usd_notional,
                         fee_usd, price_eur, notional_eur, fee_eur, tax_usd,
                         realized_gain_eur, tax_eur,
                         target_weight, actual_weight)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            profile,
                            timestamp,
                            trade["side"],
                            signal.price,
                            trade["btc_quantity"],
                            trade["usd_notional"],
                            trade["fee_usd"],
                            trade.get("price_eur", signal.price / signal.eurusd),
                            trade.get(
                                "notional_eur",
                                trade["usd_notional"] / signal.eurusd,
                            ),
                            trade.get("fee_eur", trade["fee_usd"] / signal.eurusd),
                            trade["tax_usd"],
                            trade["realized_gain_eur"],
                            trade["tax_eur"],
                            trade["target_weight"],
                            actual_weight,
                        ),
                    )
                    for match in trade.get("fifo_matches", []):
                        connection.execute(
                            """INSERT INTO fifo_disposals
                               (trade_id, profile, acquired_at, disposed_at, btc_quantity,
                                acquisition_cost_eur, disposal_proceeds_eur, fee_eur,
                                realized_gain_eur, irpf_bracket_rate)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (
                                cursor.lastrowid,
                                profile,
                                match["acquired_at"].isoformat(),
                                match["disposed_at"].isoformat(),
                                match["btc_quantity"],
                                match["acquisition_cost_eur"],
                                match["disposal_proceeds_eur"],
                                match["fee_eur"],
                                match["realized_gain_eur"],
                                match["irpf_bracket_rate"],
                            ),
                        )
                equity_eur = equity / signal.eurusd
                net_profit = equity_eur - contributed
                roi_pct = net_profit / contributed * 100.0 if contributed else None
                btc_equivalent = btc
                net_btc_value = net_btc_equivalent(
                    btc, usdt, signal.price, tax_escrow_eur, signal.eurusd
                )
                connection.execute(
                    """
                    INSERT OR REPLACE INTO equity_curve
                    (profile, timestamp, btc_price, prob_up, hmm_regime, target_weight,
                     actual_weight, btc_balance, usdt_balance, total_contributed_usd,
                     total_equity_usd, net_profit_usd, roi_pct, fees_paid_usd,
                     fifo_taxes_usd, usdt_yield_usd, btc_equivalent,
                     net_btc_equivalent, equity_eur, equity_usd, tax_escrow_eur)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        profile,
                        timestamp,
                        signal.price,
                        signal.probability,
                        signal.hmm_regime,
                        1.0 if profile == "dca_holder" else target_weight,
                        actual_weight,
                        btc,
                        usdt,
                        contributed,
                        equity,
                        net_profit,
                        roi_pct,
                        fees_total,
                        taxes_total,
                        yield_total,
                        btc_equivalent,
                        net_btc_value,
                        equity_eur,
                        equity,
                        tax_escrow_eur,
                    ),
                )
                self._save_lots(connection, profile, lots, signal.eurusd)
                connection.execute(
                    """
                    UPDATE portfolio_state
                    SET btc_balance = ?, usdt_balance = ?,
                        total_contributed_usd = ?, last_evaluation_timestamp = ?
                        , target_weight = ?, usdt_yield_usd = ?,
                        fees_paid_usd = ?, taxes_paid_usd = ?,
                        tax_escrow_eur = ?, ytd_realized_gain_eur = ?, tax_year = ?
                    WHERE profile = ?
                    """,
                    (
                        btc,
                        usdt,
                        contributed,
                        timestamp,
                        target_weight,
                        yield_total,
                        fees_total,
                        taxes_total,
                        tax_escrow_eur,
                        ytd_gain_eur,
                        tax_year,
                        profile,
                    ),
                )
                results["profiles"][profile] = {
                    "trades": trades,
                    "equity": equity,
                    "equity_eur": equity_eur,
                    "contributed": contributed,
                    "contributed_eur": contributed,
                    "roi_pct": roi_pct,
                    "fees_paid_usd": fees_total,
                    "fees_paid_eur": fees_total / signal.eurusd,
                    "fifo_taxes_usd": taxes_total,
                    "usdt_yield_usd": yield_total,
                    "cash_yield_eur": yield_total / signal.eurusd,
                    "btc_balance": btc,
                    "usdt_balance": usdt,
                    "net_btc_equivalent": net_btc_value,
                    "equity_eur": equity_eur,
                    "equity_usd": equity,
                    "tax_escrow_eur": tax_escrow_eur,
                }
                results["executed"] = results["executed"] or bool(trades)
            if scheduled_period_key:
                connection.execute(
                    """
                    UPDATE dca_config
                    SET last_executed_period_key = ?
                    WHERE id = 1
                    """,
                    (scheduled_period_key,),
                )
            if seed_pending:
                connection.execute(
                    """
                    INSERT INTO initial_deposits (seed_key, amount_usd, timestamp)
                    VALUES (?, ?, ?)
                    """,
                    (seed_key, initial_deposit_eur, timestamp),
                )
        return results

    def deposit_capital(
        self,
        amount_eur: float,
        signal: Signal | None = None,
    ) -> dict[str, object]:
        """Deposit EUR into both profiles at the latest closed candle."""
        if not math.isfinite(amount_eur) or amount_eur <= 0:
            raise ValueError("amount_eur must be a finite value greater than zero")
        current_signal = signal or _latest_signal()
        return self.process_signal(
            current_signal,
            initial_deposit_eur=amount_eur,
            seed_key=f"manual-deposit:{uuid4()}",
            record_deposit_event=True,
        )

    @staticmethod
    def _load_lots(
        connection: sqlite3.Connection,
        profile: str,
        eurusd: float = 1.0,
    ) -> FIFOTaxLotQueue:
        rows = connection.execute(
            """
            SELECT acquired_at, quantity, unit_cost, unit_cost_eur FROM tax_lots
            WHERE profile = ? ORDER BY acquired_at, id
            """,
            (profile,),
        ).fetchall()
        return FIFOTaxLotQueue(
            [
                (datetime.fromisoformat(row["acquired_at"]), row["quantity"], row["unit_cost"])
                for row in rows
            ],
            [
                float(row["unit_cost_eur"] or row["unit_cost"] / eurusd)
                for row in rows
            ],
        )

    @staticmethod
    def _save_lots(
        connection: sqlite3.Connection,
        profile: str,
        lots: FIFOTaxLotQueue,
        eurusd: float = 1.0,
    ) -> None:
        connection.execute("DELETE FROM tax_lots WHERE profile = ?", (profile,))
        connection.executemany(
            """
            INSERT INTO tax_lots
                (profile, acquired_at, quantity, unit_cost, unit_cost_eur)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (
                    profile,
                    acquired_at.isoformat(),
                    quantity,
                    unit_cost,
                    lots.unit_costs_eur[index]
                    if index < len(lots.unit_costs_eur)
                    else unit_cost / eurusd,
                )
                for index, (acquired_at, quantity, unit_cost) in enumerate(lots.lots)
            ],
        )

    @staticmethod
    def _tax_aware_target(
        signal: Signal,
        previous_target: float,
        lots: FIFOTaxLotQueue,
        btc: float,
        price: float,
    ) -> float:
        if signal.probability >= 0.60:
            return 1.0
        if signal.probability < 0.30 and btc > 0:
            cost = sum(quantity * unit_cost for _, quantity, unit_cost in lots.lots)
            weighted_avg_cost = cost / btc if cost > 0 else 0.0
            if weighted_avg_cost > 0 and price >= weighted_avg_cost * 1.15:
                return 0.20
        return previous_target

    def _buy_with_cash(self, price: float, deposit: float) -> tuple[float, float]:
        fee = deposit * self.fee_rate / (1.0 + self.fee_rate)
        return (deposit - fee) / price, fee

    def _rebalance(
        self,
        btc: float,
        usdt: float,
        signal: Signal,
        equity: float,
        actual_weight: float,
        lots: FIFOTaxLotQueue,
        target: float,
        eurusd: float = 1.0,
        ytd_gain_eur: float = 0.0,
        disposed_at: datetime | None = None,
    ) -> tuple[float, float, dict[str, object]]:
        gain = 0.0
        if target > actual_weight:
            notional = (target * equity - btc * signal.price) / (
                1.0 + target * self.fee_rate
            )
            side = "BUY"
            btc_quantity = notional / signal.price
            fee = notional * self.fee_rate
            btc += btc_quantity
            usdt -= notional + fee
            lots.buy(
                signal.timestamp,
                btc_quantity,
                (notional + fee) / btc_quantity,
                (notional + fee) / btc_quantity / eurusd,
            )
            tax = 0.0
        else:
            notional = (btc * signal.price - target * equity) / (
                1.0 - target * self.fee_rate
            )
            side = "SELL"
            btc_quantity = notional / signal.price
            fee = notional * self.fee_rate
            btc -= btc_quantity
            disposal_matches = self._fifo_disposal_matches(
                lots,
                btc_quantity,
                signal.price / eurusd,
                fee / eurusd,
                disposed_at or signal.timestamp,
                ytd_gain_eur,
                eurusd,
            )
            lots.sell(btc_quantity, signal.price, self.tax_rate)
            gain = sum(float(match["realized_gain_eur"]) for match in disposal_matches) * eurusd
            realized_gain_eur = sum(
                float(match["realized_gain_eur"]) for match in disposal_matches
            )
            usdt += notional - fee
            tax = 0.0
        if notional <= 0 or btc < -1e-9 or usdt < -1e-9:
            raise ValueError("Invalid rebalance calculation")
        trade = {
            "side": side,
            "btc_quantity": btc_quantity,
            "usd_notional": notional,
            "fee_usd": fee,
            "price_eur": signal.price / signal.eurusd,
            "notional_eur": notional / signal.eurusd,
            "fee_eur": fee / signal.eurusd,
            "tax_usd": tax,
            "realized_gain_eur": realized_gain_eur if side == "SELL" else 0.0,
            "tax_eur": 0.0,
            "target_weight": target,
        }
        if side == "SELL":
            trade["fifo_matches"] = disposal_matches
        return btc, usdt, trade


def run_once(dry_run: bool = False) -> dict[str, object]:
    update_live_data()
    signal = _latest_signal()
    result = (
        {"executed": False, "reason": "dry_run"}
        if dry_run
        else PaperTrader().process_signal(signal)
    )
    print(f"Signal: p(up 10% / 30d)={signal.probability:.4f}")
    print(f"HMM regime: {signal.hmm_regime}")
    print(f"Bar: {signal.timestamp} | BTC: €{signal.price / signal.eurusd:,.2f}")
    for profile, values in result.get("profiles", {}).items():
        print(
            f"{profile}: equity=€{values['equity_eur']:,.2f}, "
            f"contributed=€{values['contributed_eur']:,.2f}, "
            f"ROI={values['roi_pct'] if values['roi_pct'] is not None else 'n/a'}"
        )
    return result


def seed_initial(amount_eur: float) -> dict[str, object]:
    """Seed both profiles once and evaluate the latest closed candle."""
    if amount_eur <= 0:
        raise ValueError("Initial seed amount must be positive")
    update_live_data()
    signal = _latest_signal()
    result = PaperTrader().process_signal(
        signal,
        initial_deposit_eur=amount_eur,
        seed_key="initial_test_deposit_usd",
    )
    print(f"Initial seed: €{amount_eur:,.2f} per profile")
    print(f"Bar: {signal.timestamp} | BTC: €{signal.price / signal.eurusd:,.2f}")
    for profile, values in result.get("profiles", {}).items():
        print(
            f"{profile}: equity=€{values['equity_eur']:,.2f}, "
            f"BTC={values['btc_balance']:.8f}, "
            f"cash=€{values['usdt_balance'] / signal.eurusd:,.2f}"
        )
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--seed-initial", type=float)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if args.seed_initial is not None:
        seed_initial(args.seed_initial)
    else:
        run_once(dry_run=args.dry_run)
