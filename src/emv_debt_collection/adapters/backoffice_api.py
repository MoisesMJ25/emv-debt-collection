"""API del backoffice. Clasifica cada error según si es seguro reintentar: las operaciones idempotentes
(bloquear, desbloquear) se distinguen del RETIRO, que nunca se reintenta a ciegas."""

from __future__ import annotations

import threading
from decimal import Decimal
from typing import Any

import requests

from emv_debt_collection.config import WITHDRAW, Config
from emv_debt_collection.domain.errors import AmbiguousError, BusinessError, TransientError


class BackofficeApi:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._local = threading.local()  # Session por hilo: reutiliza conexiones sin compartir estado

    def _session(self) -> requests.Session:
        if not hasattr(self._local, "session"):
            session = requests.Session()
            session.headers.update({"Content-Type": "application/json", "x-api-key": self.cfg.api_key})
            self._local.session = session
        session_: requests.Session = self._local.session
        return session_

    def _audit(self, reason: str) -> dict[str, Any]:
        return {"user": self.cfg.operator_user, "origin": self.cfg.origin, "reason": reason}

    def _call(self, method: str, path: str, payload: dict[str, Any], idempotent: bool) -> None:
        try:
            resp = self._session().request(
                method,
                f"{self.cfg.api_base_url.rstrip('/')}/{path}",
                json=payload,
                timeout=self.cfg.http_timeout,
            )
        except requests.exceptions.ConnectTimeout as exc:  # nunca llegó al servidor
            raise TransientError(f"Timeout de conexión en {method} /{path}") from exc
        except requests.exceptions.RequestException as exc:  # pudo haber llegado
            raise (TransientError if idempotent else AmbiguousError)(
                f"Error de red en {method} /{path}: {type(exc).__name__}"
            ) from exc
        code = resp.status_code
        if 200 <= code < 300:
            return
        msg = f"HTTP {code} en {method} /{path}: {_api_error_text(resp)}"
        if code == 429:
            raise TransientError(msg)
        if code >= 500 or code == 408:
            raise (TransientError if idempotent else AmbiguousError)(msg)
        raise BusinessError(msg)

    def block_account(self, account_id: str, reason: str) -> None:
        self._call("POST", f"accounts/block/{account_id}", self._audit(reason), idempotent=True)

    def unblock_account(self, account_id: str, reason: str) -> None:
        self._call("POST", f"accounts/unblock/{account_id}", self._audit(reason), idempotent=True)

    def withdraw(self, account_id: str, amount: Decimal, reason: str) -> None:
        """Retiro con reason = '<TXID>/CxC': asocia el cobro a la deuda y es la clave de idempotencia."""
        if amount != amount.to_integral_value():
            raise BusinessError(f"Monto con decimales ({amount})")
        payload = {
            "accountId": account_id,
            "type": WITHDRAW["type"],
            "amount": int(amount),
            "reference": WITHDRAW["reference"],
            "metadata": dict(WITHDRAW["metadata"]),
            "user": self.cfg.operator_user,
            "origin": self.cfg.origin,
            "reason": reason,
        }
        self._call("POST", "accounts/fund/manual", payload, idempotent=False)


def _api_error_text(resp: requests.Response) -> str:
    """El backoffice responde {"error": {"message", "internalCode", "metadata"}}: se extrae lo legible."""
    try:
        err = resp.json().get("error") or {}
        parts = [str(err.get("message") or "").strip()]
        if err.get("internalCode") is not None:
            parts.append(f"código {err['internalCode']}")
        if err.get("metadata"):
            parts.append(f"detalle {err['metadata']}")
        text = " | ".join(p for p in parts if p)
        if text:
            return text
    except Exception:  # noqa: S110 (si el cuerpo no es el JSON esperado, se usa el texto crudo)
        pass
    return (resp.text or "")[:300]
