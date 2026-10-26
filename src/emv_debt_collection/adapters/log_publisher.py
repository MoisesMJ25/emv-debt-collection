"""Publicación del log local en el Volume de Unity Catalog."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

log = logging.getLogger(__name__)


class VolumeLogPublisher:
    """Copia el log local al Volume. Si falla, se informa y se sigue: el log no es crítico."""

    def __init__(self, logger: logging.Logger, local_log: Path, logs_dir: str):
        self._logger = logger
        self._local_log = local_log
        self._logs_dir = logs_dir

    def publish(self) -> str | None:
        try:
            for handler in self._logger.handlers:
                handler.flush()
            logs_dir = Path(self._logs_dir)
            logs_dir.mkdir(parents=True, exist_ok=True)
            destination = logs_dir / self._local_log.name
            shutil.copy(self._local_log, destination)
            return str(destination)
        except Exception as exc:
            log.error("No se pudo copiar el log al Volume: %s", exc)
            return None
