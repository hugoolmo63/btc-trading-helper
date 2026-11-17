const query = (selector) => document.querySelector(selector);
const state = {
  ledger: null,
  cycles: null,
  backtest: null,
  activeProfile: localStorage.getItem("btc_helper_profile") || "cycle_ml_trader",
  theme: localStorage.getItem("btc_trader_theme") || "cyber-emerald",
  taxSummary: null,
  withdrawalTimer: null
};
if (!["dca_holder", "cycle_ml_trader"].includes(state.activeProfile)) {
  state.activeProfile = "cycle_ml_trader";
}
if (state.theme === "bitcoin-sunset" || !["cyber-emerald", "nordic-daylight"].includes(state.theme)) {
  state.theme = "cyber-emerald";
}
document.documentElement.dataset.theme = state.theme;
const money = (value) =>
  `€${Number(value || 0).toLocaleString(undefined, {
    minimumFractionDigits: 2, maximumFractionDigits: 2
  })}`;
const signedMoney = (value) => {
  const amount = Number(value || 0);
  return `${amount >= 0 ? "+" : "-"}${money(Math.abs(amount))}`;
};
const btcHolding = (value) => `${Number(value || 0).toFixed(8)} BTC`;
const btcEquivalent = (value) => `${Number(value || 0).toFixed(6)} BTC`;
const pct = (value) => value == null ? "—" : `${Number(value).toFixed(2)}%`;
const profileLabel = (profile) => t(profile === "dca_holder" ? "profile_a" : "profile_b");
const cashValue = (profile) => Number(profile?.cash_eur ?? profile?.usdt_balance_eur ?? 0);
const liveBtcPrice = (ledger) => {
  const latest = ledger?.latest || {};
  const direct = Number(latest.btc_price_eur ?? ledger?.btc_price_eur);
  if (direct > 0) return direct;
  const dca = ledger?.profiles?.dca_holder || {};
  const fallback = Number(dca.btc_balance) > 0
    ? Number(dca.equity_eur) / Number(dca.btc_balance)
    : 0;
  return fallback > 0 ? fallback : null;
};
const MADRID_TIME_ZONE = "Europe/Madrid";
const madridParts = (value, includeZone = false) => {
  if (!value) return null;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  const locale = getLanguage() === "en" ? "en-GB" : "es-ES";
  const options = {
    timeZone: MADRID_TIME_ZONE,
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hourCycle: "h23"
  };
  if (includeZone) options.timeZoneName = "short";
  return new Intl.DateTimeFormat(locale, options).formatToParts(date);
};
const madridTimezoneAbbreviation = (value = new Date()) => {
  const parts = madridParts(value, true);
  if (!parts) return "CET";
  const zone = parts.find(part => part.type === "timeZoneName")?.value || "";
  return zone.includes("2") || zone === "CEST" ? "CEST" : "CET";
};
const formatMadridDateTime = (value) => {
  const parts = madridParts(value, true);
  if (!parts) return "—";
  const get = (type) => parts.find(part => part.type === type)?.value || "";
  return `${get("year")}-${get("month")}-${get("day")} ${get("hour")}:${get("minute")} ${madridTimezoneAbbreviation(value)}`;
};
const formatDCANextRun = (value) => {
  if (!value) return "—";
  const locale = getLanguage() === "en" ? "en-GB" : "es-ES";
  const parts = new Intl.DateTimeFormat(locale, {
    timeZone: MADRID_TIME_ZONE,
    weekday: "short", day: "numeric", month: "short",
    hour: "2-digit", minute: "2-digit", hourCycle: "h23", timeZoneName: "short"
  }).formatToParts(new Date(value));
  const get = (type) => parts.find(part => part.type === type)?.value || "";
  return `${get("weekday")} ${get("day")} ${get("month")}, ${get("hour")}:${get("minute")} ${get("timeZoneName")}`;
};
const formatMadridPlotlyTimestamp = (value) => {
  const parts = madridParts(value);
  if (!parts) return "";
  const get = (type) => parts.find(part => part.type === type)?.value || "";
  return `${get("year")}-${get("month")}-${get("day")} ${get("hour")}:${get("minute")}`;
};
const madridLocalTimeLabel = () =>
  `${t("local_time")} (${madridTimezoneAbbreviation()})`;
