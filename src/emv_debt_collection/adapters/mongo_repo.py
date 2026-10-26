"""Repository de Mongo: lectura de cuentas y suma de cobros por deuda."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable
from decimal import Decimal, InvalidOperation
from functools import wraps
from typing import Any, ClassVar, ParamSpec, TypeVar

from bson import Decimal128, ObjectId
from pymongo.errors import PyMongoError

from emv_debt_collection.config import MONGO
from emv_debt_collection.domain.errors import BusinessError, TransientError
from emv_debt_collection.domain.models import AccountSnapshot
from emv_debt_collection.domain.reasons import REASON_MONGO_REGEX, debt_id_from_reason

P = ParamSpec("P")
R = TypeVar("R")


def _get_path(doc: Any, path: str) -> Any:
    for part in path.split("."):
        doc = doc.get(part) if isinstance(doc, dict) else None
    return doc


def _to_decimal(value: Any) -> Decimal:
    if value is None:
        raise BusinessError(f"La cuenta no tiene el campo de saldo '{MONGO['balance']}'")
    if isinstance(value, Decimal128):
        return value.to_decimal()
    try:
        return Decimal(str(value))  # str() evita arrastrar el error binario del float
    except InvalidOperation as exc:
        raise BusinessError(f"Saldo no numérico: {value!r}") from exc


def _id_variants(values: Iterable[Any]) -> list[Any]:
    """Un id puede estar guardado como ObjectId o como string: se busca por ambos."""
    out: list[Any] = []
    for value in values:
        for candidate in (value, str(value), ObjectId(str(value)) if ObjectId.is_valid(str(value)) else None):
            if candidate is not None and candidate not in out:
                out.append(candidate)
    return out


def mongo_call(fn: Callable[P, R]) -> Callable[P, R]:
    """Decorador: los errores de pymongo se traducen a TransientError (reintentables)."""

    @wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return fn(*args, **kwargs)
        except PyMongoError as exc:
            raise TransientError(f"Mongo: {exc}") from exc

    return wrapper


class MongoRepo:
    """Repository: todo lo que el proceso necesita leer de Mongo.

    Dos niveles:
    - Acceso crudo (`_account_docs`, `_charge_docs`): las ÚNICAS dos consultas a Mongo. Reciben listas,
      así que sirven igual para 1 id (verificación antes de cada débito) que para 500 (planificación).
      JsonStore solo reemplaza estos dos métodos.
    - Métodos de negocio (`get_accounts`, `get_account`, `collected_by_debt`, `collected_for_debt`): convierten
      documentos en objetos del dominio. Las reglas no saben de colecciones, proyecciones ni ObjectId."""

    ACCOUNT_PROJECTION: ClassVar[dict[str, int]] = {
        "_id": 1,
        MONGO["balance"]: 1,
        MONGO["currency"]: 1,
        MONGO["status"]: 1,
    }

    def __init__(self, db: Any):
        self.db = db

    # --- acceso crudo ---
    @mongo_call
    def _account_docs(self, account_ids: list[str]) -> list[dict[str, Any]]:
        """Búsqueda por _id (siempre indexado). Una consulta por bloque, no una por cuenta."""
        return list(
            self.db[MONGO["accounts"]].find({"_id": {"$in": _id_variants(account_ids)}}, self.ACCOUNT_PROJECTION)
        )

    @mongo_call
    def _charge_docs(self, account_ids: list[str]) -> list[dict[str, Any]]:
        """Cobros de deuda de estas cuentas: accountId (indexado) + reason terminado en /CxC.
        El regex solo achica el resultado; el TXID se extrae y compara en Python (debt_id_from_reason)."""
        return list(
            self.db[MONGO["transactions"]].find(
                {
                    MONGO["tx_account"]: {"$in": _id_variants(account_ids)},
                    MONGO["tx_reason"]: {"$regex": REASON_MONGO_REGEX},
                },
                {MONGO["tx_account"]: 1, MONGO["tx_reason"]: 1, MONGO["tx_amount"]: 1},
            )
        )

    # --- métodos de negocio ---
    @staticmethod
    def _snapshot(doc: dict[str, Any]) -> AccountSnapshot:
        currency = _get_path(doc, MONGO["currency"])
        if currency not in (None, MONGO["expected_currency"]):
            raise BusinessError(f"Moneda inesperada en la cuenta {doc['_id']}: {currency!r}")
        status = str(_get_path(doc, MONGO["status"]) or "").upper()
        if status not in MONGO["known_statuses"]:
            raise BusinessError(f"Status desconocido {status!r} en la cuenta {doc['_id']}")
        return AccountSnapshot(str(doc["_id"]), _to_decimal(_get_path(doc, MONGO["balance"])), status)

    def get_accounts(self, account_ids: list[str]) -> tuple[dict[str, AccountSnapshot], dict[str, str]]:
        """Lee un bloque de cuentas. Devuelve (snapshots, errores) por account_id.
        Una cuenta con datos raros (status desconocido, moneda) va a `errores` sin afectar a las demás.
        Las cuentas que no aparecen en ninguno de los dos no existen en Mongo."""
        snapshots: dict[str, AccountSnapshot] = {}
        errors: dict[str, str] = {}
        for doc in self._account_docs(account_ids):
            account_id = str(doc["_id"])
            try:
                snapshots[account_id] = self._snapshot(doc)
            except BusinessError as exc:
                errors[account_id] = str(exc)
        return snapshots, errors

    def get_account(self, account_id: str) -> AccountSnapshot | None:
        """Una sola cuenta (verificación justo antes de cada débito y del re-bloqueo)."""
        docs = self._account_docs([account_id])
        return self._snapshot(docs[0]) if docs else None

    def collected_by_debt(self, account_ids: list[str]) -> tuple[dict[str, dict[str, Decimal]], dict[str, str]]:
        """Para un bloque de cuentas: cuánto se ha cobrado de cada deuda, sumando todos sus cobros
        (parciales incluidos) agrupados por el TXID del reason. Es la misma regla del pipeline que calcula
        MONTO_COBRADO_PARCIAL. Devuelve ({cuenta: {txid: suma}}, {cuenta: error}).
        Un cobro con monto inválido marca a SU cuenta con error (no se puede saber cuánto se cobró)."""
        sums: defaultdict[str, defaultdict[str, Decimal]] = defaultdict(lambda: defaultdict(lambda: Decimal("0")))
        errors: dict[str, str] = {}
        for doc in self._charge_docs(account_ids):
            account_id = str(_get_path(doc, MONGO["tx_account"]))
            txid = debt_id_from_reason(_get_path(doc, MONGO["tx_reason"]))
            if txid is None:
                continue
            try:
                amount = _to_decimal(_get_path(doc, MONGO["tx_amount"]))
            except BusinessError as exc:
                errors[account_id] = f"Cobro {doc.get('_id')} con monto inválido ({exc})"
                continue
            if amount < 0 or amount != amount.to_integral_value():
                errors[account_id] = f"Cobro {doc.get('_id')} con monto inválido ({amount})"
                continue
            sums[account_id][txid] += amount
        return {a: dict(v) for a, v in sums.items()}, errors

    def collected_for_debt(self, account_id: str, transaction_id: str) -> Decimal:
        """Suma de cobros de UNA deuda (verificación justo antes del débito y conciliación)."""
        sums, errors = self.collected_by_debt([account_id])
        if str(account_id) in errors:
            raise BusinessError(errors[str(account_id)])
        return sums.get(str(account_id), {}).get(transaction_id, Decimal("0"))
