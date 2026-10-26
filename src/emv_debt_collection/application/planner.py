"""Capa de efectos alrededor del núcleo de planificación: lecturas en lote y armado del plan de la ejecución."""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from decimal import Decimal

from emv_debt_collection.application.retry import BULK_POLICY, call_with_retry
from emv_debt_collection.config import Config
from emv_debt_collection.domain.models import (
    AccountPlan,
    AccountSnapshot,
    Debt,
    DebtResult,
    Outcome,
    RunReport,
)
from emv_debt_collection.domain.planning import account_budget, charge_floor, debt_priority, plan_account
from emv_debt_collection.ports import AccountRepository
from emv_debt_collection.shared import chunks, clp

log = logging.getLogger(__name__)


def build_run_plan(debts: list[Debt], repo: AccountRepository, cfg: Config, report: RunReport) -> list[AccountPlan]:
    """CAPA DE EFECTOS alrededor del núcleo. Tres pasos, todos en lote:

    1. Agrupa las deudas por cuenta (la cuenta es la unidad de trabajo de todo el proceso).
    2. Lee TODAS las cuentas en bloques de `mongo_batch_size` con $in: ~25 consultas en vez de ~12.000.
       Sin filtrar por saldo en Mongo: así se distingue "sin saldo" de "no existe" o "DISABLED/DELETED".
    3. Filtro en Python: son CANDIDATAS las cuentas cobrables con presupuesto (saldo, limitado por el tope)
       de al menos el cobro mínimo. Solo para ellas se suman los cobros previos en `transactions` (en lote).

    La decisión final sigue siendo de plan_account (función pura)."""
    by_account: dict[str, list[Debt]] = defaultdict(list)
    for debt in debts:
        by_account[debt.account_id].append(debt)
    account_ids = sorted(by_account)
    report.accounts_total = len(account_ids)

    # Paso 2: cuentas en lote
    started = time.monotonic()
    snapshots: dict[str, AccountSnapshot] = {}
    errors: dict[str, str] = {}
    for n, block in enumerate(chunks(account_ids, cfg.mongo_batch_size), start=1):
        # La lambda se ejecuta dentro de la misma iteración: capturar `block` es seguro.
        (found, bad), _ = call_with_retry(lambda: repo.get_accounts(block), policy=BULK_POLICY)  # noqa: B023
        snapshots.update(found)
        errors.update(bad)
        log.info(
            "Cuentas leídas: bloque %d | acumulado %d/%d",
            n,
            min(n * cfg.mongo_batch_size, len(account_ids)),
            len(account_ids),
        )
    log.info(
        "Mongo accounts: %d encontradas, %d con datos inválidos, %d inexistentes | %.1fs",
        len(snapshots),
        len(errors),
        len(account_ids) - len(snapshots) - len(errors),
        time.monotonic() - started,
    )

    # Paso 3: candidatas y lo ya cobrado de cada deuda (solo para ellas)
    floor = charge_floor(cfg.min_charge_amount)
    candidates = [
        a
        for a in account_ids
        if (snap := snapshots.get(a)) is not None
        and snap.is_chargeable
        and account_budget(snap, cfg.max_charge_per_account) >= floor
    ]
    report.accounts_candidates = len(candidates)
    started = time.monotonic()
    collected: dict[str, dict[str, Decimal]] = {}
    for block in chunks(candidates, cfg.mongo_batch_size):
        (sums, bad), _ = call_with_retry(lambda: repo.collected_by_debt(block), policy=BULK_POLICY)  # noqa: B023
        collected.update(sums)
        errors.update(bad)
    log.info(
        "Candidatas (con saldo): %d de %d | cuentas con cobros previos: %d | %.1fs",
        len(candidates),
        len(account_ids),
        len(collected),
        time.monotonic() - started,
    )

    candidate_set = set(candidates)
    plans: list[AccountPlan] = []
    for account_id in account_ids:
        account_debts = by_account[account_id]
        if account_id in errors:
            plan = AccountPlan(account_id, None)
            plan.not_charged = [
                DebtResult(d, Outcome.EXCLUDED, f"{errors[account_id]}: revisión manual")
                for d in sorted(account_debts, key=debt_priority)
            ]
            plans.append(plan)
            continue
        plans.append(
            plan_account(
                account_id,
                account_debts,
                snapshots.get(account_id),
                collected.get(account_id, {}) if account_id in candidate_set else None,
                cfg.max_charge_per_account,
                cfg.min_charge_amount,
            )
        )
    return plans


def planned_results(plans: list[AccountPlan], detail: str) -> list[DebtResult]:
    """Convierte el plan en resultados sin ejecutar nada (dry-run, o ejecución detenida por el tope)."""
    results: list[DebtResult] = []
    for plan in plans:
        results.extend(plan.not_charged)
        balance = plan.snapshot.balance if plan.snapshot else None
        for c in plan.to_charge:
            kind = (
                f"cobro parcial {clp(c.amount)} de {clp(c.debt.amount)}"
                if c.is_partial
                else f"cobro total {clp(c.amount)}"
            )
            results.append(
                DebtResult(
                    c.debt,
                    Outcome.PLANNED,
                    f"{detail} ({kind})",
                    balance_seen=balance,
                    collected_seen=c.debt.collected_amount,
                    planned_amount=c.amount,
                )
            )
    return results
