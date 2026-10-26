"""Modelos del dominio: deudas, plan, resultados y reporte de la ejecución."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import Enum

from emv_debt_collection.config import MONGO
from emv_debt_collection.domain.reasons import build_reason
from emv_debt_collection.shared import now


@dataclass(frozen=True)
class Debt:
    """Una fila válida de la tabla de deudas. Inmutable: lo que leímos no cambia durante el proceso.

    Las tres columnas de monto cumplen: original_amount = collected_amount + amount (se valida al leer)."""

    row_seq: int  # posición en la lectura: clave estable para la tabla de resultados
    account_id: str
    transaction_id: str
    amount: Decimal  # MONTO_DEUDA_RESTANTE: lo que falta cobrar según la tabla (objetivo de hoy)
    settlement_date: date | None
    original_amount: Decimal = Decimal("0")  # CLP_AUTORIZACIONES: monto íntegro de la deuda
    collected_amount: Decimal = Decimal("0")  # MONTO_COBRADO_PARCIAL: lo ya recuperado según la tabla

    @property
    def reason(self) -> str:
        """Lo que se envía en el retiro. Ya NO es único: todos los cobros parciales de una deuda llevan el
        mismo reason. Es el "hilo conductor" con el que el pipeline (y este proceso) suman lo cobrado."""
        return build_reason(self.transaction_id)


@dataclass(frozen=True)
class PlannedCharge:
    """Un cobro decidido en el plan: la deuda y CUÁNTO se le cobrará hoy (todo lo que falta o una parte)."""

    debt: Debt
    amount: Decimal

    @property
    def is_partial(self) -> bool:
        return self.amount < self.debt.amount


@dataclass(frozen=True)
class AccountSnapshot:
    """Foto de la cuenta en Mongo en un instante."""

    account_id: str
    balance: Decimal
    status: str

    @property
    def is_blocked(self) -> bool:
        return self.status in MONGO["blocked_values"]

    @property
    def is_chargeable(self) -> bool:
        return self.status in MONGO["chargeable_statuses"]


class Outcome(str, Enum):  # noqa: UP042 (StrEnum cambiaría str()/format respecto del original)
    """Resultado de cada deuda. Más descriptivo que SUCCESS/FAILED: Operaciones entiende qué pasó."""

    PLANNED = "PLANNED"  # dry-run o ejecución detenida por el tope: se habría cobrado
    CHARGED = "CHARGED"  # se cobró todo lo que faltaba: la deuda quedó saldada
    PARTIALLY_CHARGED = "PARTIALLY_CHARGED"  # se cobró una parte; el resto queda para otra ejecución
    ALREADY_CHARGED = "ALREADY_CHARGED"  # la suma de cobros en Mongo ya cubre la deuda (idempotencia)
    NOT_ENOUGH_FUNDS = "NOT_ENOUGH_FUNDS"  # no queda saldo/presupuesto (o el cobro sería menor al mínimo)
    EXCLUDED = "EXCLUDED"  # no se opera: cuenta DISABLED/DELETED, fila inválida, duplicada,
    # o lo cobrado en Mongo no coincide con MONTO_COBRADO_PARCIAL
    FAILED = "FAILED"  # se intentó y falló (API, Mongo, cobro incierto)


@dataclass
class DebtResult:
    """Lo que pasó con una deuda. Es una fila de la tabla de resultados."""

    debt: Debt
    outcome: Outcome
    detail: str
    balance_seen: Decimal | None = None  # saldo leído justo antes de decidir
    collected_seen: Decimal | None = None  # suma de cobros previos de la deuda vista en Mongo
    planned_amount: Decimal = Decimal("0")  # lo que el plan decidió cobrar
    charged_amount: Decimal = Decimal("0")
    attempts: int = 0
    was_unblocked: bool = False
    reblock_failed: bool = False
    processed_at: datetime = field(default_factory=now)
    uncertain: bool = False  # interno: cobro ambiguo sin confirmar -> no seguir con esa cuenta
    overcharged: bool = False  # lo cobrado en Mongo supera el monto original: alerta crítica

    def note(self, text: str) -> None:
        self.detail = f"{self.detail} | {text}"

    @property
    def remaining_after(self) -> Decimal | None:
        """Deuda restante después de este cobro (solo cuando hubo cobro)."""
        if self.outcome in (Outcome.CHARGED, Outcome.PARTIALLY_CHARGED):
            return self.debt.amount - self.charged_amount
        return None


@dataclass
class AccountPlan:
    """Qué se va a hacer con una cuenta: cobros a ejecutar (en orden, con su monto) y deudas que no se cobrarán."""

    account_id: str
    snapshot: AccountSnapshot | None
    to_charge: list[PlannedCharge] = field(default_factory=list)
    not_charged: list[DebtResult] = field(default_factory=list)

    @property
    def planned_amount(self) -> Decimal:
        return sum((c.amount for c in self.to_charge), Decimal("0"))


@dataclass
class RunReport:
    """El objeto que viaja por todo el proceso. El finally lo usa para persistir y notificar."""

    run_id: str
    started_at: datetime = field(default_factory=now)
    run_date: date | None = None
    debts_read: int = 0
    planned_amount: Decimal = Decimal("0")
    accounts_total: int = 0  # cuentas distintas con deuda válida
    accounts_candidates: int = 0  # presupuesto ≥ cobro mínimo.
    results: list[DebtResult] = field(default_factory=list)
    fatal_error: str | None = None
    warnings: list[str] = field(default_factory=list)
    finished_at: datetime | None = None
    log_file: str | None = None
    teams_error: str | None = None

    def count(self, outcome: Outcome) -> int:
        return sum(r.outcome == outcome for r in self.results)

    @property
    def charged_amount(self) -> Decimal:
        return sum((r.charged_amount for r in self.results), Decimal("0"))

    @property
    def charged_count(self) -> int:
        """Retiros efectivos: deudas saldadas + cobros parciales."""
        return self.count(Outcome.CHARGED) + self.count(Outcome.PARTIALLY_CHARGED)

    @property
    def overcharged(self) -> int:
        return sum(r.overcharged for r in self.results)

    @property
    def reblock_failures(self) -> int:
        """Cuentas que quedaron desbloqueadas: es el error más grave del proceso."""
        return len({r.debt.account_id for r in self.results if r.reblock_failed})

    @property
    def status(self) -> str:
        if self.fatal_error:
            return "FAILED"
        failed = self.count(Outcome.FAILED)
        if failed == 0 and self.reblock_failures == 0:
            return "SUCCESS"  # sin deudas, sin saldo o todo cobrado también es éxito
        attempted = failed + self.charged_count
        return "FAILED" if failed and failed == attempted else "PARTIAL"

    @property
    def should_fail_job(self) -> bool:
        """El job queda en rojo si el run falló, si alguna cuenta quedó desbloqueada o si Teams falló."""
        return self.status == "FAILED" or self.reblock_failures > 0 or self.teams_error is not None
