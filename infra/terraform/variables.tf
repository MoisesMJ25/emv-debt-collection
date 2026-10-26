variable "databricks_host" {
  description = "URL del workspace de Databricks (https://adb-<id>.<n>.azuredatabricks.net)."
  type        = string
}

variable "job_principal" {
  description = "Principal con el que corren los jobs (application ID del service principal). Recibe los grants."
  type        = string
}

# --- Volume de logs ---
variable "logs_volume" {
  description = "Volume de Unity Catalog donde el proceso copia sus logs. Si ya existe, se importa (ver README)."
  type = object({
    catalog          = string
    schema           = string
    name             = string
    storage_location = optional(string) # null = MANAGED; con valor = EXTERNAL
    comment          = optional(string, "Logs de procesos de operaciones")
  })
}

# --- Tablas ---
variable "source_table" {
  description = "Tabla de deudas que lee el proceso (catalogo.schema.tabla). Recibe SELECT."
  type        = string
}

variable "results_schema" {
  description = "catalogo.schema donde el proceso crea <env>_emv_debt_collection_runs/_results (ensure_tables)."
  type        = string
}

# --- Secret scopes (solo su existencia; los valores se cargan fuera de Terraform) ---
variable "secret_scopes" {
  description = <<-EOT
    Secret scopes que usa el proceso: API key del backoffice y conexión a Mongo, de dev y prod.
    `keyvault` solo si el scope es respaldado por Azure Key Vault (resource_id y dns_name del vault).
  EOT
  type = map(object({
    keyvault = optional(object({
      resource_id = string
      dns_name    = string
    }))
  }))
}
