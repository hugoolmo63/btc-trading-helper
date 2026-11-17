from src.execution.paper_trader import net_btc_equivalent
from src.execution.tax import (
    SpanishIRPFLedger,
    spanish_irpf_marginal_rate,
    spanish_irpf_next_bracket,
    spanish_irpf_tax,
)


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


def test_progressive_irpf_brackets_and_headroom_boundaries() -> None:
    boundaries = [0.0, 6_000.0, 50_000.0, 200_000.0, 300_000.0, 500_000.0]
    marginal_rates = [0.19, 0.21, 0.23, 0.27, 0.28, 0.30]
    expected_next = [0.21, 0.23, 0.27, 0.28, 0.30, None]
    expected_headroom = [6_000, 44_000, 150_000, 100_000, 200_000, None]
    for base, marginal, next_rate, headroom in zip(
        boundaries, marginal_rates, expected_next, expected_headroom
    ):
        assert spanish_irpf_marginal_rate(base) == marginal
        actual_next, actual_headroom = spanish_irpf_next_bracket(base)
        assert actual_next == next_rate
        assert actual_headroom == headroom

    assert spanish_irpf_tax(6_000) == 1_140
    assert spanish_irpf_tax(50_000) == 10_380
    assert spanish_irpf_tax(200_000) == 44_880
    assert spanish_irpf_tax(300_000) == 71_880
    assert spanish_irpf_tax(500_000) == 127_880
    assert spanish_irpf_tax(510_000) == 130_880


def test_loss_offsets_reduce_ytd_tax_liability_and_headroom() -> None:
    ledger = SpanishIRPFLedger()
    ledger.realize(2026, 7_000)
    ledger.realize(2026, -2_000)
    assert ledger.ytd_gains_eur[2026] == 5_000
    assert spanish_irpf_tax(ledger.ytd_gains_eur[2026]) == 950
    assert spanish_irpf_next_bracket(ledger.ytd_gains_eur[2026]) == (0.21, 1_000)
    assert spanish_irpf_next_bracket(-2_000) == (0.21, 8_000)
