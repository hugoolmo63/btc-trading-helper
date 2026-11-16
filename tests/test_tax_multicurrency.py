from src.execution.paper_trader import net_btc_equivalent
from src.execution.tax import SpanishIRPFLedger, spanish_irpf_tax


def test_spanish_irpf_crosses_six_thousand_euro_bracket() -> None:
    assert spanish_irpf_tax(6_000.0) == 1_140.0
    assert spanish_irpf_tax(7_000.0) == 1_350.0

    ledger = SpanishIRPFLedger()
    assert ledger.realize(2026, 6_000.0) == 1_140.0
    assert ledger.realize(2026, 1_000.0) == 210.0
    assert ledger.tax_escrow_eur == 1_350.0


def test_tax_escrow_is_not_reinvestable_in_net_btc_equivalent() -> None:
    assert net_btc_equivalent(0.5, 1_000.0, 100.0, 0.0, 1.0) == 10.5
    assert net_btc_equivalent(0.5, 1_000.0, 100.0, 400.0, 1.0) == 6.5


def test_profile_b_cash_and_profile_a_btc_can_have_equal_net_btc() -> None:
    assert net_btc_equivalent(1.0, 0.0, 100.0) == 1.0
    assert net_btc_equivalent(0.5, 50.0, 100.0) == 1.0
