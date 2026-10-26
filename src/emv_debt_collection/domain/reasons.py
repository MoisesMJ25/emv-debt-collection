"""El `metadata.reason` de los retiros: cómo se arma y cómo se extrae el TXID de la deuda."""

from __future__ import annotations

import re
from typing import Any

REASON_SUFFIX = "/CxC"
REASON_MONGO_REGEX = re.escape(REASON_SUFFIX) + "$"  # reducir consulta; el TXID se extrae en Python


def build_reason(transaction_id: str) -> str:
    """Lo que se envía en el retiro: '<TXID>/CxC'. Es el mismo para todos los cobros parciales de una deuda."""
    return f"{transaction_id}{REASON_SUFFIX}"


def debt_id_from_reason(reason: Any) -> str | None:
    """Extrae el TXID de un metadata.reason. Formatos válidos (y solo estos):
        '<TXID>/CxC'            -> actual (este proceso)
        '<TXID>/GO-445/CxC'     -> histórico (cobros manuales antes del notebook)
    El TXID es siempre el primer segmento. Cualquier otro formato devuelve None y NO se suma: si el pipeline
    sí lo sumara, la diferencia aparece como inconsistencia y la deuda se excluye (nunca se cobra de más).
    Se hace en Python y no con un $regex en Mongo para comparar IGUALDAD exacta del TXID: un regex sin
    anclar haría calzar 'a1' con 'a10'."""
    if not isinstance(reason, str) or not reason.endswith(REASON_SUFFIX):
        return None
    parts = reason.split("/")
    if len(parts) not in (2, 3) or not parts[0].strip():
        return None
    return parts[0].strip()