const getPlotlyThemeColors = () => {
  const styles = getComputedStyle(document.documentElement);
  return {
    text: styles.getPropertyValue("--text").trim(),
    panel: styles.getPropertyValue("--panel").trim(),
    line: styles.getPropertyValue("--grid").trim(),
    profileA: styles.getPropertyValue("--accent-a").trim(),
    profileB: styles.getPropertyValue("--accent-b").trim(),
    warm: styles.getPropertyValue("--accent-warm").trim(),
    alert: styles.getPropertyValue("--accent-alert").trim(),
    muted: styles.getPropertyValue("--muted").trim()
  };
};
const getPlotlyThemeLayout = (title, height = 380) => {
  const colors = getPlotlyThemeColors();
  return {
  title: {text: title, font: {size: 14, color: colors.text}},
  paper_bgcolor: colors.panel, plot_bgcolor: colors.panel, font: {color: colors.text},
  height, margin: {l: 58, r: 20, t: 42, b: 42}, hovermode: "x unified",
  xaxis: {gridcolor: colors.line}, yaxis: {gridcolor: colors.line}
  };
};

function showStatus(message, error = false) {
  const node = query("#status");
  node.textContent = message;
  node.className = error ? "status error" : "status";
}

function closeDepositModal() {
  query("#deposit-modal").hidden = true;
  query("#deposit-error").textContent = "";
}

function openDepositModal() {
  query("#deposit-modal").hidden = false;
  query("#deposit-amount").focus();
}

async function submitDeposit(event) {
  event.preventDefault();
  const amount = Number(query("#deposit-amount").value);
  const errorNode = query("#deposit-error");
  const submitButton = query("#deposit-submit");
  if (!Number.isFinite(amount) || amount <= 0) {
    errorNode.textContent = t("deposit_invalid");
    return;
  }
  submitButton.disabled = true;
  submitButton.textContent = t("deposit_loading");
  errorNode.textContent = "";
  try {
    const response = await fetch("/api/deposit", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({amount_eur: amount})
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(payload.detail || t("refresh_error"));
    }
    await load();
    closeDepositModal();
    showStatus(t("deposit_success"));
  } catch (error) {
    errorNode.textContent = error.message;
    showStatus(error.message, true);
  } finally {
    submitButton.disabled = false;
    submitButton.textContent = t("confirm_deposit");
  }
}

function renderKpis(ledger) {
  const profiles = ledger?.profiles || {};
  const activeProfile = profiles[state.activeProfile] || {};
  const otherProfile = profiles[state.activeProfile === "dca_holder" ? "cycle_ml_trader" : "dca_holder"] || {};
  const latest = ledger.latest || {};
  const btcPrice = liveBtcPrice(ledger);
  const signal = latest.prob_up ?? ledger.prob_up;
  const regime = latest.hmm_regime ?? ledger.hmm_regime;
  const activeRow = (ledger?.equity_curve || [])
    .filter(row => row.profile === state.activeProfile)
    .at(-1) || {};
  const btcWeight = activeRow.btc_weight ??
    (state.activeProfile === "cycle_ml_trader" ? latest.actual_weight : null);
  const alpha = Number(activeProfile.equity_eur || 0) - Number(otherProfile.equity_eur || 0);
  const cards = [
    ["net_equity", money(activeProfile.equity_eur), `ROI ${pct(activeProfile.roi_pct)}`],
    ["profile_alpha", signedMoney(alpha), profileLabel(state.activeProfile === "dca_holder" ? "cycle_ml_trader" : "dca_holder")],
    ["net_btc", btcEquivalent(activeProfile.net_btc_equivalent), `${t("btc_balance")}: ${btcHolding(activeProfile.btc_balance)}`],
    ["available_cash", money(cashValue(activeProfile)), profileLabel(state.activeProfile)],
    ["active_btc_weight", pct(btcWeight == null ? null : btcWeight * 100), `${t("tax_escrow_value")}: ${money(activeProfile.tax_escrow_eur)}`]
  ];
  query("#kpis").innerHTML = cards.map(([label, value, delta]) =>
    `<div class="kpi kpi-${label}"><div class="kpi-label">${t(label)}</div><div class="kpi-value">${value}</div><div class="kpi-delta ${label === "profile_alpha" ? (alpha >= 0 ? "semantic-positive" : "semantic-negative") : ""}">${delta}</div></div>`
  ).join("");
  query("#hero-btc-price").textContent = btcPrice ? money(btcPrice) : "—";
  query("#hero-btc-updated").textContent = formatMadridDateTime(latest.timestamp);
  query("#hero-regime").textContent = regime == null ? "—" : t(`regime_${regime}`);
  query("#hero-signal").textContent = signal == null
    ? "—"
    : `${t("ml_signal_compact")}: ${(Number(signal) * 100).toFixed(2)}%`;
  document.querySelectorAll(".profile-button").forEach((button) => {
    const active = button.dataset.profile === state.activeProfile;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });
  const operational = ledger.operational || {};
  query("#operational-status").textContent =
    `${t("ops_status")}: ${t("last_backup")} ${formatMadridDateTime(operational.last_backup_timestamp)} · ` +
    `${t("model_mtime")} ${formatMadridDateTime(operational.model_mtime_utc)}`;
  document.querySelectorAll("[data-i18n='local_time']").forEach((node) => {
    node.textContent = madridLocalTimeLabel();
  });
}

