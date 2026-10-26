"""Retiro con respuesta ambigua (timeout de lectura): conciliación por suma, sin reintentar el retiro."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import requests
from bson import Decimal128, ObjectId

from emv_debt_collection.application.run import main
from emv_debt_collection.config import Config

from conftest import (
    FakeBackoffice,
    InMemoryResultsStore,
    RecordingSender,
    build_dependencies,
    new_txid,
    source_row,
)


def _seed(mongo_db: Any, balance: int, prior: list[tuple[str, int]]) -> str:
    account = ObjectId()
    mongo_db.accounts.insert_one(
        {"_id": account, "status": "ENABLED", "balance": {"amount": Decimal128(str(balance)), "currency": "CLP"}}
    )
    for txid, amount in prior:
        mongo_db.transactions.insert_one(
            {"accountId": str(account), "amount": amount, "metadata": {"reason": f"{txid}/CxC"}}
        )
    return str(account)


def test_ambiguous_withdraw_that_was_applied_is_confirmed(
    cfg: Config, mongo_db: Any, backoffice: FakeBackoffice, sender: RecordingSender
) -> None:
    txid = new_txid()
    account_id = _seed(mongo_db, balance=5000, prior=[(txid, 1000)])  # deuda 3.000: 1.000 ya cobrados
    rows = [source_row(account_id, txid, remaining=2000, collected=1000, settlement=date(2026, 9, 1))]

    def applied_then_read_timeout(payload: dict[str, Any]) -> Exception:
        backoffice.apply_withdraw(payload)  # el servidor sí aplicó el retiro...
        return requests.exceptions.ReadTimeout("read timed out")  # ...pero no alcanzamos a leer la respuesta

    backoffice.withdraw_failure = applied_then_read_timeout
    store = InMemoryResultsStore(cfg.env)

    report = main(cfg, "2026-10-26", build_dependencies(cfg, rows, mongo_db, sender, store, []))

    assert backoffice.actions() == ["withdraw"]  # una sola llamada de retiro: nunca se reintenta a ciegas
    [result] = report.results
    assert result.outcome.value == "CHARGED"
    assert result.charged_amount == Decimal("2000")
    assert result.collected_seen == Decimal("1000")  # antes = 1.000; confirmado al ver 1.000 + 2.000
    assert result.attempts == 1
    assert not result.uncertain
    assert "confirmado por conciliación" in result.detail
    assert "AmbiguousError" in result.detail
    assert report.status == "SUCCESS"
    assert store.results[(report.run_id, 1)]["outcome"] == "CHARGED"


def test_ambiguous_withdraw_not_applied_stops_the_account(
    cfg: Config, mongo_db: Any, backoffice: FakeBackoffice, sender: RecordingSender
) -> None:
    """Si la suma no aumenta, el cobro queda FAILED incierto y no se cobra más de esa cuenta."""
    tx1, tx2 = new_txid(), new_txid()
    account_id = _seed(mongo_db, balance=5000, prior=[])
    rows = [
        source_row(account_id, tx1, remaining=1000, collected=0, settlement=date(2026, 9, 1)),
        source_row(account_id, tx2, remaining=1000, collected=0, settlement=date(2026, 9, 2)),
    ]
    backoffice.withdraw_failure = lambda _payload: requests.exceptions.ReadTimeout("read timed out")
    store = InMemoryResultsStore(cfg.env)

    report = main(cfg, "2026-10-26", build_dependencies(cfg, rows, mongo_db, sender, store, []))

    assert backoffice.actions() == ["withdraw"]
    first, second = report.results
    assert (first.outcome.value, first.uncertain) == ("FAILED", True)
    assert second.outcome.value == "FAILED"
    assert "estado incierto" in second.detail
    assert report.status == "FAILED"
    assert report.should_fail_job
    assert "Cobro incierto" in sender.emails[0][2]  # caso crítico en el correo
