locals {
  infra_gpu_nodes_count_per_group  = try(local.infra_outputs.gpu_nodes_count_per_group, 0)
  infra_gpu_nodes_driverfull_image = try(local.infra_outputs.gpu_nodes_driverfull_image, false)
  infra_gpu_nodes_platform         = try(local.infra_outputs.gpu_nodes_platform, "")
  infra_storage_bucket_name        = try(local.infra_storage_bucket.name, "")
  infra_filestore_enabled          = try(local.infra_outputs.filestore, null) != null

  deploy_gpu_infrastructure_effective = (
    var.deploy_gpu_infrastructure != null
    ? var.deploy_gpu_infrastructure
    : local.infra_gpu_nodes_count_per_group > 0
  )

  configure_gpu_platform_effective = (
    var.configure_gpu_platform != null
    ? var.configure_gpu_platform
    : local.deploy_gpu_infrastructure_effective
  )

  configure_backend_scheduler_effective = (
    var.configure_backend_scheduler
    && local.deploy_gpu_infrastructure_effective
    && var.deploy_backend_operator
  )

  gpu_platform_name_value = (
    var.gpu_platform_name != null && var.gpu_platform_name != ""
    ? var.gpu_platform_name
    : (
      local.infra_gpu_nodes_platform != ""
      ? upper(regex("^gpu-([a-z0-9]+)", local.infra_gpu_nodes_platform)[0])
      : ""
    )
  )
}

resource "terraform_data" "validate" {
  lifecycle {
    precondition {
      condition     = !var.enable_auth || local.oidc_client_secret != ""
      error_message = "oidc_client_secret could not be resolved."
    }
    precondition {
      condition     = !var.enable_auth || local.auth_domain != ""
      error_message = "ingress_hostname or keycloak_hostname must resolve to a non-empty auth domain."
    }
    precondition {
      condition     = local.postgres_host != "" && local.postgres_user != "" && local.postgres_password != ""
      error_message = "PostgreSQL values could not be resolved. Run terraform -chdir=./infra apply first or set postgres_* overrides."
    }
    precondition {
      condition     = local.storage_access_key_id != "" && local.storage_secret_access_key != "" && local.storage_endpoint != "" && local.storage_region != ""
      error_message = "Storage values could not be resolved. Run terraform -chdir=./infra apply first or set storage_* overrides."
    }
    precondition {
      condition     = !var.nebius_sso_enabled || ((var.nebius_sso_client_id != null ? var.nebius_sso_client_id : "") != "" && (var.nebius_sso_client_secret != null ? var.nebius_sso_client_secret : "") != "")
      error_message = "Nebius SSO is enabled but nebius_sso_client_id or nebius_sso_client_secret is missing."
    }
    precondition {
      condition = (
        !var.tls_enabled
        || var.tls_mode != "cert-manager"
        || !var.deploy_cert_manager
        || (var.cert_manager_email != null ? var.cert_manager_email : "") != ""
      )
      error_message = "cert_manager_email must be set when tls_mode=cert-manager and deploy_cert_manager=true."
    }
    precondition {
      condition = (
        !var.configure_workflow_storage
        || local.infra_storage_bucket_name != ""
      )
      error_message = "Storage bucket details are required for configure_workflow_storage=true. Run terraform -chdir=./infra apply first."
    }
    precondition {
      condition = (
        !var.configure_dataset_bucket
        || local.infra_storage_bucket_name != ""
      )
      error_message = "Storage bucket details are required for configure_dataset_bucket=true. Run terraform -chdir=./infra apply first."
    }
    precondition {
      condition = (
        !local.configure_gpu_platform_effective
        || local.gpu_platform_name_value != ""
      )
      error_message = "gpu_platform_name could not be resolved. Set gpu_platform_name explicitly or apply infra with GPU nodes."
    }
  }
}
