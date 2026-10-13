output "cluster_id" { value = nebius_mk8s_v1_cluster.this.id }
output "endpoint" {
  description = "Public Kubernetes API endpoint."
  value       = nebius_mk8s_v1_cluster.this.status.control_plane.endpoints.public_endpoint
}
output "cluster_ca_certificate" {
  value = nebius_mk8s_v1_cluster.this.status.control_plane.auth.cluster_ca_certificate
}
output "gateway_ip" {
  description = "Static public IP of the gateway load balancer (null in internal mode)."
  value       = var.public_ip ? split("/", nebius_vpc_v1_allocation.gateway[0].status.details.allocated_cidr)[0] : null
}
output "gateway_allocation_id" {
  value = var.public_ip ? nebius_vpc_v1_allocation.gateway[0].id : null
}
output "weights_filesystem_id" { value = local.weights_fs_id }
output "nodepull_service_account_id" { value = nebius_iam_v1_service_account.nodepull.id }
output "gpu_node_group_ids" { value = { for k, g in nebius_mk8s_v1_node_group.gpu : k => g.id } }
output "gpu_cluster_ids" {
  description = "Nebius GPU cluster (InfiniBand) per pool with interconnect = infiniband."
  value       = { for pn, c in nebius_compute_v1_gpu_cluster.ib : pn => c.id }
}
