variable "kubeconfig_path" {
  description = "Path to the kubeconfig file Terraform should use."
  type        = string
  default     = "./generated/kubeconfig"
}

variable "kubeconfig_context" {
  description = "Optional kubeconfig context name."
  type        = string
  default     = null
}

variable "infra_state_path" {
  description = "Path to the tf-deploy infra Terraform state file."
  type        = string
  default     = "./infra/terraform.tfstate"
}

variable "namespace" {
  description = "Namespace where OSMO will be deployed."
  type        = string
  default     = "osmo"
}

variable "ingress_namespace" {
  description = "Namespace of the ingress-nginx controller."
  type        = string
  default     = "ingress-nginx"
}

variable "ingress_release_name" {
  description = "Helm release name for ingress-nginx when tf-deploy manages it."
  type        = string
  default     = "ingress-nginx"
}

variable "ingress_controller_service_name" {
  description = "Service name for the ingress-nginx controller used for in-cluster hostname aliases. Defaults to <ingress_release_name>-controller."
  type        = string
  default     = null
}

variable "deploy_ingress_nginx" {
  description = "Whether tf-deploy should install ingress-nginx instead of assuming it already exists."
  type        = bool
  default     = true
}

variable "ingress_nginx_chart_version" {
  description = "ingress-nginx Helm chart version."
  type        = string
  default     = "4.14.2"
}

variable "ingress_service_annotations" {
  description = "Optional annotations to add to the ingress-nginx controller Service."
  type        = map(string)
  default     = {}
}

variable "deploy_ui" {
  description = "Whether to deploy the OSMO Web UI."
  type        = bool
  default     = true
}

variable "enable_auth" {
  description = "Whether to enable Keycloak/Envoy auth wiring."
  type        = bool
  default     = true
}

variable "ingress_hostname" {
  description = "Browser-facing OSMO hostname, for example osmo.example.com."
  type        = string
}

variable "keycloak_hostname" {
  description = "External Keycloak hostname. Defaults to auth-<ingress_hostname> when omitted."
  type        = string
  default     = null
}

variable "keycloak_external_url" {
  description = "Full external Keycloak base URL. Defaults to https://<keycloak hostname>."
  type        = string
  default     = null
}

variable "keycloak_release_name" {
  description = "Helm release name for Keycloak."
  type        = string
  default     = "keycloak"
}

variable "keycloak_chart_version" {
  description = "Bitnami Keycloak chart version."
  type        = string
  default     = "24.4.9"
}

variable "keycloak_tls_secret_name" {
  description = "Kubernetes TLS secret for the Keycloak ingress."
  type        = string
  default     = "keycloak-tls"
}

variable "keycloak_admin_password" {
  description = "Optional pre-generated Keycloak admin password. Terraform generates one when omitted."
  type        = string
  default     = null
  sensitive   = true
}

variable "keycloak_db_password" {
  description = "Optional pre-generated Keycloak PostgreSQL password. Terraform generates one when omitted."
  type        = string
  default     = null
  sensitive   = true
}

variable "keycloak_create_breakglass_user" {
  description = "Whether Terraform should create the local osmo-admin Keycloak user."
  type        = bool
  default     = true
}

variable "tls_enabled" {
  description = "Whether the OSMO ingress terminates TLS."
  type        = bool
  default     = true
}

variable "tls_secret_name" {
  description = "Kubernetes TLS secret for the main OSMO ingress."
  type        = string
  default     = "osmo-tls"
}

variable "tls_mode" {
  description = "TLS mode for ingress handling. cert-manager is the default for real public hostnames; self-signed remains available as a fallback."
  type        = string
  default     = "cert-manager"

  validation {
    condition     = contains(["self-signed", "cert-manager"], var.tls_mode)
    error_message = "tls_mode must be one of: self-signed, cert-manager."
  }
}

variable "cluster_issuer_name" {
  description = "cert-manager ClusterIssuer name when tls_mode=cert-manager."
  type        = string
  default     = "letsencrypt-prod"
}

variable "deploy_cert_manager" {
  description = "Whether tf-deploy should install cert-manager and manage the configured ClusterIssuer when tls_mode=cert-manager."
  type        = bool
  default     = true
}

variable "cert_manager_namespace" {
  description = "Namespace for the cert-manager Helm release."
  type        = string
  default     = "cert-manager"
}

variable "cert_manager_chart_version" {
  description = "Optional cert-manager chart version."
  type        = string
  default     = null
}

variable "cert_manager_email" {
  description = "ACME email address for the managed cert-manager ClusterIssuer."
  type        = string
  default     = null
}

variable "cert_manager_acme_server" {
  description = "ACME directory URL for the managed cert-manager ClusterIssuer."
  type        = string
  default     = "https://acme-v02.api.letsencrypt.org/directory"
}

