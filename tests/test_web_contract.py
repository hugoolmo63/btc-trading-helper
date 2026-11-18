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


def test_signal_pill_restores_interactive_five_driver_xray_without_duplicate_timestamp() -> None:
    html = (ROOT / "web" / "index.html").read_text()
    app = (ROOT / "web" / "app.js").read_text()
    css = (ROOT / "web" / "styles.css").read_text()

    assert 'id="hero-signal" class="signal-pill" type="button"' in html
    assert 'id="signal-xray" class="signal-xray"' in html
    assert 'marker-accumulate"><i></i><b>30%</b>' in html
    assert 'marker-deploy"><i></i><b>60%</b>' in html
    assert "function renderSignalExplanation(explanation, fallbackSignal)" in app
    assert 'query("#hero-signal").addEventListener("click"' in app
    assert 'query("#signal-drivers").innerHTML' in app
    assert ".signal-pill:hover" in css
    assert 'id="hero-btc-updated"' in html
    assert 'formatMadridDateTime(ledgerResult?.latest?.timestamp)' not in app


def test_profile_b_label_and_extreme_weight_btc_subtitle_behavior() -> None:
    html = (ROOT / "web" / "index.html").read_text()
    app = (ROOT / "web" / "app.js").read_text()
    i18n = (ROOT / "web" / "i18n.js").read_text()

    # Profile B button with correct label
    assert 'data-profile="cycle_ml_trader" aria-pressed="false" data-i18n="profile_b">Perfil B: Cycle-ML Trader</button>' in html
    assert 'profile_b: "Perfil B: Cycle-ML Trader"' in i18n
    assert 'profile_b: "Profile B: Cycle-ML Trader"' in i18n

    # New unified hero card: alpha comparison uses cycle_ml_trader vs dca_holder equity
    assert 'const cycleProfile = profiles.cycle_ml_trader || {}' in app
    assert 'const dcaProfile = profiles.dca_holder || {}' in app
    assert 'const alpha = Number(cycleProfile.equity_eur || 0) - Number(dcaProfile.equity_eur || 0)' in app

    # Net BTC equivalent displayed in portfolio hero card
    assert 'query("#portfolio-net-btc")' in app
    assert 'btcEquivalent(activeProfile.net_btc_equivalent)' in app

    # Allocation bar with BTC and cash segments
    assert 'id="allocation-btc-segment"' in html
    assert 'id="allocation-cash-segment"' in html
    assert 'class="allocation-segment allocation-btc"' in html
    assert 'class="allocation-segment allocation-cash"' in html

    # Compact DCA plan with single-row summary
    assert 'class="dca-plan-summary"' in html
    assert 'id="dca-status"' in html
    assert 'id="dca-rule"' in html
    assert 'id="dca-next"' in html
    assert 'id="dca-edit-open"' in html