function renderLedger(ledger) {
  const rows = Array.isArray(ledger?.equity_curve) ? ledger.equity_curve : [];
  const dca = rows.filter(row => row.profile === "dca_holder");
  const cycle = rows.filter(row => row.profile === "cycle_ml_trader");
  const colors = getPlotlyThemeColors();
  const activeColor = state.activeProfile === "dca_holder" ? colors.profileA : colors.profileB;
  const inactiveColor = colors.muted;
  const line = (profile, color) => ({
    color,
    width: profile === state.activeProfile ? 3 : 1.2,
    opacity: profile === state.activeProfile ? 1 : .55
  });
  const plot = (id, traces, chartLayout) => {
    if (window.Plotly) {
      return window.Plotly.newPlot(id, traces, chartLayout, {responsive: true});
    }
    const node = document.getElementById(id);
    if (node) node.textContent = t("charts_unavailable");
    return null;
  };
  plot("equity-chart", [
    {x: dca.map(r => formatMadridPlotlyTimestamp(r.timestamp)), y: dca.map(r => r.equity_eur), name: t("dca_holder"), line: line("dca_holder", state.activeProfile === "dca_holder" ? activeColor : inactiveColor)},
    {x: cycle.map(r => formatMadridPlotlyTimestamp(r.timestamp)), y: cycle.map(r => r.equity_eur), name: t("cycle_ml_trader"), line: line("cycle_ml_trader", state.activeProfile === "cycle_ml_trader" ? activeColor : inactiveColor)},
    {x: dca.map(r => formatMadridPlotlyTimestamp(r.timestamp)), y: dca.map(r => r.contributed_eur), name: t("total_contributed"), line: {color: colors.warm, dash: "dash", shape: "hv"}}
  ], getPlotlyThemeLayout(t("equity_curves")));
  plot("net-btc-chart", [
    {x: dca.map(r => formatMadridPlotlyTimestamp(r.timestamp)), y: dca.map(r => r.net_btc_equivalent), name: t("dca_holder"), line: line("dca_holder", state.activeProfile === "dca_holder" ? activeColor : inactiveColor)},
    {x: cycle.map(r => formatMadridPlotlyTimestamp(r.timestamp)), y: cycle.map(r => r.net_btc_equivalent), name: t("cycle_ml_trader"), line: line("cycle_ml_trader", state.activeProfile === "cycle_ml_trader" ? activeColor : inactiveColor)}
  ], {...getPlotlyThemeLayout(t("net_btc_comparison")), yaxis: {title: "BTC"}});
  plot("signal-chart", [
    {x: cycle.map(r => formatMadridPlotlyTimestamp(r.timestamp)), y: cycle.map(r => r.prob_up), name: t("signal_probability"), line: {color: colors.warm}},
    {x: cycle.map(r => formatMadridPlotlyTimestamp(r.timestamp)), y: cycle.map(() => .45), name: t("threshold_50"), line: {color: colors.warm, dash: "dash"}},
    {x: cycle.map(r => formatMadridPlotlyTimestamp(r.timestamp)), y: cycle.map(() => .65), name: t("threshold_100"), line: {color: colors.profileB, dash: "dash"}}
  ], {...getPlotlyThemeLayout(t("signal_chart")), yaxis: {range: [0, 1], gridcolor: colors.line}});
  const trades = (Array.isArray(ledger?.trades) ? ledger.trades : [])
    .filter(trade => trade.profile === state.activeProfile);
  query("#trades").innerHTML = trades.length ? trades.map(trade =>
    `<tr><td>${t(trade.profile)}</td><td>${formatMadridDateTime(trade.timestamp)}</td><td>${t(trade.side) || trade.side}</td><td>${money(trade.price_eur)}</td><td>${money(trade.notional_eur)}</td><td>${money(trade.fee_eur)}</td></tr>`
  ).join("") : `<tr><td colspan="6">${t("no_trades")}</td></tr>`;
}

