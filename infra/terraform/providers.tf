# Autenticación por variables de entorno (DATABRICKS_HOST + DATABRICKS_CLIENT_ID/SECRET, o azure-cli).
# Si los secret scopes son respaldados por Azure Key Vault, crearlos exige un token de Microsoft Entra ID
# (azure-cli o un service principal de Azure), no un token PAT ni OAuth M2M de Databricks.
provider "databricks" {
  host = var.databricks_host
}