variable "cert_manager_http01_ingress_class" {
  description = "Ingress class used by cert-manager HTTP-01 solvers."
  type        = string
  default     = "nginx"
}

variable "dns_base_domain" {
  description = "Base domain used to derive relative DNS names for public record management."
  type        = string
  default     = null
}

variable "dns_npc_profile" {
  description = "Optional npc profile used to manage public DNS recordsets for the derived OSMO and Keycloak hostnames."
  type        = string
  default     = null
}

variable "dns_zone_id" {
  description = "Optional public DNS zone id used to manage OSMO and Keycloak A recordsets."
  type        = string
  default     = null
}

variable "oauth2_proxy_insecure_skip_tls_verify" {
  description = "Whether oauth2-proxy sidecars should skip TLS verification when calling the IdP. Defaults to true only for self-signed installs."
  type        = bool
  default     = null
}

variable "oauth2_proxy_cookie_refresh" {
  description = "oauth2-proxy cookie refresh interval."
  type        = string
  default     = "4m"
}

variable "oidc_client_secret" {
  description = "Client secret for the Keycloak osmo-browser-flow client. Terraform generates one when omitted."
  type        = string
  default     = null
  sensitive   = true
}

variable "oauth2_proxy_cookie_secret" {
  description = "Optional pre-generated oauth2-proxy cookie secret. Terraform generates one when omitted."
  type        = string
  default     = null
  sensitive   = true
}

variable "postgres_host" {
  description = "External PostgreSQL hostname or address."
  type        = string
  default     = null
}

variable "postgres_port" {
  description = "External PostgreSQL port."
  type        = number
  default     = null
}

variable "postgres_db" {
  description = "OSMO PostgreSQL database name."
  type        = string
  default     = null
}

variable "postgres_user" {
  description = "OSMO PostgreSQL username."
  type        = string
  default     = null
}

variable "postgres_password" {
  description = "OSMO PostgreSQL password."
  type        = string
  default     = null
  sensitive   = true
}

variable "storage_access_key_id" {
  description = "Access key id for the S3-compatible storage secret."
  type        = string
  default     = null
}

variable "storage_secret_access_key" {
  description = "Secret access key for the S3-compatible storage secret."
  type        = string
  default     = null
  sensitive   = true
}

variable "storage_region" {
  description = "Storage region, for example eu-north1."
  type        = string
  default     = null
}

variable "storage_endpoint" {
  description = "HTTPS S3 endpoint, for example https://storage.eu-north1.nebius.cloud."
  type        = string
  default     = null
}

variable "nebius_sso_enabled" {
  description = "Whether to configure Nebius SSO as a Keycloak identity provider."
  type        = bool
  default     = false
}

variable "nebius_sso_issuer_url" {
  description = "Nebius SSO issuer URL."
  type        = string
  default     = "https://auth.nebius.com"
}

variable "nebius_sso_client_id" {
  description = "Nebius SSO OIDC client id."
  type        = string
  default     = null
}

variable "nebius_sso_client_secret" {
  description = "Nebius SSO OIDC client secret."
  type        = string
  default     = null
  sensitive   = true
}

variable "nebius_sso_group_attribute" {
  description = "OIDC claim name Keycloak should use for Nebius SSO groups."
  type        = string
  default     = "groups"
}

variable "mek_id" {
  description = "MEK identifier used in the ConfigMap/secret payload."
  type        = string
  default     = "key1"
}

variable "mek_encoded" {
  description = "Optional base64-encoded JWK payload. Terraform generates one when omitted."
  type        = string
  default     = null
  sensitive   = true
}

variable "osmo_image_tag" {
  description = "OSMO image tag to deploy."
  type        = string
  default     = "6.2"
}

variable "osmo_chart_version" {
  description = "Shared OSMO Helm chart version for service, router, and web-ui."
  type        = string
  default     = "1.2.1"
}

variable "service_base_url" {
  description = "Optional service_base_url override. Defaults to the ingress hostname and TLS mode."
  type        = string
  default     = null
}

variable "redis_chart_version" {
  description = "Bitnami Redis chart version."
  type        = string
  default     = "25.3.1"
}

variable "deploy_observability" {
  description = "Whether tf-deploy should deploy the observability stack (Prometheus, Grafana, Loki, Promtail)."
  type        = bool
  default     = true
}

variable "monitoring_namespace" {
  description = "Namespace for the observability stack."
  type        = string
  default     = "monitoring"
}

variable "kube_prometheus_stack_chart_version" {
  description = "Optional kube-prometheus-stack chart version."
  type        = string
  default     = null
}

variable "loki_stack_chart_version" {
  description = "Optional Loki chart version."
  type        = string
  default     = null
}

