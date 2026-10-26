"""Delta: tabla de ejecuciones y tabla de resultado por deuda.

Las tablas NO se crean con Terraform: `ensure_tables` las crea y les agrega columnas (evolución aditiva)."""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal
from typing import Any

from emv_debt_collection.config import Config
from emv_debt_collection.domain.models import Outcome, RunReport
from emv_debt_collection.shared import now

log = logging.getLogger(__name__)

# Esquema como lista de (columna, tipo): permite agregar columnas nuevas a tablas ya creadas (ver ensure_tables).
DDL: dict[str, list[tuple[str, str]]] = {
    "runs_table": [
        ("run_id", "STRING"),
        ("env", "STRING"),
        ("run_date", "DATE"),
        ("status", "STRING"),
        ("debts_read", "INT"),
        ("charged_count", "INT"),
        ("charged_amount", "DECIMAL(18,2)"),
        ("planned_amount", "DECIMAL(18,2)"),
        ("failed_count", "INT"),
        ("reblock_failures", "INT"),
        ("fatal_error", "STRING"),
        ("warnings", "ARRAY<STRING>"),
        ("log_file", "STRING"),
        ("started_at", "TIMESTAMP"),
        ("finished_at", "TIMESTAMP"),
        # v2: cobros parciales
        ("partial_count", "INT"),
        ("overcharged_count", "INT"),
    ],
    "results_table": [
        ("run_id", "STRING"),
        ("env", "STRING"),
        ("run_date", "DATE"),
        ("row_seq", "INT"),
        ("account_id", "STRING"),
        ("transaction_id", "STRING"),
        ("reason", "STRING"),
        ("settlement_date", "DATE"),
        ("debt_amount", "DECIMAL(18,2)"),
        ("balance_seen", "DECIMAL(18,2)"),
        ("outcome", "STRING"),
        ("detail", "STRING"),
        ("charged_amount", "DECIMAL(18,2)"),
        ("attempts", "INT"),
        ("was_unblocked", "BOOLEAN"),
        ("reblock_failed", "BOOLEAN"),
        ("processed_at", "TIMESTAMP"),
        # v2: cobros parciales
        ("original_amount", "DECIMAL(18,2)"),
        ("collected_before", "DECIMAL(18,2)"),
        ("planned_amount", "DECIMAL(18,2)"),
        ("remaining_after", "DECIMAL(18,2)"),
        ("overcharged", "BOOLEAN"),
    ],
}
# row_seq y no transaction_id: un TXID duplicado en la tabla origen generaría dos filas con la misma clave.
KEYS = {"runs_table": ["run_id"], "results_table": ["run_id", "row_seq"]}


def _money(value: Decimal | None) -> Decimal | None:
    return None if value is None else value.quantize(Decimal("0.01"))


def results_rows(r: RunReport, env: str) -> list[dict[str, Any]]:
    """Filas de la tabla de resultados (una por deuda)."""
    return [
        {
            "run_id": r.run_id,
            "env": env,
            "run_date": r.run_date,
            "row_seq": x.debt.row_seq,
            "account_id": x.debt.account_id,
            "transaction_id": x.debt.transaction_id,
            "reason": x.debt.reason,
            "settlement_date": x.debt.settlement_date,
            "debt_amount": _money(x.debt.amount),
            "balance_seen": _money(x.balance_seen),
            "outcome": x.outcome.value,
            "detail": x.detail,
            "charged_amount": _money(x.charged_amount),
            "attempts": x.attempts,
            "was_unblocked": x.was_unblocked,
            "reblock_failed": x.reblock_failed,
            "processed_at": x.processed_at,
            "original_amount": _money(x.debt.original_amount),
            "collected_before": _money(x.collected_seen),
            "planned_amount": _money(x.planned_amount),
            "remaining_after": _money(x.remaining_after),
            "overcharged": x.overcharged,
        }
        for x in r.results
    ]


def run_row(r: RunReport, env: str) -> dict[str, Any]:
    """Fila de la tabla de ejecuciones."""
    return {
        "run_id": r.run_id,
        "env": env,
        "run_date": r.run_date,
        "status": r.status,
        "debts_read": r.debts_read,
        "charged_count": r.charged_count,
        "partial_count": r.count(Outcome.PARTIALLY_CHARGED),
        "overcharged_count": r.overcharged,
        "charged_amount": _money(r.charged_amount),
        "planned_amount": _money(r.planned_amount),
        "failed_count": r.count(Outcome.FAILED),
        "reblock_failures": r.reblock_failures,
        "fatal_error": r.fatal_error,
        "warnings": r.warnings,
        "log_file": r.log_file,
        "started_at": r.started_at,
        "finished_at": r.finished_at or now(),
    }


class DeltaResultsStore:
    """Adaptador ResultsStore sobre Delta (Spark SQL + MERGE idempotente)."""

    def __init__(self, spark: Any, cfg: Config):
        self.spark = spark
        self.cfg = cfg
        self._tables_ready = False

    def ensure_tables(self) -> None:
        """Crea las tablas si no existen y AGREGA las columnas nuevas a las ya creadas (evolución aditiva:
        nunca borra ni cambia tipos). Así la v2 funciona sobre las tablas que ya tienen datos de la v1."""
        if self._tables_ready:
            return
        for attr, columns in DDL.items():
            table = getattr(self.cfg, attr)
            self.spark.sql(
                f"CREATE TABLE IF NOT EXISTS {table} ({', '.join(f'{n} {t}' for n, t in columns)}) USING DELTA"
            )
            existing = {f.name.lower() for f in self.spark.table(table).schema.fields}
            missing = [(n, t) for n, t in columns if n.lower() not in existing]
            if missing:
                self.spark.sql(f"ALTER TABLE {table} ADD COLUMNS ({', '.join(f'{n} {t}' for n, t in missing)})")
                log.info("Columnas agregadas a %s: %s", table, ", ".join(n for n, _ in missing))
        self._tables_ready = True

    def _merge(self, attr: str, rows: list[dict[str, Any]]) -> None:
        """MERGE idempotente: guardar dos veces el mismo run no duplica filas."""
        if not rows:
            return
        table = getattr(self.cfg, attr)
        schema = self.spark.table(table).schema
        df = self.spark.createDataFrame([tuple(r[f.name] for f in schema.fields) for r in rows], schema=schema)
        view = f"_src_{attr}_{uuid.uuid4().hex[:8]}"
        df.createOrReplaceTempView(view)
        on = " AND ".join(f"t.{k} = s.{k}" for k in KEYS[attr])
        self.spark.sql(
            f"MERGE INTO {table} t USING {view} s ON {on} WHEN MATCHED THEN UPDATE SET * WHEN NOT MATCHED THEN INSERT *"
        )
        self.spark.catalog.dropTempView(view)

    def save_report(self, report: RunReport) -> None:
        self.ensure_tables()
        self._merge("results_table", results_rows(report, self.cfg.env))
        self._merge("runs_table", [run_row(report, self.cfg.env)])
        log.info("Resultados guardados en Delta | run=%s", report.run_id)
