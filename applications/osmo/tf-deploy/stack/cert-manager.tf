resource "helm_release" "cert_manager" {
  count = var.tls_enabled && var.tls_mode == "cert-manager" && var.deploy_cert_manager ? 1 : 0

  name            = "cert-manager"
  namespace       = var.cert_manager_namespace
  repository      = "https://charts.jetstack.io"
  chart           = "cert-manager"
  version         = var.cert_manager_chart_version
  atomic          = true
  cleanup_on_fail = true
  timeout         = 600

  set = [
    {
      name  = "crds.enabled"
      value = "true"
    }
  ]

  depends_on = [
    kubernetes_namespace_v1.cert_manager,
    terraform_data.validate,
  ]
}

resource "terraform_data" "public_dns_ready" {
  count = var.tls_enabled && var.tls_mode == "cert-manager" ? 1 : 0

  input = {
    ingress_hostname = var.ingress_hostname
    auth_hostname    = local.auth_domain
  }

  triggers_replace = {
    ingress_hostname     = var.ingress_hostname
    auth_hostname        = local.auth_domain
    ingress_namespace    = var.ingress_namespace
    ingress_service_name = var.ingress_controller_service_name != null ? var.ingress_controller_service_name : "${var.ingress_release_name}-controller"
  }

  depends_on = [
    terraform_data.ingress_ready,
    terraform_data.public_dns_records,
  ]

  provisioner "local-exec" {
    command = "/bin/bash ${path.module}/../scripts/wait-for-public-dns.sh"

    environment = {
      KUBECONFIG            = pathexpand(var.kubeconfig_path)
      KUBECTL_CONTEXT       = var.kubeconfig_context != null ? var.kubeconfig_context : ""
      INGRESS_NAMESPACE     = var.ingress_namespace
      INGRESS_SERVICE_NAME  = var.ingress_controller_service_name != null ? var.ingress_controller_service_name : "${var.ingress_release_name}-controller"
      OSMO_HOSTNAME         = var.ingress_hostname
      KEYCLOAK_HOSTNAME     = local.auth_domain
      WAIT_TIMEOUT_SECONDS  = "300"
    }
  }
}

resource "terraform_data" "public_dns_records" {
  count = var.tls_enabled && var.tls_mode == "cert-manager" && var.dns_base_domain != null && var.dns_npc_profile != null && var.dns_zone_id != null ? 1 : 0

  input = {
    kubeconfig_path      = pathexpand(var.kubeconfig_path)
    kubeconfig_context   = var.kubeconfig_context != null ? var.kubeconfig_context : ""
    ingress_namespace    = var.ingress_namespace
    ingress_service_name = var.ingress_controller_service_name != null ? var.ingress_controller_service_name : "${var.ingress_release_name}-controller"
    dns_base_domain      = var.dns_base_domain
    dns_npc_profile      = var.dns_npc_profile
    dns_zone_id          = var.dns_zone_id
    osmo_hostname        = var.ingress_hostname
    keycloak_hostname    = local.auth_domain
  }

  triggers_replace = {
    kubeconfig_path      = pathexpand(var.kubeconfig_path)
    kubeconfig_context   = var.kubeconfig_context != null ? var.kubeconfig_context : ""
    ingress_namespace    = var.ingress_namespace
    ingress_service_name = var.ingress_controller_service_name != null ? var.ingress_controller_service_name : "${var.ingress_release_name}-controller"
    dns_base_domain      = var.dns_base_domain
    dns_npc_profile      = var.dns_npc_profile
    dns_zone_id          = var.dns_zone_id
    osmo_hostname        = var.ingress_hostname
    keycloak_hostname    = local.auth_domain
  }

  depends_on = [
    terraform_data.ingress_ready,
  ]

  provisioner "local-exec" {
    command = "/bin/bash ${path.module}/../scripts/manage-public-dns-records.sh"

    environment = {
      ACTION               = "upsert"
      KUBECONFIG           = self.input.kubeconfig_path
      KUBECTL_CONTEXT      = self.input.kubeconfig_context
      INGRESS_NAMESPACE    = self.input.ingress_namespace
      INGRESS_SERVICE_NAME = self.input.ingress_service_name
      OSMO_BASE_DOMAIN     = self.input.dns_base_domain
      OSMO_HOSTNAME        = self.input.osmo_hostname
      KEYCLOAK_HOSTNAME    = self.input.keycloak_hostname
      DNS_NPC_PROFILE      = self.input.dns_npc_profile
      DNS_ZONE_ID          = self.input.dns_zone_id
    }
  }

  provisioner "local-exec" {
    when    = destroy
    command = "/bin/bash ${path.module}/../scripts/manage-public-dns-records.sh"

    environment = {
      ACTION            = "delete"
      OSMO_BASE_DOMAIN  = self.input.dns_base_domain
      OSMO_HOSTNAME     = self.input.osmo_hostname
      KEYCLOAK_HOSTNAME = self.input.keycloak_hostname
      DNS_NPC_PROFILE   = self.input.dns_npc_profile
      DNS_ZONE_ID       = self.input.dns_zone_id
    }
  }
}

