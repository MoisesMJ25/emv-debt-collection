"""Reintentos con backoff exponencial y jitter, y espera de conciliación."""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

from emv_debt_collection.domain.errors import TransientError

T = TypeVar("T")


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int
    base_delay: float
    max_delay: float
    jitter: bool = True

    def delay(self, attempt: int) -> float:
        """Backoff exponencial (1s, 2s, 4s... hasta max_delay) con jitter para no sincronizar hilos."""
        ceiling = min(self.max_delay, self.base_delay * 2 ** (attempt - 1))
        return random.uniform(0, ceiling) if self.jitter else ceiling  # noqa: S311 (no es criptográfico)


RETRY_POLICY = RetryPolicy(max_attempts=3, base_delay=1.0, max_delay=8.0)  # API y Mongo
VERIFY_POLICY = RetryPolicy(max_attempts=5, base_delay=2.0, max_delay=2.0, jitter=False)  # conciliación
REBLOCK_POLICY = RetryPolicy(max_attempts=5, base_delay=2.0, max_delay=16.0)  # re-bloqueo: insistir más
BULK_POLICY = RetryPolicy(max_attempts=5, base_delay=2.0, max_delay=30.0)  # lecturas en lote
# responder 429 y esperar


def call_with_retry(
    fn: Callable[[], T],
    policy: RetryPolicy | None = None,
    retry_on: tuple[type[BaseException], ...] = (TransientError,),
) -> tuple[T, int]:
    """Ejecuta fn y devuelve (resultado, intentos). Solo reintenta los errores de retry_on;
    cualquier otro error sale de inmediato. Al agotar intentos, relanza el último error."""
    policy = policy or RETRY_POLICY
    attempt = 0
    while True:
        attempt += 1
        try:
            return fn(), attempt
        except retry_on:
            if attempt >= policy.max_attempts:
                raise
            time.sleep(policy.delay(attempt))


def wait_until(predicate: Callable[[], bool], policy: RetryPolicy) -> bool:
    """Pregunta varias veces si algo ya ocurrió. Se usa para conciliar un retiro ambiguo:
    la transacción puede tardar unos segundos en aparecer en Mongo."""
    for attempt in range(1, policy.max_attempts + 1):
        try:
            if predicate():
                return True
        except TransientError:
            pass
        if attempt < policy.max_attempts:
            time.sleep(policy.delay(attempt))
    return False
