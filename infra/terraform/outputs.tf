output "logs_volume_path" {
  description = "Ruta del volume de logs (cfg.logs_dir debe ser una carpeta bajo esta ruta)."
  value       = "/Volumes/${databricks_volume.logs.catalog_name}/${databricks_volume.logs.schema_name}/${databricks_volume.logs.name}"
}

output "secret_scopes" {
  description = "Scopes administrados (sin valores)."
  value       = sort([for s in databricks_secret_scope.this : s.name])
}
