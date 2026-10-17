output "kube_cluster" {
  description = "Kubernetes cluster info."
  value = {
    id        = try(nebius_mk8s_v1_cluster.k8s-cluster.id, null)
    name      = try(nebius_mk8s_v1_cluster.k8s-cluster.name, null)
    endpoints = nebius_mk8s_v1_cluster.k8s-cluster.status.control_plane.endpoints
  }
}

output "kube_cluster_ca_certificate" {
  sensitive = true
  value     = nebius_mk8s_v1_cluster.k8s-cluster.status.control_plane.auth.cluster_ca_certificate
}

output "runai_ui_url" {
  description = "URL of the Run:ai control plane UI (allow several minutes after apply for pods to become ready)."
  value       = "https://runai.${local.runai_base_domain}"
}

output "runai_base_domain" {
  description = "Base domain passed to the control plane as global.domain."
  value       = local.runai_base_domain
}

output "runai_cluster_release_status" {
  description = "Status of the runai-cluster helm release (null when runai_cluster_enabled = false)."
  value       = one(helm_release.runai_cluster[*].status)
}
