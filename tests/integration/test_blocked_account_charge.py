"""Cuenta BLOCKED con dos deudas: un desbloqueo, un cobro total y uno parcial, re-bloqueo en finally."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

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


def test_blocked_account_full_and_partial_charge(
    cfg: Config, mongo_db: Any, backoffice: FakeBackoffice, sender: RecordingSender
) -> None:
    account = ObjectId()
    account_id = str(account)
    tx_full, tx_partial = new_txid(), new_txid()
    mongo_db.accounts.insert_one(
        {"_id": account, "status": "BLOCKED", "balance": {"amount": Decimal128("2000"), "currency": "CLP"}}
    )
    # Cobro previo de la segunda deuda en formato histórico: cuadra con MONTO_COBRADO_PARCIAL = 500.
    mongo_db.transactions.insert_one(
        {"accountId": account_id, "amount": 500, "metadata": {"reason": f"{tx_partial}/GO-445/CxC"}}
    )
    rows = [
        source_row(account_id, tx_full, remaining=1000, collected=0, settlement=date(2026, 9, 1)),
        source_row(account_id, tx_partial, remaining=1500, collected=500, settlement=date(2026, 9, 5)),
    ]
    store = InMemoryResultsStore(cfg.env)
    disconnected: list[Any] = []

    report = main(cfg, "2026-10-26", build_dependencies(cfg, rows, mongo_db, sender, store, disconnected))

    # Un solo desbloqueo, dos retiros con reason "<TXID>/CxC" y el re-bloqueo al final (finally).
    assert backoffice.actions() == ["unblock", "withdraw", "withdraw", "block"]
    withdraws = [c.payload for c in backoffice.calls if c.action == "withdraw"]
    assert [(w["reason"], w["amount"]) for w in withdraws] == [(f"{tx_full}/CxC", 1000), (f"{tx_partial}/CxC", 1000)]
    assert all(w["accountId"] == account_id and w["type"] == "WITHDRAWALS" for w in withdraws)
    assert mongo_db.accounts.find_one({"_id": account})["status"] == "BLOCKED"

    # Reporte
    assert report.status == "SUCCESS"
    assert not report.should_fail_job
    assert report.charged_amount == Decimal("2000")
    assert disconnected  # se cerró la conexión a Mongo

    # Store de resultados (mismas claves que el MERGE de Delta)
    assert set(store.runs) == {report.run_id}
    run = store.runs[report.run_id]
    assert (run["status"], run["charged_count"], run["partial_count"]) == ("SUCCESS", 2, 1)
    saved = [store.results[(report.run_id, seq)] for seq in (1, 2)]
    assert [r["outcome"] for r in saved] == ["CHARGED", "PARTIALLY_CHARGED"]
    assert [r["reason"] for r in saved] == [f"{tx_full}/CxC", f"{tx_partial}/CxC"]
    assert [r["charged_amount"] for r in saved] == [Decimal("1000.00"), Decimal("1000.00")]
    assert [r["remaining_after"] for r in saved] == [Decimal("0.00"), Decimal("500.00")]
    assert [r["collected_before"] for r in saved] == [Decimal("0.00"), Decimal("500.00")]
    assert all(r["was_unblocked"] and not r["reblock_failed"] for r in saved)
    assert all(r["detail"].endswith("| Cuenta re-bloqueada") for r in saved)

    # Notificaciones
    assert len(sender.emails) == 1
    assert len(sender.messages) == 1
    assert sender.messages[0][1] == cfg.teams_chat_id
