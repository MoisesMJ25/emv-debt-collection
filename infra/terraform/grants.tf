# Grants mínimos para el principal del job.
#
# Se usa `databricks_grant` (singular): administra SOLO los privilegios de este principal sobre cada objeto.
# `databricks_grants` (plural) es autoritativo y borraría los grants de otros equipos en objetos compartidos.

locals {
  source_parts  = split(".", var.source_table)
  results_parts = split(".", var.results_schema)

  source_schema = join(".", slice(local.source_parts, 0, 2))
  volume_schema = "${var.logs_volume.catalog}.${var.logs_volume.schema}"

  catalogs = toset([local.source_parts[0], local.results_parts[0], var.logs_volume.catalog])
  # USE_SCHEMA en cada schema involucrado (el de resultados recibe además CREATE_TABLE abajo).
  use_schemas = setsubtract(toset([local.source_schema, local.volume_schema]), toset([var.results_schema]))
}

resource "databricks_grant" "catalog_use" {
  for_each = local.catalogs

  catalog    = each.value
  principal  = var.job_principal
  privileges = ["USE_CATALOG"]
}

resource "databricks_grant" "schema_use" {
  for_each = local.use_schemas

  schema     = each.value
  principal  = var.job_principal
  privileges = ["USE_SCHEMA"]
}

# ensure_tables crea las tablas runs/results y les agrega columnas: el creador queda como owner,
# así que no hace falta MODIFY a nivel de schema.
resource "databricks_grant" "results_schema" {
  schema     = var.results_schema
  principal  = var.job_principal
  privileges = ["USE_SCHEMA", "CREATE_TABLE"]
}

# Lectura de la tabla de deudas (y requisito del trigger table_update del job).
resource "databricks_grant" "source_table" {
  table      = var.source_table
  principal  = var.job_principal
  privileges = ["SELECT"]
}

resource "databricks_grant" "logs_volume" {
  volume     = databricks_volume.logs.id
  principal  = var.job_principal
  privileges = ["READ_VOLUME", "WRITE_VOLUME"]
}
