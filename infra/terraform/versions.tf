terraform {
  required_version = "= 1.16.4"

  required_providers {
    databricks = {
      source  = "databricks/databricks"
      version = "= 1.134.0"
    }
  }

  # Estado en una storage account de Azure. La configuración concreta (resource group, cuenta, contenedor, key)
  # se entrega en `terraform init -backend-config=backend.hcl` (ver backend.hcl.example): no vive en el repo.
  backend "azurerm" {}
}
