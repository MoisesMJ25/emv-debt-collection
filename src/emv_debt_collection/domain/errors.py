"""Taxonomía de errores. La pregunta clave ante un error sigue siendo "¿es seguro reintentar?"."""


class TransientError(Exception):
    """Reintentar es seguro (la operación no se aplicó, o es idempotente)."""


class AmbiguousError(Exception):
    """No se sabe si se aplicó (timeout o 5xx en un RETIRO). Nunca reintentar: conciliar."""


class BusinessError(Exception):
    """Dato o regla de negocio (HTTP 4xx, status desconocido...). Reintentar no sirve."""


class SourceError(Exception):
    """No se pudo leer la tabla de deudas."""


class RunLimitExceeded(Exception):  # noqa: N818 (nombre del notebook original)
    """El plan supera el tope por ejecución: no se cobra nada."""
