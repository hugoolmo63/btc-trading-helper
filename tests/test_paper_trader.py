from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import src.api.server as server
from src.execution.paper_trader import (
    FIFOTaxLotQueue,
    PaperTrader,
    RISK_FREE_APY,
    Signal,
    allocation_for_probability,
)


def signal(
    probability: float,
    timestamp: datetime = datetime(2026, 1, 4, tzinfo=timezone.utc),
    price: float = 100.0,
) -> Signal:
    return Signal(timestamp, price, probability, 2)


def test_allocation_thresholds() -> None:
    assert allocation_for_probability(0.65) == 1.0
    assert allocation_for_probability(0.54, 1.0) == 0.5
    assert allocation_for_probability(0.54, 0.5) == 0.5
    assert allocation_for_probability(0.39, 0.5) == 0.2
    assert allocation_for_probability(0.47, 0.2) == 0.2
    assert allocation_for_probability(0.48, 0.2) == 0.5


def test_hysteresis_does_not_oscillate_near_thresholds() -> None:
    weight = 0.5
    for probability in (0.52, 0.55, 0.53, 0.56):
        weight = allocation_for_probability(probability, weight)
    assert weight == 0.5


def test_fifo_lots_tax_only_positive_realized_gain() -> None:
    lots = FIFOTaxLotQueue()
    acquired = datetime(2026, 1, 1, tzinfo=timezone.utc)
    lots.buy(acquired, 1.0, 100.0)
    lots.buy(acquired, 1.0, 200.0)

    gain, tax = lots.sell(1.5, 250.0, tax_rate=0.20)

    assert gain == 175.0
    assert tax == 35.0
    assert len(lots.lots) == 1
    assert lots.lots[0][1] == 0.5
    assert lots.lots[0][2] == 200.0

    loss, loss_tax = lots.sell(0.5, 150.0, tax_rate=0.20)
    assert loss == -25.0
    assert loss_tax == 0.0


def test_cycle_top_distribution_uses_fifteen_percent_profit_guard() -> None:
    lots = FIFOTaxLotQueue()
    acquired = datetime(2026, 1, 1, tzinfo=timezone.utc)
    lots.buy(acquired, 1.0, 100.0)

    assert PaperTrader._tax_aware_target(
        signal(0.20, price=114.99), 0.5, lots, 1.0, 114.99
    ) == 0.5
    assert PaperTrader._tax_aware_target(
        signal(0.20, price=115.0), 0.5, lots, 1.0, 115.0
    ) == 0.2


def test_weekly_deposit_is_equal_and_dca_buys(tmp_path) -> None:
    trader = PaperTrader(tmp_path / "paper.db")
    result = trader.process_signal(signal(0.9))

    assert result["profiles"]["dca_holder"]["contributed"] == 100.0
    assert result["profiles"]["cycle_ml_trader"]["contributed"] == 100.0
    assert result["profiles"]["dca_holder"]["trades"][0]["side"] == "DCA_BUY"
    assert result["profiles"]["cycle_ml_trader"]["trades"]


def test_fee_deduction_and_roi_are_recorded(tmp_path) -> None:
    trader = PaperTrader(tmp_path / "paper.db")
    result = trader.process_signal(signal(0.9))
    dca = result["profiles"]["dca_holder"]

    assert next(trade for trade in dca["trades"] if trade["side"] == "DCA_BUY")["fee_usd"] > 0
    assert dca["equity"] < dca["contributed"]
    assert dca["roi_pct"] < 0


def test_trade_persists_and_returns_eur_execution_values(tmp_path, monkeypatch) -> None:
    database_path = tmp_path / "paper.db"
    trader = PaperTrader(database_path)
    trader.process_signal(signal(0.9))

    with trader._connect() as connection:
        row = connection.execute(
            """
            SELECT price_eur, notional_eur, fee_eur
            FROM trades WHERE profile = 'dca_holder' AND side = 'DCA_BUY'
            """
        ).fetchone()

    assert row["price_eur"] > 0
    assert row["notional_eur"] > 0
    assert row["fee_eur"] > 0

    monkeypatch.setattr(server, "DB_PATH", database_path)
    monkeypatch.setattr(server, "live_eurusdt_rate", lambda: 1.0)
    api_trade = server.ledger_payload()["trades"][0]
    assert api_trade["price_eur"] == pytest.approx(row["price_eur"])
    assert api_trade["notional_eur"] == pytest.approx(row["notional_eur"])
    assert api_trade["fee_eur"] == pytest.approx(row["fee_eur"])

