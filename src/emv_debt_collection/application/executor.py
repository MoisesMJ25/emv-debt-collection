"""Ejecución del plan: Saga por cuenta con compensación garantizada (re-bloqueo en `finally`)."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

from emv_debt_collection.application.retry import REBLOCK_POLICY, VERIFY_POLICY, call_with_retry, wait_until
from emv_debt_collection.config import AUDIT_REASONS
from emv_debt_collection.domain.errors import AmbiguousError
from emv_debt_collection.domain.models import AccountPlan, DebtResult, Outcome, PlannedCharge
from emv_debt_collection.domain.planning import charge_floor
from emv_debt_collection.ports import AccountRepository, BackofficeGateway
from emv_debt_collection.shared import clp, describe

log = logging.getLogger(__name__)


def charge_debt(
    order: PlannedCharge, api: BackofficeGateway, repo: AccountRepository, min_charge: Decimal
) -> DebtResult:
    """Cobra UNA deuda (toda o una parte). Antes de debitar vuelve a mirar Mongo: el plan es una foto."""
    debt, account_id = order.debt, order.debt.account_id
    floor = charge_floor(min_charge)
    try:
        # 1. Idempotencia por SUMA: lo cobrado en vivo debe seguir siendo lo que vio el plan.
        before, _ = call_with_retry(lambda: repo.collected_for_debt(account_id, debt.transaction_id))
        if before >= debt.original_amount:
            return DebtResult(
                debt,
                Outcome.ALREADY_CHARGED,
                f"La deuda ya está saldada en Mongo (cobrado {clp(before)} de {clp(debt.original_amount)})",
                collected_seen=before,
                planned_amount=order.amount,
                overcharged=before > debt.original_amount,
            )
        if before != debt.collected_amount:
            return DebtResult(
                debt,
                Outcome.EXCLUDED,
                f"Lo cobrado cambió durante el proceso ({clp(debt.collected_amount)} -> {clp(before)}): "
                "revisión manual",
                collected_seen=before,
                planned_amount=order.amount,
            )

        # 2. Saldo en vivo justo antes del débito (pudo cambiar desde el plan o por el débito anterior).
        snapshot, _ = call_with_retry(lambda: repo.get_account(account_id))
        if snapshot is None or not snapshot.is_chargeable:
            status = snapshot.status if snapshot else "inexistente"
            return DebtResult(
                debt,
                Outcome.EXCLUDED,
                f"La cuenta pasó a estado {status} durante el proceso",
                collected_seen=before,
                planned_amount=order.amount,
            )
        # Nunca más de lo planificado (protege el tope por ejecución) ni más que el saldo actual.
        amount = min(order.amount, snapshot.balance)
        if amount < floor:
            return DebtResult(
                debt,
                Outcome.NOT_ENOUGH_FUNDS,
                f"Saldo actual {clp(snapshot.balance)} insuficiente para el cobro",
                balance_seen=snapshot.balance,
                collected_seen=before,
                planned_amount=order.amount,
            )
    except Exception as exc:
        return DebtResult(
            debt,
            Outcome.FAILED,
            f"No se pudo verificar la cuenta antes del cobro: {describe(exc)}",
            planned_amount=order.amount,
        )

    outcome = Outcome.CHARGED if amount == debt.amount else Outcome.PARTIALLY_CHARGED
    applied = (
        "Cobro aplicado: deuda saldada"
        if outcome == Outcome.CHARGED
        else f"Cobro parcial aplicado: quedan {clp(debt.amount - amount)}"
    )
    balance_seen, collected_seen, planned_amount = snapshot.balance, before, order.amount

    # 3. Débito. Solo se reintenta si la conexión nunca se estableció (TransientError del adaptador).
    try:
        _, attempts = call_with_retry(lambda: api.withdraw(account_id, amount, debt.reason))
        return DebtResult(
            debt,
            outcome,
            applied,
            charged_amount=amount,
            attempts=attempts,
            balance_seen=balance_seen,
            collected_seen=collected_seen,
            planned_amount=planned_amount,
        )
    except AmbiguousError as exc:
        # El servidor pudo aplicar el retiro sin respondernos. Como el reason ya no es único (cobros parciales),
        # se concilia por SUMA: si lo cobrado aumentó exactamente en `amount`, el retiro se aplicó.
        expected = before + amount

        def applied_now() -> bool:
            try:
                return repo.collected_for_debt(account_id, debt.transaction_id) == expected
            except Exception:
                return False

        if wait_until(applied_now, VERIFY_POLICY):
            return DebtResult(
                debt,
                outcome,
                f"{applied} (confirmado por conciliación: {describe(exc)})",
                charged_amount=amount,
                attempts=1,
                balance_seen=balance_seen,
                collected_seen=collected_seen,
                planned_amount=planned_amount,
            )
        return DebtResult(
            debt,
            Outcome.FAILED,
            f"Respuesta ambigua y lo cobrado no aumentó en {clp(amount)}: {describe(exc)}. "
            "Si el cobro aparece después, el pipeline lo sumará a MONTO_COBRADO_PARCIAL",
            attempts=1,
            uncertain=True,
            balance_seen=balance_seen,
            collected_seen=collected_seen,
            planned_amount=planned_amount,
        )
    except Exception as exc:
        return DebtResult(
            debt,
            Outcome.FAILED,
            describe(exc),
            balance_seen=balance_seen,
            collected_seen=collected_seen,
            planned_amount=planned_amount,
        )


def restore_block(account_id: str, api: BackofficeGateway, repo: AccountRepository) -> tuple[bool, str]:
    """Deja la cuenta bloqueada. Primero mira el estado (idempotencia por estado): si ya está
    bloqueada, no hace nada. Si no se puede leer, intenta bloquear igual: bloquear es idempotente."""
    try:
        snapshot, _ = call_with_retry(lambda: repo.get_account(account_id))
        if snapshot is not None and snapshot.is_blocked:
            return True, "La cuenta ya estaba bloqueada"
    except Exception as exc:
        log.warning("No se pudo leer la cuenta %s antes de re-bloquear: %s", account_id, describe(exc))
    try:
        call_with_retry(lambda: api.block_account(account_id, AUDIT_REASONS["reblock"]), policy=REBLOCK_POLICY)
        return True, "Cuenta re-bloqueada"
    except Exception as exc:
        log.error("FALLÓ EL RE-BLOQUEO de la cuenta %s: %s", account_id, describe(exc))
        return False, describe(exc)


def execute_account(
    plan: AccountPlan, api: BackofficeGateway, repo: AccountRepository, min_charge: Decimal
) -> list[DebtResult]:
    """Ejecuta el plan de UNA cuenta (patrón Saga con compensación garantizada).

    La cuenta bloqueada SIEMPRE vuelve a bloquearse.
    Se desbloquea UNA vez para todas las deudas de la cuenta: la ventana desbloqueada es la mínima."""

    results: list[DebtResult] = list(plan.not_charged)
    if not plan.to_charge:
        return results

    account_id = plan.account_id
    pending = list(plan.to_charge)
    executed: list[DebtResult] = []
    unblock_attempted = False
    try:
        assert plan.snapshot is not None  # noqa: S101 (un plan con cobros siempre tiene snapshot)
        if plan.snapshot.is_blocked:
            unblock_attempted = True  # se marca ANTES de llamar: si la respuesta es incierta,
            try:  # el finally igual verificará y re-bloqueará
                call_with_retry(lambda: api.unblock_account(account_id, AUDIT_REASONS["unblock"]))
            except Exception as exc:
                executed.extend(
                    DebtResult(
                        c.debt,
                        Outcome.FAILED,
                        f"No se pudo desbloquear la cuenta: {describe(exc)}",
                        balance_seen=plan.snapshot.balance,
                        planned_amount=c.amount,
                    )
                    for c in pending
                )
                pending = []

        while pending:
            order = pending.pop(0)
            result = charge_debt(order, api, repo, min_charge)
            result.was_unblocked = unblock_attempted
            executed.append(result)
            if result.uncertain:
                # Con un cobro en estado incierto no se sigue debitando esta cuenta: el saldo real es desconocido.
                executed.extend(
                    DebtResult(
                        c.debt,
                        Outcome.FAILED,
                        "No ejecutada: un cobro anterior de la cuenta quedó en estado incierto",
                        was_unblocked=unblock_attempted,
                        planned_amount=c.amount,
                    )
                    for c in pending
                )
                pending = []
    except Exception as exc:  # red de seguridad ante un bug: lo no ejecutado queda FAILED
        log.exception("Error inesperado en la cuenta %s", account_id)
        executed.extend(
            DebtResult(c.debt, Outcome.FAILED, f"Error inesperado: {describe(exc)}", planned_amount=c.amount)
            for c in pending
        )
    finally:
        if unblock_attempted:
            ok, detail = restore_block(account_id, api, repo)
            for result in executed:
                result.note(detail if ok else f"ATENCIÓN: falló el re-bloqueo, la cuenta quedó DESBLOQUEADA ({detail})")
                result.reblock_failed = not ok
    return results + executed


def execute_plans(
    plans: list[AccountPlan],
    api: BackofficeGateway,
    repo: AccountRepository,
    max_workers: int,
    min_charge: Decimal,
) -> list[DebtResult]:
    """Paralelismo por cuenta: cada hilo toma una cuenta completa. Así dos hilos nunca tocan la misma
    cuenta (por construcción, no por cuidado), y dentro de cada cuenta las deudas van en orden."""
    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="cuenta") as pool:
        per_account = list(pool.map(lambda p: execute_account(p, api, repo, min_charge), plans))
    return [result for account_results in per_account for result in account_results]
