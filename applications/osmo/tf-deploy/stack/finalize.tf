resource "terraform_data" "post_install" {
  input = {
    namespace                 = var.namespace
    ingress_namespace         = var.ingress_namespace
    ingress_hostname          = var.ingress_hostname
    auth_domain               = local.auth_domain
    tls_enabled               = tostring(var.tls_enabled)
    tls_mode                  = var.tls_mode
    tls_secret_name           = var.tls_secret_name
    workflows_namespace       = var.workflows_namespace
    workflow_service_url_mode = "public-ingress"
    enable_auth               = tostring(var.enable_auth)
    deploy_ui                 = tostring(var.deploy_ui)
    service_base_url          = local.service_base_url_value
    postgres_host             = local.postgres_host
    postgres_port             = tostring(local.postgres_port)
    postgres_db               = local.postgres_db
    postgres_user             = local.postgres_user
    ui_release_state          = try(helm_release.osmo_ui[0].status, "")
  }

  triggers_replace = {
    namespace                 = var.namespace
    ingress_namespace         = var.ingress_namespace
    ingress_hostname          = var.ingress_hostname
    auth_domain               = local.auth_domain
    tls_enabled               = tostring(var.tls_enabled)
    tls_mode                  = var.tls_mode
    tls_secret_name           = var.tls_secret_name
    workflows_namespace       = var.workflows_namespace
    workflow_service_url_mode = "public-ingress"
    enable_auth               = tostring(var.enable_auth)
    deploy_ui                 = tostring(var.deploy_ui)
    service_base_url          = local.service_base_url_value
    postgres_host             = local.postgres_host
    postgres_port             = tostring(local.postgres_port)
    postgres_db               = local.postgres_db
    postgres_user             = local.postgres_user
    postgres_password_hash    = sha256(local.postgres_password)
    ui_release_state          = try(helm_release.osmo_ui[0].status, "")
  }

  depends_on = [
    terraform_data.ingress_ready,
    helm_release.osmo_service,
    helm_release.osmo_router,
    terraform_data.keycloak_bootstrap,
    kubernetes_secret_v1.vault_secrets,
  ]

  provisioner "local-exec" {
    command = "/bin/bash ${path.module}/../scripts/post-install.sh"

    environment = {
      RUN_POST_INSTALL      = "true"
      RUN_APP_CONFIGURATION = "false"
      KUBECONFIG            = pathexpand(var.kubeconfig_path)
      KUBECTL_CONTEXT       = var.kubeconfig_context != null ? var.kubeconfig_context : ""
      OSMO_NAMESPACE        = var.namespace
      WORKFLOWS_NAMESPACE   = var.workflows_namespace
      INGRESS_NAMESPACE     = var.ingress_namespace
      INGRESS_HOSTNAME      = var.ingress_hostname
      AUTH_DOMAIN           = local.auth_domain
      TLS_ENABLED           = tostring(var.tls_enabled)
      TLS_MODE              = var.tls_mode
      TLS_SECRET_NAME       = var.tls_secret_name
      KEYCLOAK_TLS_SECRET_NAME = var.keycloak_tls_secret_name
      ENABLE_AUTH           = tostring(var.enable_auth)
      DEPLOY_UI             = tostring(var.deploy_ui)
      SERVICE_BASE_URL      = local.service_base_url_value
      POSTGRES_HOST         = local.postgres_host
      POSTGRES_PORT         = tostring(local.postgres_port)
      POSTGRES_DB           = local.postgres_db
      POSTGRES_USER         = local.postgres_user
      POSTGRES_PASSWORD     = local.postgres_password
    }
  }
}

