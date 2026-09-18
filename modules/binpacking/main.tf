data "kubectl_file_documents" "binpacking_scheduler_manifests" {
  content = templatefile("${path.module}/files/bp-scheduler.yaml.tftpl", {
    kube_sched_ver = var.kube_sched_ver
  })
}

# Use kubectl_file_documents to split multi-document into the kubectl_manifest resource
resource "kubectl_manifest" "binpacking_scheduler" {
  for_each  = data.kubectl_file_documents.binpacking_scheduler_manifests.manifests
  yaml_body = each.value
}
