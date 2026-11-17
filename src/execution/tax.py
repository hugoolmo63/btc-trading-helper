"""Spanish progressive savings-base tax accounting."""

from __future__ import annotations

from dataclasses import dataclass, field

IRPF_BRACKETS: tuple[tuple[float, float], ...] = (
    (6_000.0, 0.19),
    (50_000.0, 0.21),
    (200_000.0, 0.23),
    (300_000.0, 0.27),
    (500_000.0, 0.28),
    (float("inf"), 0.30),
)


def spanish_irpf_tax(taxable_gain_eur: float) -> float:
    """Return progressive Spanish IRPF tax for non-negative annual gains."""
    remaining = max(0.0, taxable_gain_eur)
    previous_limit = 0.0
    tax = 0.0
    for limit, rate in IRPF_BRACKETS:
        taxable = min(remaining, limit - previous_limit)
        tax += taxable * rate
        remaining -= taxable
        previous_limit = limit
        if remaining <= 0:
            break
    return tax


def spanish_irpf_marginal_rate(taxable_gain_eur: float) -> float:
    """Return the rate applying to the next positive euro of net gains."""
    taxable_base = max(0.0, taxable_gain_eur)
    for limit, rate in IRPF_BRACKETS:
        if taxable_base < limit:
            return rate
    return IRPF_BRACKETS[-1][1]


def spanish_irpf_next_bracket(taxable_gain_eur: float) -> tuple[float | None, float | None]:
    """Return the next marginal rate and EUR headroom to its boundary."""
    taxable_base = taxable_gain_eur
    for index, (limit, rate) in enumerate(IRPF_BRACKETS):
        if taxable_base < limit:
            if index + 1 >= len(IRPF_BRACKETS):
                return None, None
            return IRPF_BRACKETS[index + 1][1], max(0.0, limit - taxable_base)
    return None, None


@dataclass
class SpanishIRPFLedger:
    """Track annual EUR gains and ring-fenced accrued tax liability."""

    ytd_gains_eur: dict[int, float] = field(default_factory=dict)
    tax_escrow_eur: float = 0.0

    def realize(self, year: int, gain_eur: float) -> float:
        before_gain = self.ytd_gains_eur.get(year, 0.0)
        before_tax = spanish_irpf_tax(before_gain)
        after_gain = before_gain + gain_eur
        self.ytd_gains_eur[year] = after_gain
        after_tax = spanish_irpf_tax(after_gain)
        tax_delta = after_tax - before_tax
        self.tax_escrow_eur = max(0.0, self.tax_escrow_eur + tax_delta)
        return tax_delta