resource "terraform_data" "cert_manager_ready" {
  count = var.tls_enabled && var.tls_mode == "cert-manager" && var.deploy_cert_manager ? 1 : 0

  input = {
    release_status = try(helm_release.cert_manager[0].status, "unknown")
  }

  triggers_replace = {
    namespace     = var.cert_manager_namespace
    chart_version = var.cert_manager_chart_version != null ? var.cert_manager_chart_version : "auto"
  }

  depends_on = [
    helm_release.cert_manager,
    terraform_data.public_dns_ready,
  ]

  provisioner "local-exec" {
    command = "/bin/bash ${path.module}/../scripts/wait-for-cert-manager.sh"

    environment = {
      KUBECONFIG             = pathexpand(var.kubeconfig_path)
      KUBECTL_CONTEXT        = var.kubeconfig_context != null ? var.kubeconfig_context : ""
      CERT_MANAGER_NAMESPACE = var.cert_manager_namespace
      WAIT_TIMEOUT           = "300s"
    }
  }
}

resource "terraform_data" "cert_manager_cluster_issuer" {
  count = var.tls_enabled && var.tls_mode == "cert-manager" && var.deploy_cert_manager ? 1 : 0

  input = {
    kubeconfig_path                   = pathexpand(var.kubeconfig_path)
    kubeconfig_context                = var.kubeconfig_context != null ? var.kubeconfig_context : ""
    cert_manager_namespace            = var.cert_manager_namespace
    cluster_issuer_name               = var.cluster_issuer_name
    cert_manager_email               = var.cert_manager_email
    cert_manager_acme_server         = var.cert_manager_acme_server
    cert_manager_http01_ingress_class = var.cert_manager_http01_ingress_class
  }

  triggers_replace = {
    kubeconfig_path                    = pathexpand(var.kubeconfig_path)
    kubeconfig_context                 = var.kubeconfig_context != null ? var.kubeconfig_context : ""
    cert_manager_namespace             = var.cert_manager_namespace
    cluster_issuer_name                = var.cluster_issuer_name
    cert_manager_email                 = var.cert_manager_email
    cert_manager_acme_server           = var.cert_manager_acme_server
    cert_manager_http01_ingress_class  = var.cert_manager_http01_ingress_class
  }

  depends_on = [
    terraform_data.cert_manager_ready,
  ]

  provisioner "local-exec" {
    command = "/bin/bash ${path.module}/../scripts/apply-cert-manager-cluster-issuer.sh"

    environment = {
      ACTION                            = "apply"
      KUBECONFIG                        = self.input.kubeconfig_path
      KUBECTL_CONTEXT                   = self.input.kubeconfig_context
      CERT_MANAGER_NAMESPACE            = self.input.cert_manager_namespace
      CLUSTER_ISSUER_NAME               = self.input.cluster_issuer_name
      CERT_MANAGER_EMAIL                = self.input.cert_manager_email
      CERT_MANAGER_ACME_SERVER          = self.input.cert_manager_acme_server
      CERT_MANAGER_HTTP01_INGRESS_CLASS = self.input.cert_manager_http01_ingress_class
      WAIT_TIMEOUT                      = "300s"
    }
  }

  provisioner "local-exec" {
    when    = destroy
    command = "/bin/bash ${path.module}/../scripts/apply-cert-manager-cluster-issuer.sh"

    environment = {
      ACTION                 = "delete"
      KUBECONFIG             = self.input.kubeconfig_path
      KUBECTL_CONTEXT        = self.input.kubeconfig_context
      CERT_MANAGER_NAMESPACE = self.input.cert_manager_namespace
      CLUSTER_ISSUER_NAME    = self.input.cluster_issuer_name
      WAIT_TIMEOUT                      = "300s"
    }
  }
}
