"""Exclusión cuando lo cobrado en Mongo != MONTO_COBRADO_PARCIAL (idempotencia por suma)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from emv_debt_collection.domain.models import Outcome
from emv_debt_collection.domain.planning import plan_account

from conftest import enabled, make_debt

NO_CAP = Decimal("10000")


@pytest.mark.parametrize(
    ("live", "overcharged", "reason_fragment"),
    [
        (Decimal("200"), False, "Mongo registra menos cobros que la tabla"),
        (Decimal("700"), False, "hay cobros posteriores a la actualización de la tabla"),
        (Decimal("1500"), True, "ATENCIÓN: lo cobrado supera el monto original (posible sobrecobro)"),
    ],
    ids=["mongo-con-menos", "mongo-con-mas", "mongo-sobre-el-original"],
)
def test_mismatch_excludes_debt(live: Decimal, overcharged: bool, reason_fragment: str) -> None:
    # original 1.000 = cobrado 500 (según la tabla) + restante 500
    debt = make_debt(500, collected=500)

    plan = plan_account("acc-test", [debt], enabled(5000), {debt.transaction_id: live}, NO_CAP)

    assert plan.to_charge == []
    [result] = plan.not_charged
    assert result.outcome == Outcome.EXCLUDED
    assert result.collected_seen == live
    assert result.overcharged is overcharged
    assert reason_fragment in result.detail
    assert result.detail.endswith("Revisión manual")


def test_match_is_charged_and_mismatch_does_not_consume_budget() -> None:
    """Una deuda excluida no consume presupuesto: la siguiente se cobra entera."""
    excluded = make_debt(800, collected=200, row_seq=1)
    ok = make_debt(900, collected=100, row_seq=2)
    collected = {excluded.transaction_id: Decimal("0"), ok.transaction_id: Decimal("100")}

    plan = plan_account("acc-test", [excluded, ok], enabled(1000), collected, NO_CAP)

    assert [(c.debt, c.amount) for c in plan.to_charge] == [(ok, Decimal("900"))]
    assert [r.outcome for r in plan.not_charged] == [Outcome.EXCLUDED]


def test_missing_collected_with_budget_is_a_bug() -> None:
    with pytest.raises(ValueError, match="sin cobros consultados"):
        plan_account("acc-test", [make_debt(100)], enabled(5000), None, NO_CAP)