resource "terraform_data" "app_config" {
  input = {
    namespace                     = var.namespace
    backend_name                  = var.backend_name
    workflow_storage              = tostring(var.configure_workflow_storage)
    dataset_bucket                = tostring(var.configure_dataset_bucket)
    backend_scheduler             = tostring(local.configure_backend_scheduler_effective)
    gpu_platform                  = tostring(local.configure_gpu_platform_effective)
    configure_filestore_mounts    = tostring(local.infra_filestore_enabled)
    deploy_backend_operator       = tostring(var.deploy_backend_operator)
    deploy_gpu_infrastructure     = tostring(local.deploy_gpu_infrastructure_effective)
    deploy_observability          = tostring(var.deploy_observability)
    storage_bucket_name           = local.infra_storage_bucket_name
    storage_region                = local.storage_region
    storage_endpoint              = local.storage_endpoint
    backend_operator_service_url  = local.backend_operator_service_url_value
    gpu_platform_name             = local.gpu_platform_name_value
    dataset_bucket_name           = var.dataset_bucket_name
    post_install_service_base_url = local.service_base_url_value
    backend_operator_namespace    = var.backend_operator_namespace
    workflows_namespace           = var.workflows_namespace
    kai_scheduler_namespace       = var.kai_scheduler_namespace
    tls_mode                      = var.tls_mode
    tls_secret_name               = var.tls_secret_name
    workflow_service_url_mode     = "public-ingress"
  }

  triggers_replace = {
    namespace                     = var.namespace
    backend_name                  = var.backend_name
    workflow_storage              = tostring(var.configure_workflow_storage)
    dataset_bucket                = tostring(var.configure_dataset_bucket)
    backend_scheduler             = tostring(local.configure_backend_scheduler_effective)
    gpu_platform                  = tostring(local.configure_gpu_platform_effective)
    configure_filestore_mounts    = tostring(local.infra_filestore_enabled)
    deploy_backend_operator       = tostring(var.deploy_backend_operator)
    deploy_gpu_infrastructure     = tostring(local.deploy_gpu_infrastructure_effective)
    deploy_observability          = tostring(var.deploy_observability)
    storage_bucket_name           = local.infra_storage_bucket_name
    storage_region                = local.storage_region
    storage_endpoint              = local.storage_endpoint
    storage_access_key_id         = local.storage_access_key_id
    storage_secret_hash           = sha256(local.storage_secret_access_key)
    backend_operator_service_url  = local.backend_operator_service_url_value
    gpu_platform_name             = local.gpu_platform_name_value
    dataset_bucket_name           = var.dataset_bucket_name
    post_install_service_base_url = local.service_base_url_value
    backend_operator_namespace    = var.backend_operator_namespace
    workflows_namespace           = var.workflows_namespace
    kai_scheduler_namespace       = var.kai_scheduler_namespace
    tls_mode                      = var.tls_mode
    tls_secret_name               = var.tls_secret_name
    workflow_service_url_mode     = "public-ingress"
  }

  depends_on = [
    terraform_data.post_install,
    helm_release.backend_operator,
    helm_release.gpu_operator,
    helm_release.network_operator,
    helm_release.kai_scheduler,
  ]

  provisioner "local-exec" {
    command = "/bin/bash ${path.module}/../scripts/post-install.sh"

    environment = {
      RUN_POST_INSTALL             = "false"
      RUN_APP_CONFIGURATION        = "true"
      KUBECONFIG                   = pathexpand(var.kubeconfig_path)
      KUBECTL_CONTEXT              = var.kubeconfig_context != null ? var.kubeconfig_context : ""
      OSMO_NAMESPACE               = var.namespace
      WORKFLOWS_NAMESPACE          = var.workflows_namespace
      INGRESS_NAMESPACE            = var.ingress_namespace
      INGRESS_HOSTNAME             = var.ingress_hostname
      AUTH_DOMAIN                  = local.auth_domain
      TLS_ENABLED                  = tostring(var.tls_enabled)
      TLS_MODE                     = var.tls_mode
      TLS_SECRET_NAME              = var.tls_secret_name
      KEYCLOAK_TLS_SECRET_NAME     = var.keycloak_tls_secret_name
      ENABLE_AUTH                  = tostring(var.enable_auth)
      DEPLOY_UI                    = tostring(var.deploy_ui)
      SERVICE_BASE_URL             = local.service_base_url_value
      POSTGRES_HOST                = local.postgres_host
      POSTGRES_PORT                = tostring(local.postgres_port)
      POSTGRES_DB                  = local.postgres_db
      POSTGRES_USER                = local.postgres_user
      POSTGRES_PASSWORD            = local.postgres_password
      BACKEND_NAME                 = var.backend_name
      STORAGE_BUCKET               = local.infra_storage_bucket_name
      STORAGE_ENDPOINT             = local.storage_endpoint
      STORAGE_REGION               = local.storage_region
      STORAGE_ACCESS_KEY_ID        = local.storage_access_key_id
      STORAGE_SECRET_ACCESS_KEY    = local.storage_secret_access_key
      CONFIGURE_WORKFLOW_STORAGE   = tostring(var.configure_workflow_storage)
      CONFIGURE_DATASET_BUCKET     = tostring(var.configure_dataset_bucket)
      DATASET_BUCKET_NAME          = var.dataset_bucket_name
      CONFIGURE_BACKEND_SCHEDULER  = tostring(local.configure_backend_scheduler_effective)
      CONFIGURE_GPU_PLATFORM       = tostring(local.configure_gpu_platform_effective)
      CONFIGURE_FILESTORE_MOUNTS   = tostring(local.infra_filestore_enabled)
      GPU_PLATFORM_NAME            = local.gpu_platform_name_value
      NEBIUS_REGION                = local.storage_region
      DEFAULT_USER_POD_TEMPLATE    = "${path.module}/../config/osmo/default_user_pod_template.json"
      GPU_POD_TEMPLATE             = "${path.module}/../config/osmo/gpu_pod_template.json"
      SHM_POD_TEMPLATE             = "${path.module}/../config/osmo/shm_pod_template.json"
      GPU_PLATFORM_UPDATE_TEMPLATE = "${path.module}/../config/osmo/gpu_platform_update.json"
    }
  }
}
