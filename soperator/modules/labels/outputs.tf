output "name_nodeset_system" {
  description = "System nodeset name."
  value       = local.const.name.nodesets.system
}

output "name_nodeset_controller" {
  description = "Controller nodeset name."
  value       = local.const.name.nodesets.controller
}

output "name_nodeset_worker" {
  description = "Worker nodeset name."
  value       = local.const.name.nodesets.worker
}

output "name_nodeset_login" {
  description = "Login nodeset name."
  value       = local.const.name.nodesets.login
}

output "name_nodeset_accounting" {
  description = "Accounting nodeset name."
  value       = local.const.name.nodesets.accounting
}

output "name_nodeset_nfs" {
  description = "NFS nodeset name."
  value       = local.const.name.nodesets.nfs
}

output "name_workload_cpu" {
  description = "CPU workload name."
  value       = local.const.name.workloads.cpu
}

output "name_workload_gpu" {
  description = "GPU workload name."
  value       = local.const.name.workloads.gpu
}

output "label_nebius_gpu" {
  description = "Nebius GPU label."
  value       = local.label.nebius_gpu
}

output "label_nodeset_system" {
  description = "System nodeset label."
  value       = local.label.nodeset.system
}

output "label_nodeset_controller" {
  description = "Controller nodeset label."
  value       = local.label.nodeset.controller
}

output "label_nodeset_worker" {
  description = "Worker nodeset label."
  value       = local.label.nodeset.worker
}

output "label_nodeset_login" {
  description = "Login nodeset label."
  value       = local.label.nodeset.login
}

output "label_nodeset_accounting" {
  description = "Accounting nodeset label."
  value       = local.label.nodeset.accounting
}

output "label_nodeset_nfs" {
  description = "NFS nodeset label."
  value       = local.label.nodeset.nfs
}

output "label_workload_cpu" {
  description = "CPU workload label."
  value       = local.label.workload.cpu
}

output "label_workload_gpu" {
  description = "GPU workload label."
  value       = local.label.workload.gpu
}

output "key_nebius_gpu" {
  description = "Nebius GPU label key."
  value       = local.label_key.nebius_gpu
}

output "key_nebius_nvlink_instance_group" {
  description = "Nebius NVLink instance group label key."
  value       = local.label_key.nebius_nvlink_instance_group
}

output "key_nvidia_gpu" {
  description = "Nvidia GPU label key."
  value       = local.label_key.nvidia_gpu
}

output "key_slurm_nodeset_name" {
  description = "Slurm nodeset label key."
  value       = local.label_key.slurm_nodeset
}

output "key_slurm_nodeset_name_name" {
  description = "Slurm nodeset-name label key."
  value       = local.label_key.slurm_nodeset_name
}

output "key_slurm_workload_name" {
  description = "Slurm workload label key."
  value       = local.label_key.slurm_workload
}

output "label_jail" {
  description = "System nodeset label."
  value       = local.label.jail
}

output "label_exclude_from_external_lb" {
  description = "Exclude nodes from external load balancer"
  value       = local.label.exclude_from_external_lb
}
