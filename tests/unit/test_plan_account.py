"""plan_account: asignación con cobro parcial, orden de prioridad, tope por cuenta y cobro mínimo."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from emv_debt_collection.domain.models import AccountSnapshot, Outcome
from emv_debt_collection.domain.planning import plan_account

from conftest import enabled, make_debt

NO_CAP = Decimal("10000")


def test_partial_allocation_in_priority_order() -> None:
    """Saldo 2.500 con deudas 1.000, 1.000, 2.800 y 400 -> 1.000 + 1.000 + 500 (parcial); la de 400 sin saldo."""
    d1 = make_debt(1000, settlement_date=date(2026, 8, 1))
    d2 = make_debt(1000, settlement_date=date(2026, 8, 2))
    d3 = make_debt(2800, settlement_date=date(2026, 8, 3))
    d4 = make_debt(400, settlement_date=date(2026, 8, 4))

    plan = plan_account("acc-test", [d4, d3, d2, d1], enabled(2500), {}, NO_CAP)  # desordenadas a propósito

    assert [(c.debt, c.amount) for c in plan.to_charge] == [
        (d1, Decimal("1000")),
        (d2, Decimal("1000")),
        (d3, Decimal("500")),
    ]
    assert [c.is_partial for c in plan.to_charge] == [False, False, True]
    assert plan.planned_amount == Decimal("2500")
    assert len(plan.not_charged) == 1
    assert plan.not_charged[0].debt == d4
    assert plan.not_charged[0].outcome == Outcome.NOT_ENOUGH_FUNDS


def test_tie_breaks_by_larger_amount_then_txid() -> None:
    same_day = date(2026, 8, 1)
    small = make_debt(300, transaction_id="TXTEST-A", settlement_date=same_day)
    big = make_debt(900, transaction_id="TXTEST-Z", settlement_date=same_day)
    twin_b = make_debt(500, transaction_id="TXTEST-B", settlement_date=same_day)
    twin_a = make_debt(500, transaction_id="TXTEST-A2", settlement_date=same_day)

    plan = plan_account("acc-test", [small, twin_b, big, twin_a], enabled(5000), {}, NO_CAP)

    assert [c.debt for c in plan.to_charge] == [big, twin_a, twin_b, small]


def test_cap_per_account_limits_budget() -> None:
    """Con saldo de sobra, el tope por cuenta manda: 1.500 = 1.000 + 500 parcial."""
    d1 = make_debt(1000, settlement_date=date(2026, 8, 1))
    d2 = make_debt(1000, settlement_date=date(2026, 8, 2))

    plan = plan_account("acc-test", [d1, d2], enabled(50_000), {}, max_per_account=Decimal("1500"))

    assert [c.amount for c in plan.to_charge] == [Decimal("1000"), Decimal("500")]
    assert plan.planned_amount == Decimal("1500")


def test_min_charge_skips_charges_below_floor() -> None:
    """Con mínimo 600, el remanente de 500 no alcanza para la 2.800 ni para la de 400."""
    d1 = make_debt(2000, settlement_date=date(2026, 8, 1))
    d2 = make_debt(2800, settlement_date=date(2026, 8, 2))
    d3 = make_debt(400, settlement_date=date(2026, 8, 3))

    plan = plan_account("acc-test", [d1, d2, d3], enabled(2500), {}, NO_CAP, min_charge=Decimal("600"))

    assert [(c.debt, c.amount) for c in plan.to_charge] == [(d1, Decimal("2000"))]
    assert {r.debt.transaction_id: r.outcome for r in plan.not_charged} == {
        d2.transaction_id: Outcome.NOT_ENOUGH_FUNDS,
        d3.transaction_id: Outcome.NOT_ENOUGH_FUNDS,
    }
    assert all("menor al mínimo $600" in r.detail for r in plan.not_charged)


def test_zero_min_charge_still_requires_one_peso() -> None:
    """min_charge_amount=0 significa "sin mínimo", pero nunca se cobra menos de $1."""
    debt = make_debt(1000)

    plan = plan_account("acc-test", [debt], enabled(0), None, NO_CAP, min_charge=Decimal("0"))

    assert plan.to_charge == []
    assert plan.not_charged[0].outcome == Outcome.NOT_ENOUGH_FUNDS
    assert plan.not_charged[0].detail == "Saldo $0 insuficiente"


def test_disabled_account_is_excluded() -> None:
    debt = make_debt(1000)

    plan = plan_account("acc-test", [debt], AccountSnapshot("acc-test", Decimal("9000"), "DISABLED"), None, NO_CAP)

    assert plan.to_charge == []
    assert plan.not_charged[0].outcome == Outcome.EXCLUDED
