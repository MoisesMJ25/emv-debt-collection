"""Configuración del proceso.

- `DEFAULTS`: parámetros de negocio comunes a los ambientes (los mismos valores del notebook original).
- `EnvSettings`: lo que cambia entre ambientes o no debe vivir en el repo (URLs, nombres de secretos, correos,
  chats, rutas, tablas). Llega como parámetros del job, definidos como variables del bundle.
- `MONGO`, `AUDIT_REASONS`, `WITHDRAW`: contrato con Mongo y con la API del backoffice.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields
from decimal import Decimal
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from emv_debt_collection.ports import SecretsProvider

ENVIRONMENTS = ("dev", "prod")

# Parámetros de negocio: todo lo que el negocio necesite ajustar y que no es sensible.
DEFAULTS: dict[str, Any] = {
    "settlement_days": 14,  # solo deudas liquidadas hace 14 días o más
    "max_charge_per_account": Decimal("10000"),  # tope de cobro por cuenta en una ejecución
    "max_charge_per_run": Decimal("1000000"),  # tope total de la ejecución (circuit breaker)
    "min_charge_amount": Decimal("0"),  # cobro mínimo por deuda; 0 = sin mínimo (se cobra desde $1)
    "origin": "ops-databricks",
    "max_workers": 4,  # cuentas procesadas en paralelo
    "http_timeout": 10,
    "mongo_batch_size": 500,  # ids por consulta $in (con string + ObjectId son hasta 1.000 valores)
}

# Tablas Delta del proceso: "<catalogo>.<schema>.<env>_emv_debt_collection_<runs|results>".
TABLES = "{schema}.{env}_emv_debt_collection_{name}"

# Esquema de Mongo que usa el proceso. Si un nombre de campo cambia, se cambia aquí y en ningún otro lugar.
MONGO: dict[str, Any] = {
    "accounts": "accounts",
    "balance": "balance.amount",
    "currency": "balance.currency",
    "expected_currency": "CLP",
    "status": "status",
    "blocked_values": ("BLOCKED",),
    "chargeable_statuses": ("ENABLED", "BLOCKED"),  # DISABLED y DELETED quedan fuera
    "known_statuses": ("ENABLED", "BLOCKED", "DISABLED", "DELETED"),
    "transactions": "transactions",
    "tx_account": "accountId",  # indexado: las búsquedas de cobros van siempre por cuenta
    "tx_reason": "metadata.reason",  # "<TXID>/CxC" (actual) o "<TXID>/GO-xxx/CxC" (anterior)
    "tx_amount": "amount",
}

AUDIT_REASONS = {
    "unblock": "desbloqueo para cobro EMV",
    "reblock": "rebloqueo post cobro EMV",
}

WITHDRAW: dict[str, Any] = {
    "type": "WITHDRAWALS",
    "reference": "POMELO",
    "metadata": {"paymentId": "", "provider": "MetroPago", "product": "Cash_out"},
}

# Parámetros del job (widgets) que construyen EnvSettings. Los dos últimos son opcionales y solo valen en dev.
ENV_PARAMETERS = (
    "api_base_url",
    "api_key_scope",
    "api_key_name",
    "mongo_connection",
    "mongo_secret",
    "source_table",
    "tables_schema",
    "operator_user",
    "logs_dir",
    "email_recipients",
    "teams_chat_id",
    "dev_json_accounts",
    "dev_json_transactions",
)
OPTIONAL_ENV_PARAMETERS = frozenset({"dev_json_accounts", "dev_json_transactions"})


@dataclass(frozen=True)
class EnvSettings:
    """Valores propios de cada ambiente. Equivale a una entrada de `ENVIRONMENTS` del notebook original."""

    api_base_url: str
    api_key_secret: tuple[str, str]  # (scope, key) en dbutils.secrets
    mongo: tuple[str, str]  # (conexión, secreto) que recibe conectar_mongo
    source_table: str
    tables_schema: str  # "<catalogo>.<schema>" donde viven las tablas runs/results
    operator_user: str
    logs_dir: str
    email_recipients: tuple[str, ...]
    teams_chat_id: str
    dev_json_accounts: str | None = None
    dev_json_transactions: str | None = None

    @classmethod
    def from_parameters(cls, params: Mapping[str, str | None]) -> EnvSettings:
        """Construye la configuración del ambiente desde los parámetros del job. Falla si falta alguno obligatorio.

        `email_recipients` se recibe como UN string y se envuelve en una tupla de un elemento: igual que el
        notebook original, se hace una sola llamada a send_email con ese string (ver README, Observaciones)."""
        values = {name: (params.get(name) or "").strip() for name in ENV_PARAMETERS}
        missing = [name for name in ENV_PARAMETERS if name not in OPTIONAL_ENV_PARAMETERS and not values[name]]
        if missing:
            raise ValueError(f"Faltan parámetros de ambiente: {', '.join(missing)}")
        return cls(
            api_base_url=values["api_base_url"],
            api_key_secret=(values["api_key_scope"], values["api_key_name"]),
            mongo=(values["mongo_connection"], values["mongo_secret"]),
            source_table=values["source_table"],
            tables_schema=values["tables_schema"],
            operator_user=values["operator_user"],
            logs_dir=values["logs_dir"],
            email_recipients=(values["email_recipients"],),
            teams_chat_id=values["teams_chat_id"],
            dev_json_accounts=values["dev_json_accounts"] or None,
            dev_json_transactions=values["dev_json_transactions"] or None,
        )


@dataclass(frozen=True)
class Config:
    """Configuración inmutable: si alguien intenta cambiarla durante la ejecución, Python lanza error."""

    env: str
    dry_run: bool
    api_base_url: str
    api_key: str
    mongo_connection: str
    mongo_secret: str
    source_table: str
    settlement_days: int
    max_charge_per_account: Decimal
    max_charge_per_run: Decimal
    min_charge_amount: Decimal
    operator_user: str
    origin: str
    max_workers: int
    http_timeout: int
    mongo_batch_size: int
    logs_dir: str
    runs_table: str
    results_table: str
    email_recipients: tuple[str, ...]
    teams_chat_id: str
    dev_json_accounts: str | None = None
    dev_json_transactions: str | None = None


def load_config(env: str, dry_run: bool, settings: EnvSettings, secrets: SecretsProvider) -> Config:
    """Combina DEFAULTS con el ambiente y lee la API key desde dbutils.secrets (por scope y key)."""
    if env not in ENVIRONMENTS:
        raise ValueError(f"Ambiente desconocido {env!r}: se esperaba uno de {ENVIRONMENTS}")
    raw: dict[str, Any] = {**DEFAULTS, **{f.name: getattr(settings, f.name) for f in fields(settings)}}
    scope, key = raw.pop("api_key_secret")
    mongo_connection, mongo_secret = raw.pop("mongo")
    tables_schema = raw.pop("tables_schema")
    if env != "dev" and raw.get("dev_json_accounts"):
        raise ValueError("Los JSON de prueba solo están permitidos en dev")  # blindaje: prod siempre lee Mongo
    tables: dict[str, Any] = {
        f"{name}_table": TABLES.format(schema=tables_schema, env=env, name=name) for name in ("runs", "results")
    }
    return Config(
        env=env,
        dry_run=dry_run,
        api_key=secrets.get(scope=scope, key=key),
        mongo_connection=mongo_connection,
        mongo_secret=mongo_secret,
        **tables,
        **raw,
    )
