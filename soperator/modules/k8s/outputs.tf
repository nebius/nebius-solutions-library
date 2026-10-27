output "control_plane" {
  description = "Control plane details used by Helm."
  value = {
    public_endpoint        = nebius_mk8s_v1_cluster.this.status.control_plane.endpoints.public_endpoint
    cluster_ca_certificate = nebius_mk8s_v1_cluster.this.status.control_plane.auth.cluster_ca_certificate
  }
}

output "cluster_id" {
  description = "K8s cluster ID."
  value       = nebius_mk8s_v1_cluster.this.id
}

output "cluster_context" {
  description = "Context name of the K8s cluster."
  value       = local.context_name
}

output "static_ip_allocation_id" {
  description = "ID of the VPC allocation used for SSH connection into Slurm cluster."
  value       = nebius_vpc_v1_allocation.this.id
}

output "gpu_involved" {
  description = "Whether the GPUs were involved."
  value       = length([for worker in local.node_group_gpu_present.worker : worker if worker]) > 0
}

output "gpu_cluster_id_by_fabric" {
  description = "Map of InfiniBand fabric name to its generated GPU cluster ID."
  value = {
    for fabric, gc in nebius_compute_v1_gpu_cluster.this_v2 : fabric => gc.id
  }
}