def test_neutral_weekly_deposit_stays_cash_without_tax_event(tmp_path) -> None:
    trader = PaperTrader(tmp_path / "paper.db")
    result = trader.process_signal(signal(0.50))
    cycle = result["profiles"]["cycle_ml_trader"]

    assert [trade["side"] for trade in cycle["trades"]] == ["SCHEDULED_DCA"]
    assert cycle["trades"][0]["fee_eur"] == 0.0
    assert cycle["net_btc_equivalent"] == 1.0
    assert cycle["tax_escrow_eur"] == 0.0


def test_default_risk_free_yield_is_2_5_percent_apy(tmp_path) -> None:
    trader = PaperTrader(tmp_path / "paper.db")
    first_timestamp = datetime(2026, 1, 4, tzinfo=timezone.utc)
    second_timestamp = first_timestamp + timedelta(days=7)
    trader.process_signal(signal(0.50, first_timestamp))
    result = trader.process_signal(signal(0.50, second_timestamp))

    expected_yield = 100.0 * (
        (1.0 + 0.025) ** (7.0 / 365.25) - 1.0
    )
    cycle = result["profiles"]["cycle_ml_trader"]

    assert RISK_FREE_APY == 0.025
    assert cycle["cash_yield_eur"] == pytest.approx(expected_yield)


def test_idempotency_and_profile_balances(tmp_path) -> None:
    trader = PaperTrader(tmp_path / "paper.db")
    first = trader.process_signal(signal(0.9))
    second = trader.process_signal(signal(0.9))

    assert first["executed"] is True
    assert second["reason"] == "duplicate_timestamp"


def test_next_week_adds_equal_contribution(tmp_path) -> None:
    trader = PaperTrader(tmp_path / "paper.db")
    trader.process_signal(signal(0.9))
    result = trader.process_signal(
        signal(0.0, datetime(2026, 1, 11, tzinfo=timezone.utc), 110.0)
    )

    assert result["profiles"]["dca_holder"]["contributed"] == 200.0
    assert result["profiles"]["cycle_ml_trader"]["contributed"] == 200.0


def test_initial_seed_buys_dca_and_is_idempotent(tmp_path) -> None:
    trader = PaperTrader(tmp_path / "paper.db")
    latest = signal(0.1617, datetime(2026, 1, 5, tzinfo=timezone.utc), 100.0)

    first = trader.process_signal(
        latest, initial_deposit_eur=1_000.0, seed_key="initial_test_deposit_usd"
    )
    second = trader.process_signal(
        latest, initial_deposit_eur=1_000.0, seed_key="initial_test_deposit_usd"
    )

    dca = first["profiles"]["dca_holder"]
    cycle = first["profiles"]["cycle_ml_trader"]
    assert dca["contributed"] == 1_000.0
    assert dca["equity"] == pytest.approx(999.000999)
    assert dca["btc_balance"] == pytest.approx(9.99000999)
    assert dca["trades"][0]["side"] == "DCA_BUY"
    assert cycle["contributed"] == 1_000.0
    assert cycle["btc_balance"] == 0.0
    assert cycle["equity"] == pytest.approx(1_000.0)
    assert second["reason"] == "duplicate_timestamp"


def test_weekly_dca_schedule_fires_once_per_iso_week(tmp_path) -> None:
    trader = PaperTrader(tmp_path / "paper.db")
    trader.update_dca_schedule({
        "enabled": True, "frequency": "weekly", "day_of_week": 6,
        "day_of_month": 1, "amount_eur": 125.0,
    })
    first = trader.process_signal(signal(0.50, datetime(2026, 1, 4, tzinfo=timezone.utc)))
    repeated_period = trader.process_signal(
        signal(0.50, datetime(2026, 1, 4, 1, tzinfo=timezone.utc))
    )

    assert first["profiles"]["dca_holder"]["contributed_eur"] == 125.0
    assert first["profiles"]["cycle_ml_trader"]["contributed_eur"] == 125.0
    assert repeated_period["profiles"]["dca_holder"]["contributed_eur"] == 125.0
    with trader._connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM trades WHERE side = 'SCHEDULED_DCA'"
        ).fetchone()[0] == 2
        dca_lot = connection.execute(
            "SELECT quantity, unit_cost_eur FROM tax_lots WHERE profile = 'dca_holder'"
        ).fetchone()
        cycle_state = connection.execute(
            "SELECT usdt_balance, total_contributed_usd FROM portfolio_state WHERE profile = 'cycle_ml_trader'"
        ).fetchone()
        period_key = connection.execute(
            "SELECT last_executed_period_key FROM dca_config WHERE id = 1"
        ).fetchone()[0]
    assert dca_lot[0] > 0
    assert dca_lot[1] == pytest.approx(125.0 / dca_lot[0])
    assert cycle_state[0] >= 125.0
    assert cycle_state[1] == pytest.approx(125.0)
    assert period_key == "2026-W01"


