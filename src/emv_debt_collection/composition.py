"""Composition root: el único lugar que decide qué implementaciones reales se usan.

El notebook de entrada llama a `run_job` con las dependencias de Databricks y las corporativas
(`dbutils`, `spark`, `conectar_mongo`, `desconectar_mongo`, `send_email`, `send_message`)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from emv_debt_collection.adapters.backoffice_api import BackofficeApi
from emv_debt_collection.adapters.delta_store import DeltaResultsStore
from emv_debt_collection.adapters.dev_json import JsonStore, MirroredApi
from emv_debt_collection.adapters.log_publisher import VolumeLogPublisher
from emv_debt_collection.adapters.mongo_repo import MongoRepo
from emv_debt_collection.adapters.notifier import Notifier
from emv_debt_collection.adapters.source_table import SparkDebtSource
from emv_debt_collection.application.run import RunDependencies, main
from emv_debt_collection.config import Config, EnvSettings, load_config
from emv_debt_collection.domain.models import RunReport
from emv_debt_collection.logging_setup import setup_logging
from emv_debt_collection.ports import (
    AccountRepository,
    BackofficeGateway,
    ConnectMongo,
    DisconnectMongo,
    SendEmail,
    SendMessage,
)


def open_connections(cfg: Config, connect_mongo: ConnectMongo) -> tuple[AccountRepository, BackofficeGateway, Any]:
    """Devuelve (repo, api, mongo_client). En dev con JSON no hay cliente que cerrar."""
    api = BackofficeApi(cfg)
    if cfg.dev_json_accounts:
        store = JsonStore.from_files(cfg.dev_json_accounts, cfg.dev_json_transactions)
        return store, MirroredApi(api, store), None
    db, client = connect_mongo(cfg.mongo_connection, cfg.mongo_secret, read=True)
    return MongoRepo(db), api, client


def run_job(
    *,
    env: str,
    dry_run: bool,
    run_date_raw: str | None,
    parameters: Mapping[str, str | None],
    spark: Any,
    dbutils: Any,
    conectar_mongo: ConnectMongo,
    desconectar_mongo: DisconnectMongo,
    send_email: SendEmail,
    send_message: SendMessage,
) -> RunReport:
    """Arma la configuración y las dependencias y ejecuta el proceso (mismo orden que el notebook original:
    primero la configuración con su secreto, luego el logging, luego main)."""
    cfg = load_config(env, dry_run, EnvSettings.from_parameters(parameters), dbutils.secrets)
    logger, local_log = setup_logging()
    deps = RunDependencies(
        source=SparkDebtSource(spark, cfg),
        connect=lambda: open_connections(cfg, conectar_mongo),
        disconnect=desconectar_mongo,
        results_store=DeltaResultsStore(spark, cfg),
        notifier=Notifier(cfg, send_email, send_message),
        log_publisher=VolumeLogPublisher(logger, local_log, cfg.logs_dir),
    )
    return main(cfg, run_date_raw, deps)


def report_rows(report: RunReport) -> list[dict[str, Any]]:
    """Filas para el `display` del notebook (salida visible en el run del job)."""
    return [
        {
            "account_id": x.debt.account_id,
            "transaction_id": x.debt.transaction_id,
            "settlement_date": x.debt.settlement_date,
            "original_amount": float(x.debt.original_amount),
            "collected_before": float(x.collected_seen) if x.collected_seen is not None else None,
            "debt_amount": float(x.debt.amount),
            "balance_seen": float(x.balance_seen) if x.balance_seen is not None else None,
            "outcome": x.outcome.value,
            "planned_amount": float(x.planned_amount),
            "charged_amount": float(x.charged_amount),
            "detail": x.detail,
        }
        for x in report.results
    ]


def failure_message(report: RunReport) -> str:
    """Mensaje del RuntimeError que deja el job en rojo."""
    return (
        f"Cobro EMV: status={report.status}, re-bloqueos fallidos={report.reblock_failures}, "
        f"error={report.fatal_error}, teams_error={report.teams_error}"
    )