const WEEKDAY_KEYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"];
const localizedWeekday = (day) => {
  const locale = getLanguage() === "en" ? "en-GB" : "es-ES";
  const monday = new Date(Date.UTC(2024, 0, 1 + Number(day), 12));
  return new Intl.DateTimeFormat(locale, {weekday: "long", timeZone: "UTC"}).format(monday);
};
function populateDCADayOptions(frequency, selectedValue) {
  const select = query("#dca-day");
  const options = frequency === "weekly"
    ? Array.from({length: 7}, (_, index) => `<option value="${index}">${localizedWeekday(index)}</option>`)
    : Array.from({length: 28}, (_, index) => `<option value="${index + 1}">${index + 1}</option>`);
  select.innerHTML = options.join("");
  select.value = String(selectedValue);
  query("#dca-day-label").textContent = t(frequency === "weekly" ? "dca_execution_day" : "dca_day_of_month");
}

function renderDCASchedule(schedule = state.ledger?.dca_schedule) {
  if (!schedule) return;
  const badge = query("#dca-status");
  badge.textContent = t(schedule.enabled ? "dca_active" : "dca_paused");
  badge.classList.toggle("paused", !schedule.enabled);
  const amount = money(schedule.amount_eur);
  const day = schedule.frequency === "weekly"
    ? localizedWeekday(schedule.day_of_week)
    : Number(schedule.day_of_month);
  query("#dca-rule").textContent = `${amount} / ${t(schedule.frequency === "weekly" ? "dca_week_summary" : "dca_month_summary").replace("{day}", day)}`;
  query("#dca-next").textContent = schedule.enabled && schedule.next_execution_madrid
    ? `${t("dca_next")} ${formatDCANextRun(schedule.next_execution_utc)}`
    : t("dca_paused_next");
  query("#dca-enabled").checked = Boolean(schedule.enabled);
  query("#dca-enabled-label").textContent = t(schedule.enabled ? "dca_active" : "dca_paused");
  query("#dca-frequency").value = schedule.frequency;
  populateDCADayOptions(schedule.frequency, schedule.frequency === "weekly" ? schedule.day_of_week : schedule.day_of_month);
  query("#dca-amount").value = Number(schedule.amount_eur).toFixed(2);
}

function openDCAScheduleEditor() {
  query("#dca-error").textContent = "";
  query("#dca-editor").hidden = false;
  query("#dca-edit-open").hidden = true;
  renderDCASchedule(state.ledger?.dca_schedule);
}

function closeDCAScheduleEditor() {
  query("#dca-editor").hidden = true;
  query("#dca-edit-open").hidden = false;
  query("#dca-error").textContent = "";
}

function renderTaxPanel(summary = state.taxSummary) {
  const profile = state.activeProfile;
  query("#tax-export").href = `/api/tax/export.csv?profile=${encodeURIComponent(profile)}`;
  if (!summary && state.ledger?.tax_profiles?.[profile]) {
    summary = state.ledger.tax_profiles[profile];
    state.taxSummary = summary;
  }
  if (!summary || summary.profile !== profile) {
    query("#tax-kpis").innerHTML = `<div class="tax-kpi"><div class="tax-kpi-label">${t("loading")}</div></div>`;
    query("#tax-lots").innerHTML = `<tr><td colspan="4">${t("loading")}</td></tr>`;
    return;
  }
  const cards = [
    [t("tax_avg_cost"), money(summary.avg_cost_basis_eur)],
    [t("tax_ytd_realized"), money(summary.ytd_realized_pnl_eur)],
    [t("tax_current_bracket"), pct(Number(summary.irpf_marginal_rate) * 100)],
    [t("tax_escrow_label"), money(summary.tax_escrow_eur)]
  ];
  query("#tax-kpis").innerHTML = cards.map(([label, value]) =>
    `<div class="tax-kpi"><div class="tax-kpi-label">${label}</div><div class="tax-kpi-value">${value}</div></div>`
  ).join("");
  const headroom = summary.eur_to_next_bracket;
  const nextRate = summary.next_bracket_rate;
  query("#tax-headroom-value").textContent = nextRate == null
    ? t("no_data")
    : `${money(headroom)} · ${t("tax_next_rate")} ${pct(Number(nextRate) * 100)}`;
  const bounds = [6000, 50000, 200000, 300000, 500000];
  const base = Math.max(0, Number(summary.ytd_realized_pnl_eur));
  const bracketFloor = [...bounds].reverse().find(limit => base >= limit) || 0;
  const bracketCeiling = bounds.find(limit => base < limit);
  const progress = bracketCeiling ? ((base - bracketFloor) / (bracketCeiling - bracketFloor)) * 100 : 100;
  query("#tax-progress").style.width = `${Math.max(0, Math.min(100, progress))}%`;
  const lots = Array.isArray(summary.open_lots) ? summary.open_lots : [];
  query("#tax-lots").innerHTML = lots.length ? lots.map(lot =>
    `<tr><td>${formatMadridDateTime(lot.acquired_at_utc)}</td><td>${Number(lot.btc_remaining).toFixed(8)}</td><td>${money(lot.unit_cost_eur)}</td><td>${signedMoney(lot.unrealized_pnl_eur)}</td></tr>`
  ).join("") : `<tr><td colspan="4">${t("tax_no_lots")}</td></tr>`;
}