def test_scheduled_deposit_deploys_cycle_profile_on_high_signal(tmp_path) -> None:
    trader = PaperTrader(tmp_path / "paper.db")
    high_signal = signal(0.60, datetime(2026, 1, 4, tzinfo=timezone.utc))
    result = trader.process_signal(high_signal)
    cycle = result["profiles"]["cycle_ml_trader"]

    assert cycle["btc_balance"] > 0
    assert cycle["fees_paid_eur"] > 0
    with trader._connect() as connection:
        lot = connection.execute(
            "SELECT quantity, unit_cost_eur FROM tax_lots WHERE profile = 'cycle_ml_trader'"
        ).fetchone()
    assert lot[0] > 0
    assert lot[1] == pytest.approx(100.0 / lot[0])


def test_scheduled_deposit_retains_cycle_cash_below_deployment_threshold(tmp_path) -> None:
    trader = PaperTrader(tmp_path / "paper.db")
    result = trader.process_signal(
        signal(0.50, datetime(2026, 1, 4, tzinfo=timezone.utc))
    )
    cycle = result["profiles"]["cycle_ml_trader"]

    assert cycle["btc_balance"] == 0.0
    assert cycle["usdt_balance"] >= 100.0
    assert cycle["fees_paid_eur"] == 0.0
    assert [trade["side"] for trade in cycle["trades"]] == ["SCHEDULED_DCA"]


def test_monthly_dca_schedule_uses_madrid_month_and_fires_once(tmp_path) -> None:
    trader = PaperTrader(tmp_path / "paper.db")
    trader.update_dca_schedule({
        "enabled": True, "frequency": "monthly", "day_of_week": 6,
        "day_of_month": 1, "amount_eur": 300.0,
    })
    local_midnight = datetime(2026, 1, 1, tzinfo=ZoneInfo("Europe/Madrid")).astimezone(timezone.utc)
    first = trader.process_signal(signal(0.50, local_midnight))
    repeated_period = trader.process_signal(signal(0.50, local_midnight + timedelta(hours=1)))

    assert first["profiles"]["dca_holder"]["contributed_eur"] == 300.0
    assert repeated_period["profiles"]["cycle_ml_trader"]["contributed_eur"] == 300.0
    with trader._connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM trades WHERE side = 'SCHEDULED_DCA'"
        ).fetchone()[0] == 2
        assert connection.execute(
            "SELECT last_executed_period_key FROM dca_config WHERE id = 1"
        ).fetchone()[0] == "2026-01"


def test_tax_summary_reports_open_lots_ytd_bracket_and_escrow(tmp_path) -> None:
    trader = PaperTrader(tmp_path / "paper.db")
    trader.process_signal(signal(0.90))
    with trader._connect() as connection:
        connection.execute(
            "UPDATE portfolio_state SET ytd_realized_gain_eur = 7000, tax_year = 2026, tax_escrow_eur = 1350 WHERE profile = 'dca_holder'"
        )
    summary = trader.get_tax_summary(
        "dca_holder", eurusd=1.0, now=datetime(2026, 6, 1, tzinfo=timezone.utc)
    )

    assert summary["avg_cost_basis_eur"] > 0
    assert summary["ytd_realized_pnl_eur"] == 7_000
    assert summary["ytd_tax_liability_eur"] == 1_350
    assert summary["irpf_marginal_rate"] == 0.21
    assert summary["tax_escrow_eur"] == 1_350
    assert summary["eur_to_next_bracket"] == 43_000
    assert summary["open_lots"][0]["acquired_at_madrid"].endswith("+01:00")


