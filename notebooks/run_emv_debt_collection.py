# Databricks notebook source
# MAGIC %md
# MAGIC # Cobro de deuda EMV — entrada del job
# MAGIC
# MAGIC Notebook **delgado**: lee parámetros, carga las dependencias corporativas y llama al paquete
# MAGIC `emv_debt_collection` (instalado como wheel por el bundle). Toda la lógica vive en el paquete.
# MAGIC
# MAGIC Una ejecución manual usa `dry_run=true` por defecto: no cobra.

# COMMAND ----------

# _____Parámetros_____:
from emv_debt_collection.config import ENV_PARAMETERS

dbutils.widgets.dropdown("env", "dev", ["dev", "prod"])
dbutils.widgets.dropdown("dry_run", "true", ["true", "false"])  # default seguro: una ejecución manual no cobra
dbutils.widgets.text("run_date", "")  # vacío = hoy en hora de Chile
dbutils.widgets.text("utils_path", "")  # carpeta del módulo `utils` si no está en el sys.path
for _name in ENV_PARAMETERS:  # valores por ambiente: llegan como parámetros del job (variables del bundle)
    dbutils.widgets.text(_name, "")

ENV = dbutils.widgets.get("env")
DRY_RUN = dbutils.widgets.get("dry_run") == "true"
RUN_DATE_RAW = dbutils.widgets.get("run_date")
PARAMETERS = {name: dbutils.widgets.get(name) for name in ENV_PARAMETERS}
print(f"env={ENV} | dry_run={DRY_RUN} | run_date={RUN_DATE_RAW or '(hoy)'}")

# COMMAND ----------

# MAGIC %run "/Workspace/Procesos_Automaticos/Funciones/Funciones_Conexion_MongoDB_MI"

# COMMAND ----------

# _____Dependencias corporativas_____:
# conectar_mongo / desconectar_mongo vienen del %run anterior; send_email / send_message del módulo utils.
import sys

_utils_path = dbutils.widgets.get("utils_path").strip()
if _utils_path and _utils_path not in sys.path:
    sys.path.append(_utils_path)

from utils import send_email, send_message

# COMMAND ----------

# _____Ejecución_____:
import pandas as pd

from emv_debt_collection.composition import failure_message, report_rows, run_job

report = run_job(
    env=ENV,
    dry_run=DRY_RUN,
    run_date_raw=RUN_DATE_RAW,
    parameters=PARAMETERS,
    spark=spark,
    dbutils=dbutils,
    conectar_mongo=conectar_mongo,
    desconectar_mongo=desconectar_mongo,
    send_email=send_email,
    send_message=send_message,
)

if report.results:
    display(pd.DataFrame(report_rows(report)))

# El job queda en rojo si el run falló, si alguna cuenta quedó desbloqueada o si Teams falló.
if report.should_fail_job:
    raise RuntimeError(failure_message(report))