function renderWithdrawalResult(result) {
  const cards = [
    [t("withdrawal_cash_used"), money(result.cash_used_eur)],
    [t("withdrawal_btc"), `${Number(result.btc_to_sell).toFixed(8)} BTC`],
    [t("withdrawal_gross"), money(result.gross_sale_eur)],
    [t("withdrawal_fee"), money(result.fee_eur)],
    [t("withdrawal_tax"), money(result.estimated_irpf_eur)],
    [t("withdrawal_effective_tax"), pct(result.effective_tax_rate_pct)],
    [t("withdrawal_net"), money(result.net_cash_out_eur)],
    [t("withdrawal_remaining_btc"), `${Number(result.remaining_btc).toFixed(8)} BTC`],
    [t("withdrawal_remaining_cash"), money(result.remaining_cash_eur)]
  ];
  query("#withdrawal-results").innerHTML = cards.map(([label, value], index) =>
    `<div class="withdrawal-result ${index === 6 ? "highlight" : ""}"><span>${label}</span><strong>${value}</strong></div>`
  ).join("");
  query("#withdrawal-results").hidden = false;
  const warning = query("#withdrawal-warning");
  warning.textContent = result.insufficient_funds ? t("withdrawal_insufficient") : "";
  warning.classList.toggle("error", Boolean(result.insufficient_funds));
  const consumed = Array.isArray(result.fifo_lots_consumed) ? result.fifo_lots_consumed : [];
  query("#withdrawal-lots").innerHTML = consumed.length
    ? `<strong>${t("withdrawal_fifo")}</strong><table><thead><tr><th>${t("tax_acquired")}</th><th>BTC</th><th>${t("tax_unit_cost")}</th><th>${t("withdrawal_gain")}</th></tr></thead><tbody>${consumed.map(lot => `<tr><td>${formatMadridDateTime(lot.acquired_at_utc)}</td><td>${Number(lot.btc_sold).toFixed(8)}</td><td>${money(lot.unit_cost_eur)}</td><td>${signedMoney(lot.realized_gain_eur)}</td></tr>`).join("")}</tbody></table>`
    : t("withdrawal_no_sale");
}

function syncWithdrawalChipHighlight() {
  const input = query("#withdrawal-target");
  const value = input.value.trim() === "" ? NaN : Number(input.value);
  document.querySelectorAll("[data-withdrawal-amount]").forEach((button) => {
    const active = Number.isFinite(value) && value === Number(button.dataset.withdrawalAmount);
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });
}

async function requestTaxSummary() {
  const requestedProfile = state.activeProfile;
  try {
    const currentSummary = state.ledger?.tax_profiles?.[requestedProfile];
    if (currentSummary) {
      state.taxSummary = currentSummary;
      renderTaxPanel(currentSummary);
      return;
    }
    const response = await fetch(`/api/tax/summary?profile=${encodeURIComponent(requestedProfile)}`);
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.detail || t("dca_save_error"));
    if (state.activeProfile === requestedProfile) {
      state.taxSummary = payload;
      renderTaxPanel(payload);
    }
  } catch (error) {
    query("#tax-kpis").textContent = error.message;
  }
}

