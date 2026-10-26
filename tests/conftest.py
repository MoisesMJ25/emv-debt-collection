"""Dobles de prueba compartidos. Nada aquí toca Databricks, Mongo real ni la red.

- Mongo           -> mongomock
- API backoffice  -> `responses`, con callbacks que simulan el backend sobre la misma base mongomock
- Spark/Delta     -> InMemoryDebtSource / InMemoryResultsStore (mismos Protocols que los adaptadores reales)
- dbutils, correo y Teams -> fakes que registran las llamadas
- Reintentos      -> sin esperas reales (time.sleep anulado)

Todos los datos son inventados: ObjectIds generados, TXIDs ficticios y montos de ejemplo.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

import mongomock
import pytest
import requests
import responses
from bson import Decimal128, ObjectId

from emv_debt_collection.adapters.backoffice_api import BackofficeApi
from emv_debt_collection.adapters.delta_store import results_rows, run_row
from emv_debt_collection.adapters.mongo_repo import MongoRepo
from emv_debt_collection.adapters.notifier import Notifier
from emv_debt_collection.application import retry
from emv_debt_collection.application.run import RunDependencies
from emv_debt_collection.config import Config, EnvSettings, load_config
from emv_debt_collection.domain.models import AccountSnapshot, Debt, RunReport

API_BASE_URL = "https://backoffice.example.test/api"


# ---------- ids y montos inventados ----------


def new_account_id() -> str:
    return str(ObjectId())


def new_txid() -> str:
    return f"TXTEST{uuid.uuid4().hex[:12].upper()}"


def make_debt(
    amount: int,
    *,
    account_id: str = "acc-test",
    transaction_id: str | None = None,
    settlement_date: date = date(2026, 9, 1),
    collected: int = 0,
    row_seq: int = 1,
) -> Debt:
    """Deuda válida: original = cobrado + restante."""
    return Debt(
        row_seq=row_seq,
        account_id=account_id,
        transaction_id=transaction_id or new_txid(),
        amount=Decimal(amount),
        settlement_date=settlement_date,
        original_amount=Decimal(amount + collected),
        collected_amount=Decimal(collected),
    )


def enabled(balance: int, account_id: str = "acc-test") -> AccountSnapshot:
    return AccountSnapshot(account_id, Decimal(balance), "ENABLED")


def source_row(debt_account: str, txid: str, remaining: int, collected: int, settlement: date) -> dict[str, Any]:
    """Fila cruda como la devuelve load_debts (después de toPandas().to_dict('records'))."""
    return {
        "account_id": debt_account,
        "transaction_id": txid,
        "original_amount": remaining + collected,
        "collected_amount": collected,
        "amount": remaining,
        "settlement_date": settlement,
    }


# ---------- fakes de Databricks y notificaciones ----------


class FakeSecrets:
    def __init__(self, values: dict[tuple[str, str], str]):
        self._values = values
        self.requested: list[tuple[str, str]] = []

    def get(self, scope: str, key: str) -> str:
        self.requested.append((scope, key))
        return self._values[(scope, key)]


class FakeDbutils:
    def __init__(self, secrets: dict[tuple[str, str], str]):
        self.secrets = FakeSecrets(secrets)


@dataclass
class RecordingSender:
    """Registra correos y mensajes de Teams en vez de enviarlos."""

    emails: list[tuple[str, str, str]] = field(default_factory=list)
    messages: list[tuple[str, str]] = field(default_factory=list)

    def send_email(self, email: str, subject: str, body: str) -> None:
        self.emails.append((email, subject, body))

    def send_message(self, message: str, chat_id: str) -> None:
        self.messages.append((message, chat_id))


class InMemoryDebtSource:
    def __init__(self, rows: list[dict[str, Any]]):
        self.rows = rows
        self.requested_dates: list[date] = []

    def load_debts(self, run_date: date) -> list[dict[str, Any]]:
        self.requested_dates.append(run_date)
        return [dict(r) for r in self.rows]


class InMemoryResultsStore:
    """Mismo contrato que DeltaResultsStore y mismas claves de MERGE: (run_id) y (run_id, row_seq)."""

    def __init__(self, env: str):
        self.env = env
        self.runs: dict[str, dict[str, Any]] = {}
        self.results: dict[tuple[str, int], dict[str, Any]] = {}

    def save_report(self, report: RunReport) -> None:
        for row in results_rows(report, self.env):
            self.results[(row["run_id"], row["row_seq"])] = row
        row = run_row(report, self.env)
        self.runs[row["run_id"]] = row


class NullLogPublisher:
    def publish(self) -> str | None:
        return None


# ---------- backend simulado (API + Mongo) ----------


@dataclass
class BackofficeCall:
    action: str  # "block" | "unblock" | "withdraw"
    account_id: str
    payload: dict[str, Any]


class FakeBackoffice:
    """Simula los efectos del backoffice sobre la base mongomock (status, saldo y transacción)."""

    def __init__(self, db: Any, rsps: responses.RequestsMock):
        self.db = db
        self.calls: list[BackofficeCall] = []
        self.withdraw_failure: Any = None  # callable(payload) -> Exception | None (después de aplicar o no)
        base = re.escape(API_BASE_URL)
        rsps.add_callback(responses.POST, re.compile(f"{base}/accounts/unblock/(.+)"), callback=self._unblock)
        rsps.add_callback(responses.POST, re.compile(f"{base}/accounts/block/(.+)"), callback=self._block)
        rsps.add_callback(responses.POST, f"{API_BASE_URL}/accounts/fund/manual", callback=self._withdraw)

    def _set_status(
        self, request: requests.PreparedRequest, action: str, status: str
    ) -> tuple[int, dict[str, str], str]:
        account_id = str(request.url).rsplit("/", 1)[-1]
        self.calls.append(BackofficeCall(action, account_id, json.loads(request.body or "{}")))
        self.db.accounts.update_one({"_id": ObjectId(account_id)}, {"$set": {"status": status}})
        return 200, {}, "{}"

    def _unblock(self, request: requests.PreparedRequest) -> tuple[int, dict[str, str], str]:
        return self._set_status(request, "unblock", "ENABLED")

    def _block(self, request: requests.PreparedRequest) -> tuple[int, dict[str, str], str]:
        return self._set_status(request, "block", "BLOCKED")

    def apply_withdraw(self, payload: dict[str, Any]) -> None:
        account = self.db.accounts.find_one({"_id": ObjectId(payload["accountId"])})
        balance = account["balance"]["amount"].to_decimal() - Decimal(payload["amount"])
        self.db.accounts.update_one({"_id": account["_id"]}, {"$set": {"balance.amount": Decimal128(str(balance))}})
        self.db.transactions.insert_one(
            {
                "accountId": payload["accountId"],
                "amount": payload["amount"],
                "type": payload["type"],
                "metadata": {"reason": payload["reason"]},
            }
        )

    def _withdraw(self, request: requests.PreparedRequest) -> Any:
        payload = json.loads(request.body or "{}")
        self.calls.append(BackofficeCall("withdraw", payload["accountId"], payload))
        if self.withdraw_failure is not None:
            return self.withdraw_failure(payload)  # responses lanza la excepción si el callback la devuelve
        self.apply_withdraw(payload)
        return 200, {}, "{}"

    def actions(self) -> list[str]:
        return [c.action for c in self.calls]


# ---------- fixtures ----------


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Las políticas de reintento y la conciliación corren sin esperas reales."""
    monkeypatch.setattr(retry.time, "sleep", lambda _seconds: None)


