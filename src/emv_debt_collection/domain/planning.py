"""Planificación: núcleo funcional del proceso. Todas las funciones son PURAS (sin Mongo, sin API, sin Spark)."""

from __future__ import annotations

from decimal import Decimal

from emv_debt_collection.domain.errors import RunLimitExceeded
from emv_debt_collection.domain.models import (
    AccountPlan,
    AccountSnapshot,
    Debt,
    DebtResult,
    Outcome,
    PlannedCharge,
)
from emv_debt_collection.shared import clp


def debt_priority(debt: Debt) -> tuple[object, ...]:
    """Orden de cobro dentro de una cuenta: más antigua primero; si empata la fecha, mayor monto primero;
    y el TXID como último desempate para que el orden sea siempre el mismo entre ejecuciones."""
    return (debt.settlement_date, -debt.amount, debt.transaction_id)


def charge_floor(min_charge: Decimal) -> Decimal:
    """Monto mínimo de un cobro: el configurado, y nunca menos de $1."""
    return max(min_charge, Decimal("1"))


def account_budget(snapshot: AccountSnapshot, max_per_account: Decimal) -> Decimal:
    """Lo máximo que se le puede cobrar hoy a la cuenta: el saldo en vivo, limitado por el tope por cuenta."""
    return max(min(snapshot.balance, max_per_account), Decimal("0"))


def plan_account(
    account_id: str,
    debts: list[Debt],
    snapshot: AccountSnapshot | None,
    collected: dict[str, Decimal] | None,
    max_per_account: Decimal,
    min_charge: Decimal = Decimal("0"),
) -> AccountPlan:
    """FUNCIÓN PURA: recibe datos y devuelve una decisión. No lee Mongo ni llama a la API.

    `collected` = {txid: suma de cobros en Mongo} de esta cuenta. Puede ser None SOLO si la cuenta no tiene
    presupuesto (no se consultó transactions porque no se le va a cobrar nada).

    Regla de asignación (cobro parcial): se recorren las deudas en orden de prioridad y a cada una se le cobra
    min(lo que falta, presupuesto restante). La última deuda alcanzada puede quedar cobrada en parte.
    Ej.: saldo $80, deudas a1=$100 y a2=$50 -> se cobran $80 a a1. Mañana, con saldo $100: $20 a a1 y $50 a a2.

    Validación de consistencia (idempotencia): lo cobrado en Mongo para la deuda debe ser IGUAL a
    MONTO_COBRADO_PARCIAL. Si difiere, la tabla y Mongo no cuentan la misma historia (cobros posteriores a la
    actualización de la tabla, formato de reason no reconocido...) y la deuda se excluye: nunca se cobra de más."""

    plan = AccountPlan(account_id, snapshot)
    ordered = sorted(debts, key=debt_priority)

    if snapshot is None:
        plan.not_charged = [
            DebtResult(d, Outcome.EXCLUDED, "La cuenta no existe en Mongo: revisión manual") for d in ordered
        ]
        return plan
    if not snapshot.is_chargeable:
        plan.not_charged = [
            DebtResult(d, Outcome.EXCLUDED, f"Cuenta {snapshot.status}: revisión manual", balance_seen=snapshot.balance)
            for d in ordered
        ]
        return plan

    floor = charge_floor(min_charge)
    budget = account_budget(snapshot, max_per_account)
    if budget < floor:
        plan.not_charged = [
            DebtResult(
                d,
                Outcome.NOT_ENOUGH_FUNDS,
                f"Saldo {clp(snapshot.balance)} insuficiente",
                balance_seen=snapshot.balance,
            )
            for d in ordered
        ]
        return plan
    if collected is None:
        raise ValueError(f"Cuenta {account_id} con presupuesto pero sin cobros consultados (bug)")

    for debt in ordered:
        live = collected.get(debt.transaction_id, Decimal("0"))
        if live != debt.collected_amount:
            over = live > debt.original_amount
            reason = (
                "ATENCIÓN: lo cobrado supera el monto original (posible sobrecobro)"
                if over
                else "hay cobros posteriores a la actualización de la tabla"
                if live > debt.collected_amount
                else "Mongo registra menos cobros que la tabla"
            )
            plan.not_charged.append(
                DebtResult(
                    debt,
                    Outcome.EXCLUDED,
                    f"Cobrado en Mongo {clp(live)} != MONTO_COBRADO_PARCIAL {clp(debt.collected_amount)}: {reason}. "
                    "Revisión manual",
                    balance_seen=snapshot.balance,
                    collected_seen=live,
                    overcharged=over,
                )
            )
            continue

        charge = min(debt.amount, budget)
        if charge < floor:
            plan.not_charged.append(
                DebtResult(
                    debt,
                    Outcome.NOT_ENOUGH_FUNDS,
                    f"Presupuesto restante {clp(budget)} (saldo {clp(snapshot.balance)}, tope {clp(max_per_account)})"
                    + (f"; cobro posible {clp(charge)} menor al mínimo {clp(floor)}" if charge > 0 else ""),
                    balance_seen=snapshot.balance,
                    collected_seen=live,
                )
            )
            continue
        plan.to_charge.append(PlannedCharge(debt, charge))
        budget -= charge
    return plan


def check_run_limits(plans: list[AccountPlan], max_charge_per_run: Decimal) -> Decimal:
    """Circuit breaker: si el total planificado supera el tope por ejecución, no se cobra NADA.
    Protege del día en que la tabla venga mal. Lo cobrado nunca puede superar lo planificado."""
    total = sum((p.planned_amount for p in plans), Decimal("0"))
    if total > max_charge_per_run:
        raise RunLimitExceeded(
            f"El total a cobrar {clp(total)} supera el tope por ejecución {clp(max_charge_per_run)}. No se cobró nada."
        )
    return total
