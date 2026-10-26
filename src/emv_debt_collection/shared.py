"""Utilidades transversales sin dependencias del resto del paquete (dominio, aplicación y adaptadores las usan)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone
from decimal import Decimal
from typing import TypeVar
from zoneinfo import ZoneInfo

CHILE_TZ = ZoneInfo("America/Santiago")

T = TypeVar("T")


def describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def now() -> datetime:
    return datetime.now(timezone.utc)


def chunks(items: list[T], size: int) -> Iterator[list[T]]:
    """Parte una lista en bloques de `size`: [1..1200] con size=500 -> [1..500], [501..1000], [1001..1200]."""
    for start in range(0, len(items), size):
        yield items[start : start + size]


def clp(amount: Decimal | None) -> str:
    """Formato chileno para mostrar montos: 1234567 -> '$1.234.567'. Solo en correos y Teams."""
    return "-" if amount is None else "$" + f"{int(amount):,}".replace(",", ".")