@pytest.fixture
def settings() -> EnvSettings:
    return EnvSettings.from_parameters(
        {
            "api_base_url": API_BASE_URL,
            "api_key_scope": "test-scope",
            "api_key_name": "test-api-key",
            "mongo_connection": "test-mongo-connection",
            "mongo_secret": "test-mongo-secret",
            "source_table": "test_catalog.test_schema.debts",
            "tables_schema": "test_catalog.test_schema",
            "operator_user": "operator@example.test",
            "logs_dir": "/tmp/emv-tests/logs",
            "email_recipients": "ops@example.test",
            "teams_chat_id": "test-chat",
        }
    )


@pytest.fixture
def cfg(settings: EnvSettings) -> Config:
    dbutils = FakeDbutils({("test-scope", "test-api-key"): "fake-api-key"})
    return load_config("dev", dry_run=False, settings=settings, secrets=dbutils.secrets)


@pytest.fixture
def mongo_db() -> Any:
    return mongomock.MongoClient().get_database("emv_test")


@pytest.fixture
def backoffice(mongo_db: Any) -> Iterator[FakeBackoffice]:
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        yield FakeBackoffice(mongo_db, rsps)


@pytest.fixture
def sender() -> RecordingSender:
    return RecordingSender()


def build_dependencies(
    cfg: Config,
    rows: list[dict[str, Any]],
    mongo_db: Any,
    sender: RecordingSender,
    store: InMemoryResultsStore,
    disconnected: list[Any],
) -> RunDependencies:
    """Mismas piezas que el composition root, con Mongo y API simulados."""
    client = object()
    return RunDependencies(
        source=InMemoryDebtSource(rows),
        connect=lambda: (MongoRepo(mongo_db), BackofficeApi(cfg), client),
        disconnect=disconnected.append,
        results_store=store,
        notifier=Notifier(cfg, sender.send_email, sender.send_message),
        log_publisher=NullLogPublisher(),
    )
