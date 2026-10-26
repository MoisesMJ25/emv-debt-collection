"""Tabla origen de deudas: lectura con SQL parametrizado y validación de filas en el borde."""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import pandas as pd

from emv_debt_collection.config import Config
from emv_debt_collection.domain.errors import SourceError
from emv_debt_collection.domain.models import Debt, DebtResult, Outcome
from emv_debt_collection.shared import describe


def load_debts(spark: Any, cfg: Config, run_date: date) -> list[dict[str, Any]]:
    """Lee las deudas pendientes. Trae la deuda restante (no el LEAST con el saldo de la tabla):
    el saldo que decide es el de Mongo en vivo. La fecha de corte es un parámetro, no CURRENT_DATE(),
    para que una reejecución de un día pasado use el mismo corte."""
    sql = f"""
        SELECT
            ACCOUNT_ID_          AS account_id,
            TRANSACTION_ID       AS transaction_id,
            CLP_AUTORIZACIONES    AS original_amount,
            MONTO_COBRADO_PARCIAL AS collected_amount,
            MONTO_DEUDA_RESTANTE AS amount,
            FECHA_LIQUIDACION    AS settlement_date
        FROM {cfg.source_table}
        WHERE MONTO_DEUDA_RESTANTE > 0
          --LEAST(SALDO_ACTUAL, MONTO_DEUDA_RESTANTE) > 0
          AND FECHA_LIQUIDACION <= date_sub(:run_date, :settlement_days)
          --LIMIT 10
    """  # noqa: S608 (el nombre de tabla viene de la configuración; los valores van como parámetros)
    try:
        df = spark.sql(sql, args={"run_date": run_date, "settlement_days": cfg.settlement_days}).toPandas()
    except Exception as exc:
        raise SourceError(f"No se pudo leer {cfg.source_table}: {describe(exc)}") from exc
    records: list[dict[str, Any]] = df.to_dict("records")
    return records


class SparkDebtSource:
    """Adaptador DebtSource sobre Spark SQL."""

    def __init__(self, spark: Any, cfg: Config):
        self._spark = spark
        self._cfg = cfg

    def load_debts(self, run_date: date) -> list[dict[str, Any]]:
        return load_debts(self._spark, self._cfg, run_date)


def _to_date(value: Any) -> date:
    if isinstance(value, datetime):  # pd.Timestamp también es datetime
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _parse_debt(row_seq: int, row: dict[str, Any]) -> Debt:
    """Convierte una fila cruda en Debt o lanza ValueError con el motivo."""

    def clean(key: str) -> Any:
        value = row.get(key)
        return None if value is None or (not isinstance(value, str) and pd.isna(value)) else value

    def money(key: str, label: str) -> Decimal:
        value = clean(key)
        if value is None:
            raise ValueError(f"{label} vacío")
        try:
            amount = Decimal(str(value))
        except InvalidOperation:
            raise ValueError(f"{label} no numérico: {value!r}") from None
        if amount < 0:
            raise ValueError(f"{label} negativo: {amount}")
        if amount != amount.to_integral_value():
            raise ValueError(f"{label} con decimales: {amount}")
        return amount

    account_id, transaction_id = clean("account_id"), clean("transaction_id")
    if not account_id or not str(account_id).strip():
        raise ValueError("account_id vacío")
    if not transaction_id or not str(transaction_id).strip():
        raise ValueError("transaction_id vacío")
    if "/" in str(transaction_id):
        raise ValueError("transaction_id contiene '/': no se puede asociar a su metadata.reason")
    remaining = money("amount", "MONTO_DEUDA_RESTANTE")
    original = money("original_amount", "CLP_AUTORIZACIONES")
    collected = money("collected_amount", "MONTO_COBRADO_PARCIAL")
    if remaining <= 0:
        raise ValueError(f"MONTO_DEUDA_RESTANTE no positivo: {remaining}")
    if original != collected + remaining:
        raise ValueError(
            f"montos inconsistentes: CLP_AUTORIZACIONES {original} != "
            f"MONTO_COBRADO_PARCIAL {collected} + MONTO_DEUDA_RESTANTE {remaining}"
        )
    if clean("settlement_date") is None:
        raise ValueError("fecha de liquidación vacía")
    return Debt(
        row_seq,
        str(account_id).strip(),
        str(transaction_id).strip(),
        remaining,
        _to_date(row["settlement_date"]),
        original,
        collected,
    )


def validate_debts(rows: list[dict[str, Any]]) -> tuple[list[Debt], list[DebtResult]]:
    """Devuelve (deudas válidas, filas excluidas). Validar en el borde: desde aquí en adelante,
    todo el código trabaja con Debt limpios y puede confiar en ellos.

    Duplicados: si un TRANSACTION_ID aparece más de una vez, la tabla está inconsistente para esa
    deuda. Se excluyen TODAS sus filas (no se adivina cuál es la correcta) y el resto sigue."""
    parsed: list[Debt] = []
    excluded: list[DebtResult] = []
    for seq, row in enumerate(rows, start=1):
        try:
            parsed.append(_parse_debt(seq, row))
        except ValueError as exc:
            raw = Debt(seq, str(row.get("account_id") or ""), str(row.get("transaction_id") or ""), Decimal("0"), None)
            excluded.append(DebtResult(raw, Outcome.EXCLUDED, f"Fila inválida en la tabla origen: {exc}"))

    occurrences = Counter(d.transaction_id for d in parsed)
    valid: list[Debt] = []
    for debt in parsed:
        if occurrences[debt.transaction_id] > 1:
            excluded.append(
                DebtResult(
                    debt,
                    Outcome.EXCLUDED,
                    f"TRANSACTION_ID duplicado en la tabla origen ({occurrences[debt.transaction_id]} filas): "
                    "no se cobra hasta corregir la tabla",
                )
            )
        else:
            valid.append(debt)
    return valid, excluded
