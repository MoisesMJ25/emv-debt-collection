"""debt_id_from_reason: solo '<TXID>/CxC' y '<TXID>/GO-445/CxC'; igualdad exacta del TXID."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import mongomock
import pytest
from bson import Decimal128, ObjectId

from emv_debt_collection.adapters.mongo_repo import MongoRepo
from emv_debt_collection.domain.reasons import build_reason, debt_id_from_reason


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("TXTEST001/CxC", "TXTEST001"),  # formato actual
        ("TXTEST001/GO-445/CxC", "TXTEST001"),  # formato histórico
        (" TXTEST001 /CxC", "TXTEST001"),  # el TXID se limpia de espacios
    ],
)
def test_valid_formats(reason: str, expected: str) -> None:
    assert debt_id_from_reason(reason) == expected


@pytest.mark.parametrize(
    "reason",
    [
        "TXTEST001",  # sin /CxC
        "TXTEST001/GO-445",  # sin /CxC
        "TXTEST001/GO-445/X/CxC",  # más segmentos
        "/CxC",  # TXID vacío
        "   /CxC",  # TXID en blanco
        "",  # vacío
        None,  # None
        "TXTEST001/cxc",  # sufijo en minúsculas
        "TXTEST001/CXC",  # sufijo en mayúsculas
        12345,  # no es string
    ],
)
def test_invalid_formats(reason: Any) -> None:
    assert debt_id_from_reason(reason) is None


def test_build_reason_round_trip() -> None:
    assert build_reason("TXTEST001") == "TXTEST001/CxC"
    assert debt_id_from_reason(build_reason("TXTEST001")) == "TXTEST001"


def test_no_false_positive_between_prefix_txids() -> None:
    """'a1' no debe calzar con 'a10': la suma se agrupa por TXID exacto, no por prefijo."""
    db = mongomock.MongoClient().get_database("emv_test")
    account_id = ObjectId()
    db.accounts.insert_one({"_id": account_id, "balance": {"amount": Decimal128("0"), "currency": "CLP"}})
    db.transactions.insert_many(
        [
            {"accountId": str(account_id), "amount": 100, "metadata": {"reason": "a1/CxC"}},
            {"accountId": str(account_id), "amount": 900, "metadata": {"reason": "a10/CxC"}},
            {"accountId": account_id, "amount": 50, "metadata": {"reason": "a1/GO-445/CxC"}},  # accountId ObjectId
            {"accountId": str(account_id), "amount": 7, "metadata": {"reason": "a1/cxc"}},  # no se suma
        ]
    )
    repo = MongoRepo(db)

    assert repo.collected_for_debt(str(account_id), "a1") == Decimal("150")
    assert repo.collected_for_debt(str(account_id), "a10") == Decimal("900")
    assert repo.collected_for_debt(str(account_id), "a") == Decimal("0")
