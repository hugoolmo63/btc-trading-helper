from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_withdrawal_fifo_gain_header_is_localized_separately_from_tax() -> None:
    app = (ROOT / "web" / "app.js").read_text()
    i18n = (ROOT / "web" / "i18n.js").read_text()

    assert '<th>${t("withdrawal_gain")}</th>' in app
    assert 'withdrawal_gain: "Plusvalía (€)"' in i18n
    assert 'withdrawal_gain: "Gain / loss (€)"' in i18n
    assert '[t("withdrawal_tax"), money(result.estimated_irpf_eur)]' in app


def test_withdrawal_quick_chips_follow_the_numeric_input_value() -> None:
    app = (ROOT / "web" / "app.js").read_text()

    assert "function syncWithdrawalChipHighlight()" in app
    assert "value === Number(button.dataset.withdrawalAmount)" in app
    assert 'button.classList.toggle("active", active)' in app
    assert 'query("#withdrawal-target").addEventListener("input", () =>' in app
    assert "syncWithdrawalChipHighlight();" in app