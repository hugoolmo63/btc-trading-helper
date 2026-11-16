from datetime import datetime, timezone
import sqlite3
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi.testclient import TestClient

import src.api.server as server
import src.execution.paper_trader as paper_trader
from src.execution.paper_trader import PaperTrader
from src.execution.paper_trader import Signal
from src.api.server import _utc_string


def test_api_timestamps_are_serialized_as_utc() -> None:
    value = datetime(2026, 11, 16, 15, tzinfo=timezone.utc)
    assert _utc_string(value) == "2026-11-16T15:00:00Z"


def test_ledger_and_recurring_backtest_use_eur_contract(
    tmp_path, monkeypatch
) -> None:
    database_path = tmp_path / "paper_trades.db"
    trader = PaperTrader(database_path)
    with trader._connect() as connection:
        connection.execute(
            """
            INSERT INTO trades (
                profile, timestamp, side, price, btc_quantity, usd_notional,
                fee_usd, tax_usd, target_weight, actual_weight
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "dca_holder",
                "2026-11-16T18:00:00+00:00",
                "BUY",
                60_000.0,
                0.01665,
                1_000.0,
                1.0,
                0.0,
                1.0,
                1.0,
            ),
        )
    monkeypatch.setattr(server, "DB_PATH", database_path)
    monkeypatch.setattr(server, "live_eurusdt_rate", lambda: 1.0)

    curve = pl.DataFrame(
        {
            "profile": ["dca_holder", "cycle_ml_trader"],
            "timestamp": [
                datetime(2026, 11, 15, tzinfo=timezone.utc),
                datetime(2026, 11, 15, tzinfo=timezone.utc),
            ],
            "equity_eur": [1_000.0, 1_010.0],
        }
    )
    metrics = {
        "dca_holder": {"equity_eur": 1_000.0, "net_btc_equivalent": 0.016},
        "cycle_ml_trader": {
            "equity_eur": 1_010.0,
            "net_btc_equivalent": 0.0161,
            "cash_yield_eur": 1.0,
            "fees_paid_eur": 1.0,
            "spanish_irpf_taxes_eur": 0.0,
        },
    }
    monkeypatch.setattr(
        server,
        "run_walk_forward",
        lambda collect_shap=False: SimpleNamespace(
            oos_predictions=pl.DataFrame()
        ),
    )
    monkeypatch.setattr(
        server,
        "simulate_recurring_profiles",
        lambda predictions: (curve, metrics),
    )

    client = TestClient(server.app)
    ledger_response = client.get("/api/ledger")
    assert ledger_response.status_code == 200
    trade = ledger_response.json()["trades"][0]
    assert trade["price_eur"] > 0
    assert trade["notional_eur"] > 0
    assert trade["fee_eur"] > 0
    assert trade["btc_amount"] > 0

    recurring_response = client.get("/api/backtest/recurring")
    assert recurring_response.status_code == 200
    recurring = recurring_response.json()
    assert recurring["metrics"]["cycle_ml_trader"]["equity_eur"] > 0
    assert recurring["curve"]
    assert recurring["curve"][0]["timestamp"].endswith("Z")


def test_deposit_rejects_non_positive_amounts(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(server, "DB_PATH", tmp_path / "paper_trades.db")
    client = TestClient(server.app)

    assert client.post("/api/deposit", json={"amount_eur": 0}).status_code == 422
    assert client.post("/api/deposit", json={"amount_eur": -10}).status_code == 422
    assert client.post("/api/deposit", json={"amount_eur": "invalid"}).status_code == 422


def test_deposit_applies_profile_rules_and_eur_fifo_lots(
    tmp_path, monkeypatch
) -> None:
    database_path = tmp_path / "paper_trades.db"
    monkeypatch.setattr(server, "DB_PATH", database_path)
    monkeypatch.setattr(server, "live_eurusdt_rate", lambda: 1.0)
    monkeypatch.setattr(
        paper_trader,
        "_latest_signal",
        lambda: Signal(
            datetime(2026, 11, 16, 18, tzinfo=timezone.utc),
            100.0,
            0.1626,
            1,
            1.0,
        ),
    )
    client = TestClient(server.app)

    response = client.post("/api/deposit", json={"amount_eur": 1_000.0})
    assert response.status_code == 200
    payload = response.json()
    assert payload["amount_eur"] == 1_000.0
    assert payload["result"]["profiles"]["cycle_ml_trader"]["trades"] == [
        {
            "side": "DEPOSIT",
            "btc_quantity": 0.0,
            "usd_notional": 1_000.0,
            "fee_usd": 0.0,
            "price_eur": 100.0,
            "notional_eur": 1_000.0,
            "fee_eur": 0.0,
            "tax_usd": 0.0,
            "realized_gain_eur": 0.0,
            "tax_eur": 0.0,
            "target_weight": 0.2,
        }
    ]
    with sqlite3.connect(database_path) as connection:
        dca_lot = connection.execute(
            """
            SELECT quantity, unit_cost_eur FROM tax_lots
            WHERE profile = 'dca_holder'
            """
        ).fetchone()
        cycle_cash = connection.execute(
            """
            SELECT usdt_balance, total_contributed_usd
            FROM portfolio_state WHERE profile = 'cycle_ml_trader'
            """
        ).fetchone()
    assert dca_lot[0] > 0
    assert dca_lot[1] == pytest.approx(1_000.0 / dca_lot[0])
    assert cycle_cash == pytest.approx((1_000.0, 1_000.0))
    dca_trade = next(
        trade for trade in payload["ledger"]["trades"] if trade["side"] == "DCA_BUY"
    )
    assert dca_trade["price_eur"] > 0
    assert dca_trade["notional_eur"] > 0
    assert dca_trade["fee_eur"] > 0

    high_signal_db = tmp_path / "high-signal.db"
    monkeypatch.setattr(server, "DB_PATH", high_signal_db)
    monkeypatch.setattr(
        paper_trader,
        "_latest_signal",
        lambda: Signal(
            datetime(2026, 11, 16, 18, tzinfo=timezone.utc),
            100.0,
            0.60,
            1,
            1.0,
        ),
    )
    high_response = TestClient(server.app).post(
        "/api/deposit", json={"amount_eur": 1_000.0}
    )
    assert high_response.status_code == 200
    with sqlite3.connect(high_signal_db) as connection:
        high_cycle_lot = connection.execute(
            """
            SELECT quantity, unit_cost_eur FROM tax_lots
            WHERE profile = 'cycle_ml_trader'
            """
        ).fetchone()
    assert high_cycle_lot[0] > 0
    assert high_cycle_lot[1] == pytest.approx(1_000.0 / high_cycle_lot[0])


def test_dca_schedule_api_persists_and_returns_next_run(tmp_path, monkeypatch) -> None:
    database_path = tmp_path / "paper_trades.db"
    monkeypatch.setattr(server, "DB_PATH", database_path)
    monkeypatch.setattr(server, "live_eurusdt_rate", lambda: 1.0)
    client = TestClient(server.app)
    payload = {
        "enabled": False,
        "frequency": "monthly",
        "day_of_week": 2,
        "day_of_month": 15,
        "amount_eur": 250.0,
    }

    response = client.post("/api/dca-schedule", json=payload)
    assert response.status_code == 200
    updated = response.json()
    assert updated["enabled"] is False
    assert updated["frequency"] == "monthly"
    assert updated["day_of_month"] == 15
    assert updated["amount_eur"] == 250.0
    assert updated["next_execution_utc"] is None
    assert updated["next_execution_madrid"] is None
    ledger_schedule = client.get("/api/ledger").json()["dca_schedule"]
    assert ledger_schedule["frequency"] == "monthly"
    assert ledger_schedule["amount_eur"] == 250.0
    assert PaperTrader(database_path).get_dca_schedule()["enabled"] is False


@pytest.mark.parametrize(
    "override",
    [
        {"frequency": "daily"},
        {"day_of_week": 7},
        {"day_of_month": 29},
        {"amount_eur": 0},
        {"amount_eur": "Infinity"},
        {"enabled": 1},
    ],
)
def test_dca_schedule_api_rejects_invalid_inputs(tmp_path, monkeypatch, override) -> None:
    monkeypatch.setattr(server, "DB_PATH", tmp_path / "paper_trades.db")
    payload = {
        "enabled": True,
        "frequency": "weekly",
        "day_of_week": 6,
        "day_of_month": 1,
        "amount_eur": 100.0,
    }
    payload.update(override)
    response = TestClient(server.app).post("/api/dca-schedule", json=payload)
    assert response.status_code == 422


def test_ledger_includes_active_dca_schedule_and_next_runs(tmp_path, monkeypatch) -> None:
    database_path = tmp_path / "paper_trades.db"
    monkeypatch.setattr(server, "DB_PATH", database_path)
    monkeypatch.setattr(server, "live_eurusdt_rate", lambda: 1.0)
    schedule = PaperTrader(database_path).get_dca_schedule()
    assert schedule["enabled"] is True
    payload = server.ledger_payload()["dca_schedule"]
    assert payload["frequency"] == "weekly"
    assert payload["day_of_week"] == 6
    assert payload["next_execution_utc"].endswith("Z")
    assert payload["next_execution_madrid"].endswith("+01:00") or payload[
        "next_execution_madrid"
    ].endswith("+02:00")
