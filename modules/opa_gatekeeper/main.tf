data "http" "gatekeeper_url" {
  url = "https://raw.githubusercontent.com/open-policy-agent/gatekeeper/${var.gk_version}/deploy/gatekeeper.yaml"
}

data "kubectl_file_documents" "gatekeeper_install_documents" {
  content = data.http.gatekeeper_url.response_body
}

# Use kubectl_file_documents to split multi-document into the kubectl_manifest resource
resource "kubectl_manifest" "gatekeeper_manifests" {
  for_each  = data.kubectl_file_documents.gatekeeper_install_documents.manifests
  yaml_body = each.value
}

data "kubectl_file_documents" "gatekeeper_config_manifests" {
  content = var.configs
}

resource "kubectl_manifest" "gatekeeper_configs" {
  for_each  = data.kubectl_file_documents.gatekeeper_config_manifests.manifests
  yaml_body = each.value
  depends_on = [
    kubectl_manifest.gatekeeper_manifests
  ]
}