async function simulateWithdrawal() {
  const target = Number(query("#withdrawal-target").value);
  const warning = query("#withdrawal-warning");
  if (!Number.isFinite(target) || target <= 0) {
    warning.textContent = t("deposit_invalid");
    warning.classList.add("error");
    return;
  }
  const requestedProfile = state.activeProfile;
  const button = query("#withdrawal-simulate");
  button.disabled = true;
  button.textContent = t("withdrawal_loading");
  warning.textContent = "";
  try {
    const response = await fetch("/api/tax/simulate-withdrawal", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({profile: requestedProfile, target_net_eur: target})
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.detail || t("withdrawal_error"));
    if (state.activeProfile === requestedProfile) renderWithdrawalResult(payload);
  } catch (error) {
    warning.textContent = error.message;
    warning.classList.add("error");
  } finally {
    button.disabled = false;
    button.textContent = t("withdrawal_simulate");
  }
}

async function saveDCASchedule(event) {
  event.preventDefault();
  const frequency = query("#dca-frequency").value;
  const day = Number(query("#dca-day").value);
  const amount = Number(query("#dca-amount").value);
  const error = query("#dca-error");
  if (!Number.isFinite(amount) || amount <= 0) {
    error.textContent = t("deposit_invalid");
    return;
  }
  const payload = {
    enabled: query("#dca-enabled").checked,
    frequency,
    day_of_week: frequency === "weekly" ? day : Number(state.ledger?.dca_schedule?.day_of_week ?? 6),
    day_of_month: frequency === "monthly" ? day : Number(state.ledger?.dca_schedule?.day_of_month ?? 1),
    amount_eur: amount
  };
  const button = query("#dca-save");
  button.disabled = true;
  error.textContent = "";
  try {
    const response = await fetch("/api/dca-schedule", {
      method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload)
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(result.detail || t("dca_save_error"));
    state.ledger.dca_schedule = result;
    renderDCASchedule(result);
    closeDCAScheduleEditor();
    showStatus(t("dca_save_success"));
  } catch (saveError) {
    error.textContent = saveError.message;
  } finally {
    button.disabled = false;
  }
}

function renderCycles(rows) {
  rows = Array.isArray(rows) ? rows : [];
  const colors = getPlotlyThemeColors();
  const traces = [];
  for (let regime = 0; regime < 4; regime++) {
    const subset = rows.filter(row => row.hmm_regime === regime);
    traces.push({x: subset.map(r => formatMadridPlotlyTimestamp(r.timestamp)), y: subset.map(r => r.btc_price_eur), mode: "markers", marker: {size: 3, color: [colors.profileA, colors.profileB, colors.alert, colors.warm][regime]}, name: t(`regime_${regime}`)});
  }
  if (!window.Plotly) return;
  Plotly.newPlot("cycle-chart", traces, {...getPlotlyThemeLayout(t("btc_by_regime"), 700), yaxis: {type: "log", gridcolor: colors.line}}, {responsive: true});
  Plotly.newPlot("indicators-chart", [
    {x: rows.map(r => formatMadridPlotlyTimestamp(r.timestamp)), y: rows.map(r => r.cycle_cos), name: t("cycle_cos"), line: {color: colors.profileA}},
    {x: rows.map(r => formatMadridPlotlyTimestamp(r.timestamp)), y: rows.map(r => r.dist_200w_sma), name: t("sma_distance"), line: {color: colors.profileB}},
    {x: rows.map(r => formatMadridPlotlyTimestamp(r.timestamp)), y: rows.map(r => r.mvrv_zscore_365d), name: t("mvrv_zscore"), line: {color: colors.warm}}
  ], getPlotlyThemeLayout(t("cycle_diagnostics")), {responsive: true});
}

function renderRecurring(backtest) {
  const rows = Array.isArray(backtest?.curve) ? backtest.curve : [];
  const drawdownNode = query("#drawdown-chart");
  const attributionNode = query("#attribution-chart");
  if (!rows.length) {
    if (drawdownNode) drawdownNode.textContent = t("no_data");
    if (attributionNode) attributionNode.textContent = t("no_data");
    return;
  }
  const drawdown = (profile) => {
    let peak = 0;
    return rows.filter(row => row.profile === profile).map(row => {
      peak = Math.max(peak, Number(row.equity_eur));
      return peak ? Number(row.equity_eur) / peak - 1 : 0;
    });
  };
  const dca = rows.filter(row => row.profile === "dca_holder");
  const cycle = rows.filter(row => row.profile === "cycle_ml_trader");
  if (!window.Plotly) return;
  const colors = getPlotlyThemeColors();
  const recurringLine = (profile, color) => ({
    color: profile === state.activeProfile ? color : "#52617f",
    width: profile === state.activeProfile ? 3 : 1.2,
    opacity: profile === state.activeProfile ? 1 : .55
  });
  try {
    Plotly.newPlot("drawdown-chart", [
      {x: dca.map(r => formatMadridPlotlyTimestamp(r.timestamp)), y: drawdown("dca_holder"), name: t("dca_holder"), line: recurringLine("dca_holder", colors.profileA)},
      {x: cycle.map(r => formatMadridPlotlyTimestamp(r.timestamp)), y: drawdown("cycle_ml_trader"), name: t("cycle_ml_trader"), line: recurringLine("cycle_ml_trader", colors.profileB)}
    ], {...getPlotlyThemeLayout(t("underwater")), yaxis: {tickformat: ".1%", gridcolor: colors.line}}, {responsive: true});
  } catch (error) {
    console.error("Unable to render recurring drawdown chart", error);
    if (drawdownNode) drawdownNode.textContent = t("charts_unavailable");
  }
  const metrics = backtest?.metrics?.cycle_ml_trader || {};
  const metricValue = (key) => Number(metrics[key] || 0);
  try {
    Plotly.newPlot("attribution-chart", [{
      type: "bar",
      x: [t("market_timing"), t("cash_yield"), t("trading_fees"), t("fifo_taxes")],
      y: [
        metricValue("market_timing_alpha_eur"),
        metricValue("cash_yield_eur"),
        -metricValue("fees_paid_eur"),
        -metricValue("spanish_irpf_taxes_eur")
      ],
      marker: {color: [colors.profileA, colors.profileB, colors.alert, colors.warm]}
    }], getPlotlyThemeLayout(t("attribution")), {responsive: true});
  } catch (error) {
    console.error("Unable to render recurring attribution chart", error);
    if (attributionNode) attributionNode.textContent = t("charts_unavailable");
  }
}

function renderAll() {
  translateStatic();
  if (state.ledger) renderKpis(state.ledger), renderLedger(state.ledger);
  if (state.ledger?.dca_schedule) renderDCASchedule(state.ledger.dca_schedule);
  state.taxSummary = state.ledger?.tax_profiles?.[state.activeProfile] || state.taxSummary;
  renderTaxPanel(state.taxSummary);
  if (state.cycles) renderCycles(state.cycles.rows);
  if (state.backtest) renderRecurring(state.backtest);
}

function applyTheme(theme) {
  if (!["cyber-emerald", "nordic-daylight"].includes(theme)) return;
  state.theme = theme;
  localStorage.setItem("btc_trader_theme", theme);
  document.documentElement.dataset.theme = theme;
  const toggle = query("#theme-toggle");
  const lightMode = theme === "nordic-daylight";
  toggle.innerHTML = lightMode
    ? '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M21 12.8A8.5 8.5 0 1 1 11.2 3 6.7 6.7 0 0 0 21 12.8Z"></path></svg>'
    : '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="4"></circle><path d="M12 2v2M12 20v2M4.93 4.93l1.42 1.42M17.65 17.65l1.42 1.42M2 12h2M20 12h2M4.93 19.07l1.42-1.42M17.65 6.35l1.42-1.42"></path></svg>';
  const label = t(lightMode ? "theme_to_dark" : "theme_to_light");
  toggle.setAttribute("aria-label", label);
  toggle.title = label;
  if (state.ledger || state.cycles || state.backtest) renderAll();
}

async function load() {
  const requests = {
    ledger: fetch("/api/ledger").then(response => {
      if (!response.ok) throw new Error(`ledger ${response.status}`);
      return response.json();
    }),
    cycles: fetch("/api/cycles").then(response => {
      if (!response.ok) throw new Error(`cycles ${response.status}`);
      return response.json();
    }),
    backtest: fetch("/api/backtest/recurring").then(response => {
      if (!response.ok) throw new Error(`backtest ${response.status}`);
      return response.json();
    })
  };
  const settled = Object.entries(requests).map(([name, request]) =>
    request.then(value => {
      state[name] = value;
      if (name === "ledger") {
        renderKpis(value);
        renderLedger(value);
        renderDCASchedule(value.dca_schedule);
        state.taxSummary = value.tax_profiles?.[state.activeProfile] || null;
        renderTaxPanel(state.taxSummary);
      }
      if (name === "cycles") renderCycles(value.rows);
      if (name === "backtest") renderRecurring(value);
      return {ok: true, value};
    }).catch(error => {
      console.warn(`Unable to load ${name}`, error);
      if (name === "ledger") {
        state.ledger = {profiles: {}, equity_curve: [], trades: []};
        renderKpis(state.ledger);
        renderLedger(state.ledger);
      }
      if (name === "cycles") renderCycles([]);
      if (name === "backtest") renderRecurring({curve: [], metrics: {}});
      return {ok: false};
    })
  );
  const results = await Promise.allSettled(settled);
  const ledgerResult = results[0]?.value?.value;
  const failures = results.filter(result => result.status === "rejected" || !result.value?.ok).length;
  showStatus(
    failures ? `${t("load_error")}: ${t("partial_load")}` :
      `${t("updated")} ${formatMadridDateTime(ledgerResult?.latest?.timestamp)}.`,
    failures > 0
  );
}

document.querySelectorAll(".profile-button").forEach((button) => {
  button.addEventListener("click", () => {
    state.activeProfile = button.dataset.profile;
    localStorage.setItem("btc_helper_profile", state.activeProfile);
    if (state.ledger) {
      renderKpis(state.ledger);
      renderLedger(state.ledger);
    }
    state.taxSummary = state.ledger?.tax_profiles?.[state.activeProfile] || null;
    renderTaxPanel(state.taxSummary);
    requestTaxSummary();
    if (state.backtest) renderRecurring(state.backtest);
  });
});
query("#theme-toggle").addEventListener("click", () => {
  applyTheme(state.theme === "cyber-emerald" ? "nordic-daylight" : "cyber-emerald");
});
query("#deposit-open").addEventListener("click", openDepositModal);
query("#deposit-cancel").addEventListener("click", closeDepositModal);
query("#deposit-cancel-x").addEventListener("click", closeDepositModal);
query("#deposit-form").addEventListener("submit", submitDeposit);
query("#dca-edit-open").addEventListener("click", openDCAScheduleEditor);
query("#dca-cancel").addEventListener("click", closeDCAScheduleEditor);
query("#dca-editor").addEventListener("submit", saveDCASchedule);
query("#dca-frequency").addEventListener("change", () => {
  const frequency = query("#dca-frequency").value;
  const selected = frequency === "weekly"
    ? Number(state.ledger?.dca_schedule?.day_of_week ?? 6)
    : Number(state.ledger?.dca_schedule?.day_of_month ?? 1);
  populateDCADayOptions(frequency, selected);
});
query("#dca-enabled").addEventListener("change", () => {
  query("#dca-enabled-label").textContent = t(query("#dca-enabled").checked ? "dca_active" : "dca_paused");
});
document.querySelectorAll("[data-dca-amount]").forEach((button) => {
  button.addEventListener("click", () => { query("#dca-amount").value = button.dataset.dcaAmount; });
});
query("#withdrawal-simulate").addEventListener("click", simulateWithdrawal);
query("#withdrawal-target").addEventListener("input", () => {
  syncWithdrawalChipHighlight();
  clearTimeout(state.withdrawalTimer);
  state.withdrawalTimer = setTimeout(simulateWithdrawal, 450);
});
document.querySelectorAll("[data-withdrawal-amount]").forEach((button) => {
  button.addEventListener("click", () => {
    query("#withdrawal-target").value = button.dataset.withdrawalAmount;
    syncWithdrawalChipHighlight();
    simulateWithdrawal();
  });
});
syncWithdrawalChipHighlight();
document.querySelectorAll("[data-deposit-amount]").forEach((button) => {
  button.addEventListener("click", () => {
    query("#deposit-amount").value = button.dataset.depositAmount;
  });
});
document.querySelectorAll("[data-lang]").forEach((button) => {
  button.addEventListener("click", () => setLanguage(button.dataset.lang));
});
window.addEventListener("languagechange", renderAll);
query("#refresh").addEventListener("click", async () => {
  const button = query("#refresh");
  button.disabled = true; showStatus(t("refresh_loading"));
  try {
    const response = await fetch("/api/refresh?dry_run=false", {method: "POST"});
    if (!response.ok) throw new Error(t("refresh_error"));
    await load();
  } catch (error) {
    showStatus(error.message, true);
  } finally { button.disabled = false; }
});
applyTheme(state.theme);
translateStatic();
load();
