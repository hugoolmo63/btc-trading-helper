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


def test_dashboard_has_only_one_hero_timestamp_and_no_profile_kpi_subtitles() -> None:
    html = (ROOT / "web" / "index.html").read_text()
    app = (ROOT / "web" / "app.js").read_text()

    assert html.count('id="hero-btc-updated"') == 1
    assert 'class="hero-updated"><span data-i18n="updated"' in html
    assert 'formatMadridDateTime(ledgerResult?.latest?.timestamp)' not in app
    assert 'profileLabel(state.activeProfile === "dca_holder" ? "cycle_ml_trader" : "dca_holder")' not in app
    assert 'profileLabel(state.activeProfile)' not in app
    assert 'Array.from({length: 31}' in app