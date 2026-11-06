data "http" "manifest" {
  count = var.config.manifest_file == null ? 1 : 0
  url   = "https://raw.githubusercontent.com/kubeflow/mpi-operator/v${var.config.version}/deploy/v2beta1/mpi-operator.yaml"
}

data "kubectl_file_documents" "manifest" {
  content = var.config.manifest_file == null ? data.http.manifest[0].response_body : file(var.config.manifest_file)
}

resource "kubectl_manifest" "namespace" {
  for_each          = { for key, body in data.kubectl_file_documents.manifest.manifests : key => body if yamldecode(body).kind == "Namespace" }
  yaml_body         = each.value
  server_side_apply = true
}

resource "kubectl_manifest" "this" {
  for_each          = { for key, body in data.kubectl_file_documents.manifest.manifests : key => body if yamldecode(body).kind != "Namespace" }
  yaml_body         = each.value
  server_side_apply = true
  depends_on        = [kubectl_manifest.namespace]
}