def test_read_only_withdrawal_simulation_is_cash_first_and_multi_lot_fifo(tmp_path) -> None:
    trader = PaperTrader(tmp_path / "paper.db")
    timestamp = datetime(2026, 1, 4, tzinfo=timezone.utc)
    trader.process_signal(signal(0.50, timestamp, 100.0))
    with trader._connect() as connection:
        connection.execute(
            "UPDATE portfolio_state SET btc_balance = 3, usdt_balance = 400, tax_escrow_eur = 100, ytd_realized_gain_eur = 5000, tax_year = 2026 WHERE profile = 'cycle_ml_trader'"
        )
        connection.execute(
            "INSERT INTO tax_lots(profile, acquired_at, quantity, unit_cost, unit_cost_eur) VALUES ('cycle_ml_trader', ?, 1, 60, 60)",
            (datetime(2025, 1, 1, tzinfo=timezone.utc).isoformat(),),
        )
        connection.execute(
            "INSERT INTO tax_lots(profile, acquired_at, quantity, unit_cost, unit_cost_eur) VALUES ('cycle_ml_trader', ?, 2, 80, 80)",
            (datetime(2025, 2, 1, tzinfo=timezone.utc).isoformat(),),
        )
        connection.execute(
            "UPDATE equity_curve SET btc_price=100, btc_balance=3, usdt_balance=400, total_equity_usd=700 WHERE profile='cycle_ml_trader' AND timestamp=?",
            (timestamp.isoformat(),),
        )
        before_state = tuple(connection.execute(
            "SELECT btc_balance, usdt_balance, tax_escrow_eur FROM portfolio_state WHERE profile='cycle_ml_trader'"
        ).fetchone())
        before_lots = tuple(tuple(row) for row in connection.execute(
            "SELECT acquired_at, quantity, unit_cost_eur FROM tax_lots WHERE profile='cycle_ml_trader' ORDER BY id"
        ).fetchall())
    result = trader.simulate_withdrawal("cycle_ml_trader", 250.0, 1.0)
    assert result["cash_used_eur"] == 250.0
    assert result["btc_to_sell"] == 0.0
    assert result["net_cash_out_eur"] == 250.0
    assert result["remaining_cash_eur"] == 50.0

    multi_lot = trader.simulate_withdrawal("cycle_ml_trader", 450.0, 1.0)
    assert multi_lot["cash_used_eur"] == 300.0
    assert multi_lot["btc_to_sell"] > 1.0
    assert len(multi_lot["fifo_lots_consumed"]) == 2
    assert multi_lot["fifo_lots_consumed"][0]["acquired_at_utc"].startswith("2025-01-01")
    assert multi_lot["gross_sale_eur"] > multi_lot["fee_eur"]
    assert multi_lot["estimated_irpf_eur"] >= 0
    assert multi_lot["net_cash_out_eur"] == pytest.approx(450.0)

    with trader._connect() as connection:
        after_state = tuple(connection.execute(
            "SELECT btc_balance, usdt_balance, tax_escrow_eur FROM portfolio_state WHERE profile='cycle_ml_trader'"
        ).fetchone())
        after_lots = tuple(tuple(row) for row in connection.execute(
            "SELECT acquired_at, quantity, unit_cost_eur FROM tax_lots WHERE profile='cycle_ml_trader' ORDER BY id"
        ).fetchall())
    assert after_state == before_state
    assert after_lots == before_lots


def test_withdrawal_simulation_reports_insufficient_funds(tmp_path) -> None:
    trader = PaperTrader(tmp_path / "paper.db")
    trader.process_signal(signal(0.50))
    result = trader.simulate_withdrawal("dca_holder", 1_000_000.0, 1.0)
    assert result["insufficient_funds"] is True
    assert result["warning"] == "insufficient_funds"


def test_execution_persists_closed_fifo_disposal_matches(tmp_path) -> None:
    trader = PaperTrader(tmp_path / "paper.db")
    first = datetime(2026, 1, 4, tzinfo=timezone.utc)
    trader.process_signal(signal(0.90, first, 100.0))
    trader.process_signal(signal(0.0, first + timedelta(days=7), 130.0))

    with trader._connect() as connection:
        matches = connection.execute(
            "SELECT profile, btc_quantity, acquisition_cost_eur, disposal_proceeds_eur, fee_eur, realized_gain_eur, irpf_bracket_rate FROM fifo_disposals ORDER BY id"
        ).fetchall()
    assert matches
    assert matches[0]["profile"] == "cycle_ml_trader"
    assert matches[0]["btc_quantity"] > 0
    assert matches[0]["disposal_proceeds_eur"] > matches[0]["acquisition_cost_eur"]
    assert matches[0]["fee_eur"] > 0
    assert matches[0]["irpf_bracket_rate"] == 0.19
