"""Logging en hora de Chile, a stdout y a un archivo local que luego se copia al Volume."""

from __future__ import annotations

import logging
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from emv_debt_collection.shared import CHILE_TZ

LOGGER_NAME = "emv_debt_collection"  # los módulos usan getLogger(__name__): son hijos de este logger


class ChileanFormatter(logging.Formatter):
    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:  # noqa: N802
        dt = datetime.fromtimestamp(record.created, tz=CHILE_TZ)
        return dt.strftime(datefmt) if datefmt else dt.strftime("%Y-%m-%d %H:%M:%S") + f",{int(record.msecs):03d}"


def setup_logging() -> tuple[logging.Logger, Path]:
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for handler in list(logger.handlers):  # re-ejecutar la celda no duplica handlers
        handler.close()
        logger.removeHandler(handler)
    fmt = ChileanFormatter("%(asctime)s | %(levelname)-7s | %(threadName)s | %(message)s")
    local_file = Path(tempfile.gettempdir()) / f"emv_debt_collection_{datetime.now(CHILE_TZ):%Y%m%d_%H%M%S}.log"
    handlers: tuple[logging.Handler, ...] = (
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(local_file, encoding="utf-8"),
    )
    for handler in handlers:
        handler.setFormatter(fmt)
        logger.addHandler(handler)
    return logger, local_file
