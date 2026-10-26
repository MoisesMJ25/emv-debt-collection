"""SOLO DEV: cuentas y transacciones desde exports JSON (adaptador alternativo, activado por configuración).

`load_config` impide usarlo fuera de dev: prod siempre lee Mongo."""

from __future__ import annotations

import logging
import threading
from decimal import Decimal
from pathlib import Path
from typing import Any

from bson import Decimal128, json_util

from emv_debt_collection.adapters.backoffice_api import BackofficeApi
from emv_debt_collection.adapters.mongo_repo import MongoRepo, _get_path, _to_decimal
from emv_debt_collection.config import MONGO, WITHDRAW
from emv_debt_collection.domain.reasons import REASON_SUFFIX

log = logging.getLogger(__name__)


def _load_extended_json(path: str) -> list[dict[str, Any]]:
    """Acepta arreglo JSON (Compass), un documento por línea (mongoexport) u objetos separados por coma."""
    text = Path(path).read_text(encoding="utf-8").strip()
    if not text:
        return []
    for candidate in (text, "[" + text.rstrip(",") + "]"):
        try:
            data = json_util.loads(candidate)
            return data if isinstance(data, list) else [data]
        except ValueError:
            continue
    return [json_util.loads(line.rstrip(",")) for line in text.splitlines() if line.strip()]


def _set_path(doc: dict[str, Any], path: str, value: Any) -> None:
    *parents, last = path.split(".")
    for part in parents:
        doc = doc.setdefault(part, {})
    doc[last] = value


class JsonStore(MongoRepo):
    """Mismo repositorio, otra fuente: los datos del JSON se validan con las mismas reglas que los de Mongo."""

    def __init__(
        self,
        accounts: list[dict[str, Any]],
        transactions: list[dict[str, Any]],
        accounts_path: str | None = None,
        transactions_path: str | None = None,
    ):
        super().__init__(db=None)
        self._accounts = {str(d["_id"]): d for d in accounts}
        self._transactions = list(transactions)
        self._paths = (accounts_path, transactions_path)
        self._lock = threading.RLock()

    def _persist(self) -> None:
        """Escribe el estado actual en los JSON (se llama con el lock tomado)."""
        accounts_path, transactions_path = self._paths
        if accounts_path:
            Path(accounts_path).write_text(json_util.dumps(list(self._accounts.values()), indent=2), encoding="utf-8")
        if transactions_path:
            Path(transactions_path).write_text(json_util.dumps(self._transactions, indent=2), encoding="utf-8")

    @classmethod
    def from_files(cls, accounts_path: str, transactions_path: str | None) -> JsonStore:
        accounts = _load_extended_json(accounts_path)
        transactions: list[dict[str, Any]] = []
        if transactions_path and Path(transactions_path).exists():
            transactions = _load_extended_json(transactions_path)
        elif transactions_path:
            log.warning("No existe %s: se asume que no hay cobros previos", transactions_path)
        log.warning(
            "DEV: datos desde JSON (%d cuentas, %d transacciones), NO desde Mongo", len(accounts), len(transactions)
        )
        return cls(accounts, transactions, accounts_path, transactions_path)

    # Solo se reemplaza el acceso crudo: get_accounts, get_account, collected_by_debt y collected_for_debt
    # se heredan de MongoRepo, así que en dev se prueba exactamente la misma lógica que en prod.
    def _account_docs(self, account_ids: list[str]) -> list[dict[str, Any]]:
        with self._lock:
            return [self._accounts[str(a)] for a in account_ids if str(a) in self._accounts]

    def _charge_docs(self, account_ids: list[str]) -> list[dict[str, Any]]:
        wanted_accounts = {str(a) for a in account_ids}
        with self._lock:
            return [
                t
                for t in self._transactions
                if str(_get_path(t, MONGO["tx_account"])) in wanted_accounts
                and str(_get_path(t, MONGO["tx_reason"]) or "").endswith(REASON_SUFFIX)
            ]

    # --- efectos reflejados por MirroredApi ---
    def set_status(self, account_id: str, status: str) -> None:
        with self._lock:
            if (doc := self._accounts.get(str(account_id))) is not None:
                _set_path(doc, MONGO["status"], status)
                self._persist()

    def record_charge(self, account_id: str, amount: Decimal, reason: str) -> None:
        with self._lock:
            if (doc := self._accounts.get(str(account_id))) is not None:
                # Se guarda como Decimal128 (igual que en Mongo) para que el JSON se pueda volver a escribir.
                new_balance = _to_decimal(_get_path(doc, MONGO["balance"])) - amount
                _set_path(doc, MONGO["balance"], Decimal128(str(new_balance)))
            transaction: dict[str, Any] = {"type": WITHDRAW["type"]}
            _set_path(transaction, MONGO["tx_amount"], int(amount))
            _set_path(transaction, MONGO["tx_account"], str(account_id))
            _set_path(transaction, MONGO["tx_reason"], reason)
            self._transactions.append(transaction)
            self._persist()


class MirroredApi:
    """Decorator: ejecuta la llamada real (API de dev) y, solo si fue exitosa, refleja el efecto en el JSON."""

    def __init__(self, api: BackofficeApi, store: JsonStore):
        self._api, self._store = api, store

    def block_account(self, account_id: str, reason: str) -> None:
        self._api.block_account(account_id, reason)
        self._store.set_status(account_id, "BLOCKED")

    def unblock_account(self, account_id: str, reason: str) -> None:
        self._api.unblock_account(account_id, reason)
        self._store.set_status(account_id, "ENABLED")

    def withdraw(self, account_id: str, amount: Decimal, reason: str) -> None:
        self._api.withdraw(account_id, amount, reason)
        self._store.record_charge(account_id, amount, reason)
