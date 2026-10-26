"""Puertos (Protocols): los contratos entre la aplicación y el mundo exterior.

Incluye las dependencias corporativas que NO viven en este repo y se inyectan desde el notebook de entrada:
- `conectar_mongo` / `desconectar_mongo`: vienen del `%run` corporativo de conexión a Mongo.
- `send_email` / `send_message`: vienen del módulo `utils` de Databricks.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Protocol

from emv_debt_collection.domain.models import AccountSnapshot, RunReport

# --- dependencias de Databricks y corporativas ---


class SecretsProvider(Protocol):
    """Lo que se usa de `dbutils.secrets`."""

    def get(self, scope: str, key: str) -> str: ...


class ConnectMongo(Protocol):
    """`conectar_mongo(conexion, secreto, read=True) -> (db, client)` del %run corporativo."""

    def __call__(self, connection: str, secret: str, /, *, read: bool) -> tuple[Any, Any]: ...


class DisconnectMongo(Protocol):
    """`desconectar_mongo(client)` del %run corporativo."""

    def __call__(self, client: Any, /) -> Any: ...


class SendEmail(Protocol):
    """`utils.send_email(destinatario, asunto, cuerpo_html)`."""

    def __call__(self, email: str, subject: str, body: str, /) -> Any: ...


class SendMessage(Protocol):
    """`utils.send_message(mensaje_html, chat_id)` (Teams)."""

    def __call__(self, message: str, chat_id: str, /) -> Any: ...


# --- puertos del proceso ---


class AccountRepository(Protocol):
    """Repository de lectura de cuentas y cobros (MongoRepo en prod, JsonStore en dev)."""

    def get_accounts(self, account_ids: list[str]) -> tuple[dict[str, AccountSnapshot], dict[str, str]]: ...

    def get_account(self, account_id: str) -> AccountSnapshot | None: ...

    def collected_by_debt(self, account_ids: list[str]) -> tuple[dict[str, dict[str, Decimal]], dict[str, str]]: ...

    def collected_for_debt(self, account_id: str, transaction_id: str) -> Decimal: ...


class BackofficeGateway(Protocol):
    """Operaciones del backoffice (BackofficeApi en prod, MirroredApi en dev con JSON)."""

    def block_account(self, account_id: str, reason: str) -> None: ...

    def unblock_account(self, account_id: str, reason: str) -> None: ...

    def withdraw(self, account_id: str, amount: Decimal, reason: str) -> None: ...


class DebtSource(Protocol):
    """Lectura de las filas crudas de la tabla de deudas."""

    def load_debts(self, run_date: date) -> list[dict[str, Any]]: ...


class ResultsStore(Protocol):
    """Persistencia del reporte (Delta en Databricks)."""

    def save_report(self, report: RunReport) -> None: ...


class RunNotifier(Protocol):
    """Correo + Teams. Debe dejar `report.teams_error` si Teams falla."""

    def notify(self, report: RunReport) -> None: ...


class LogPublisher(Protocol):
    """Copia el log local a su destino final. Devuelve la ruta publicada o None si falló."""

    def publish(self) -> str | None: ...
