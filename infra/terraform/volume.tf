# Volume donde se copian los logs de cada ejecución (cfg.logs_dir apunta a una carpeta dentro de él).
# Si el volume ya existe y es compartido, se importa al estado antes del primer apply (ver README).
resource "databricks_volume" "logs" {
  catalog_name     = var.logs_volume.catalog
  schema_name      = var.logs_volume.schema
  name             = var.logs_volume.name
  volume_type      = var.logs_volume.storage_location == null ? "MANAGED" : "EXTERNAL"
  storage_location = var.logs_volume.storage_location
  comment          = var.logs_volume.comment

  lifecycle {
    prevent_destroy = true # compartido con otros procesos: nunca se elimina desde este repo
  }
}
