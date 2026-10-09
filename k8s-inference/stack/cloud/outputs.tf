output "clusters" {
  description = "Per cluster id: roles, region, project, Kubernetes endpoint and CA, gateway IP, weights filesystem."
  value = { for id, c in local.clusters : id => {
    id                     = id
    name                   = "${local.f.name}-${id}"
    region                 = c.region
    project_id             = c.project_id
    roles                  = local.roles[id]
    cluster_id             = module.cluster[id].cluster_id
    endpoint               = module.cluster[id].endpoint
    cluster_ca_certificate = module.cluster[id].cluster_ca_certificate
    gateway_ip             = module.cluster[id].gateway_ip
    gateway_allocation_id  = module.cluster[id].gateway_allocation_id
    weights_filesystem_id  = module.cluster[id].weights_filesystem_id
    weights_filesystem     = c.weights_filesystem
    gpu_node_group_ids     = module.cluster[id].gpu_node_group_ids
    ops_service_account_id = nebius_iam_v1_service_account.ops[c.project_id].id
    kubeconfig_command     = "nebius mk8s cluster get-credentials --id ${module.cluster[id].cluster_id} --external"
  } }
}

output "hub" {
  value = {
    region                  = local.hub_region
    id                      = local.hub_id
    project_id              = local.hub_project
    registry                = local.images_source
    registry_id             = local.f.images.source == null ? nebius_registry_v1_registry.images[0].id : null
    backups_bucket          = nebius_storage_v1_bucket.backups.name
    backups_bucket_endpoint = "https://storage.${local.hub_region}.nebius.cloud"
  }
}

output "secrets" {
  description = "Generated credentials consumed by the platform stage (sensitive)."
  sensitive   = true
  value = {
    ops_credentials = local.ops_credentials # project id -> service-account credentials JSON
    backups_s3 = {
      access_key_id = nebius_iam_v2_access_key.backups.status.aws_access_key_id
      secret        = nebius_iam_v2_access_key.backups.status.secret
    }
    litellm_master_key     = "sk-${random_password.litellm_master.result}"
    database_password      = random_password.database.result # the managed PostgreSQL user (database.tf)
    grafana_admin_password = random_password.grafana_admin.result
  }
}