variable "promtail_chart_version" {
  description = "Optional Promtail chart version."
  type        = string
  default     = null
}

variable "grafana_admin_password" {
  description = "Optional pre-generated Grafana admin password. Terraform generates one when omitted."
  type        = string
  default     = null
  sensitive   = true
}

variable "deploy_backend_operator" {
  description = "Whether tf-deploy should deploy the OSMO backend operator."
  type        = bool
  default     = true
}

variable "backend_operator_namespace" {
  description = "Namespace for the OSMO backend operator."
  type        = string
  default     = "osmo-operator"
}

variable "workflows_namespace" {
  description = "Namespace for OSMO workflow pods managed by the backend operator."
  type        = string
  default     = "osmo-workflows"
}

variable "backend_operator_release_name" {
  description = "Helm release name for the OSMO backend operator."
  type        = string
  default     = "osmo-operator"
}

variable "backend_operator_chart_version" {
  description = "Optional OSMO backend operator chart version."
  type        = string
  default     = null
}

variable "backend_name" {
  description = "Logical backend name registered in OSMO."
  type        = string
  default     = "default"
}

variable "backend_operator_service_url" {
  description = "Service URL used by the backend operator to connect to OSMO. Defaults to the internal osmo-agent service."
  type        = string
  default     = null
}

variable "backend_operator_username" {
  description = "Keycloak username used by the backend operator when loginMethod=password."
  type        = string
  default     = "osmo-admin"
}

variable "backend_operator_password" {
  description = "Keycloak password used by the backend operator when loginMethod=password."
  type        = string
  default     = "osmo-admin"
  sensitive   = true
}

variable "backend_operator_password_secret_name" {
  description = "Secret name for the backend operator password."
  type        = string
  default     = "osmo-operator-password"
}

variable "backend_operator_password_secret_key" {
  description = "Secret key for the backend operator password."
  type        = string
  default     = "password"
}

variable "backend_operator_login_method" {
  description = "Backend operator auth method. Defaults to password when auth is enabled, otherwise token."
  type        = string
  default     = null

  validation {
    condition     = var.backend_operator_login_method == null || contains(["password", "token"], var.backend_operator_login_method)
    error_message = "backend_operator_login_method must be null, password, or token."
  }
}

variable "backend_operator_service_token" {
  description = "Service token used by the backend operator when loginMethod=token."
  type        = string
  default     = null
  sensitive   = true
}

variable "deploy_gpu_infrastructure" {
  description = "Whether tf-deploy should deploy GPU operator and KAI scheduler. Defaults to true when infra reports GPU nodes."
  type        = bool
  default     = null
}

variable "configure_gpu_platform" {
  description = "Whether tf-deploy should configure the OSMO GPU platform and pod templates. Defaults to the GPU infrastructure setting."
  type        = bool
  default     = null
}

variable "deploy_network_operator" {
  description = "Whether tf-deploy should deploy the NVIDIA Network Operator for InfiniBand support."
  type        = bool
  default     = false
}

variable "gpu_operator_namespace" {
  description = "Namespace for the NVIDIA GPU Operator."
  type        = string
  default     = "gpu-operator"
}

variable "network_operator_namespace" {
  description = "Namespace for the NVIDIA Network Operator."
  type        = string
  default     = "network-operator"
}

variable "kai_scheduler_namespace" {
  description = "Namespace for the KAI scheduler."
  type        = string
  default     = "kai-scheduler"
}

variable "gpu_operator_chart_version" {
  description = "Optional NVIDIA GPU Operator chart version."
  type        = string
  default     = null
}

variable "gpu_driver_version" {
  description = "Optional NVIDIA GPU driver version override for the GPU Operator when driver.enabled=true."
  type        = string
  default     = null
}

variable "network_operator_chart_version" {
  description = "Optional NVIDIA Network Operator chart version."
  type        = string
  default     = null
}

variable "kai_scheduler_chart_version" {
  description = "KAI scheduler chart version."
  type        = string
  default     = "v0.9.8"
}

variable "gpu_platform_name" {
  description = "Friendly OSMO GPU platform name, for example H100."
  type        = string
  default     = null
}

variable "configure_workflow_storage" {
  description = "Whether tf-deploy should configure OSMO workflow_data and workflow_log to use the Nebius object store."
  type        = bool
  default     = true
}

variable "configure_dataset_bucket" {
  description = "Whether tf-deploy should register the Nebius object store as the default OSMO dataset bucket."
  type        = bool
  default     = true
}

variable "dataset_bucket_name" {
  description = "OSMO short name for the default dataset bucket."
  type        = string
  default     = "nebius"
}

variable "configure_backend_scheduler" {
  description = "Whether tf-deploy should patch the backend config to use the KAI scheduler."
  type        = bool
  default     = true
}
