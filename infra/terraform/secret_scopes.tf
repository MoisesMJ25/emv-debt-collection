# Solo la EXISTENCIA de los scopes y el permiso de lectura del principal del job.
# Los valores de los secretos NUNCA pasan por Terraform: se cargan con `databricks secrets put-secret`.
resource "databricks_secret_scope" "this" {
  for_each = var.secret_scopes

  name = each.key

  dynamic "keyvault_metadata" {
    for_each = each.value.keyvault == null ? [] : [each.value.keyvault]
    content {
      resource_id = keyvault_metadata.value.resource_id
      dns_name    = keyvault_metadata.value.dns_name
    }
  }

  lifecycle {
    prevent_destroy = true # scopes compartidos con otros procesos
  }
}

resource "databricks_secret_acl" "job_read" {
  for_each = databricks_secret_scope.this

  scope      = each.value.name
  principal  = var.job_principal
  permission = "READ"
}
