"""Orquestador de la ejecución: mismo orden de pasos que el notebook original, con dependencias inyectadas."""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from emv_debt_collection.adapters.source_table import validate_debts
from emv_debt_collection.application.executor import execute_plans
from emv_debt_collection.application.planner import build_run_plan, planned_results
from emv_debt_collection.config import Config
from emv_debt_collection.domain.errors import RunLimitExceeded
from emv_debt_collection.domain.models import Outcome, RunReport
from emv_debt_collection.domain.planning import check_run_limits
from emv_debt_collection.ports import (
    AccountRepository,
    BackofficeGateway,
    DebtSource,
    DisconnectMongo,
    LogPublisher,
    ResultsStore,
    RunNotifier,
)
from emv_debt_collection.shared import CHILE_TZ, clp, describe, now

log = logging.getLogger(__name__)

# Abre las conexiones reales (o las de dev con JSON). Devuelve (repo, api, mongo_client | None).
Connector = Callable[[], tuple[AccountRepository, BackofficeGateway, Any]]


@dataclass(frozen=True)
class RunDependencies:
    """Todo lo que `main` necesita del exterior. Lo arma el composition root (o los tests)."""

    source: DebtSource
    connect: Connector
    disconnect: DisconnectMongo
    results_store: ResultsStore
    notifier: RunNotifier
    log_publisher: LogPublisher


def parse_run_date(raw: str | None) -> date:
    """El parámetro del job manda; vacío = hoy en hora de Chile. Tolera '2026-10-19T08:00:00'."""
    raw = (raw or "").strip()
    return date.fromisoformat(raw[:10]) if raw else datetime.now(CHILE_TZ).date()


def main(cfg: Config, run_date_raw: str | None, deps: RunDependencies) -> RunReport:
    report = RunReport(run_id=uuid.uuid4().hex)
    client = None
    log.info("═" * 70)
    log.info("INICIO run=%s | env=%s | dry_run=%s", report.run_id, cfg.env, cfg.dry_run)
    try:
        report.run_date = parse_run_date(run_date_raw)
        started = time.monotonic()
        rows = deps.source.load_debts(report.run_date)
        report.debts_read = len(rows)
        log.info("Tabla origen leída | %d filas | %.1fs", len(rows), time.monotonic() - started)
        debts, excluded = validate_debts(rows)
        report.results.extend(excluded)
        log.info("Deudas leídas=%d | válidas=%d | excluidas=%d", len(rows), len(debts), len(excluded))
        if not debts:
            return report  # el finally igual persiste y notifica

        repo, api, client = deps.connect()
        if cfg.dev_json_accounts:
            report.warnings.append("DEV: cuentas y transacciones desde JSON, no desde Mongo")

        started = time.monotonic()
        plans = build_run_plan(debts, repo, cfg, report)
        report.planned_amount = sum((p.planned_amount for p in plans), Decimal("0"))
        log.info(
            "Plan | cuentas=%d | candidatas=%d | cobros=%d (parciales=%d) | monto=%s | %.1fs",
            len(plans),
            report.accounts_candidates,
            sum(len(p.to_charge) for p in plans),
            sum(c.is_partial for p in plans for c in p.to_charge),
            clp(report.planned_amount),
            time.monotonic() - started,
        )

        try:
            check_run_limits(plans, cfg.max_charge_per_run)
        except RunLimitExceeded as exc:
            report.results.extend(planned_results(plans, f"No ejecutada: {exc}"))
            raise

        if cfg.dry_run:
            report.results.extend(planned_results(plans, "Dry-run: se cobraría"))
            return report

        started = time.monotonic()
        report.results.extend(execute_plans(plans, api, repo, cfg.max_workers, cfg.min_charge_amount))
        log.info("Ejecución terminada | %.1fs", time.monotonic() - started)
    except Exception as exc:
        log.exception("Error general de la ejecución")
        report.fatal_error = describe(exc)
    finally:
        # SIEMPRE: cerrar Mongo -> log -> persistir -> notificar (cada paso aislado del siguiente)
        report.results.sort(key=lambda x: x.debt.row_seq)
        report.finished_at = now()
        if client is not None:
            try:
                deps.disconnect(client)
            except Exception as exc:
                log.warning("No se pudo cerrar la conexión a Mongo: %s", exc)
        log.info(
            "FIN run=%s | estado=%s | cobros=%d (saldadas=%d, parciales=%d) | monto=%s | "
            "re-bloqueos fallidos=%d | posibles sobrecobros=%d",
            report.run_id,
            report.status,
            report.charged_count,
            report.count(Outcome.CHARGED),
            report.count(Outcome.PARTIALLY_CHARGED),
            clp(report.charged_amount),
            report.reblock_failures,
            report.overcharged,
        )
        report.log_file = deps.log_publisher.publish()
        if not cfg.dry_run:  # dry-run: no se modifica ningún registro
            try:
                deps.results_store.save_report(report)
            except Exception as exc:
                log.exception("No se pudieron guardar los resultados")
                report.warnings.append(f"No se pudo guardar en Delta: {describe(exc)}")
        deps.notifier.notify(report)
    return report
