output "nims_lb_ip" {
  description = "LoadBalancer IP for all NIMs"
  value       = var.proxy_service_type == "LoadBalancer" ? try(kubernetes_service_v1.model_lbs["protein-apps"].status[0].load_balancer[0].ingress[0].ip, null) : null
}

output "cosmos_lb_ip" {
  description = "LoadBalancer IP for Cosmos World Foundation Models"
  value       = var.proxy_service_type == "LoadBalancer" ? try(kubernetes_service_v1.model_lbs["cosmos"].status[0].load_balancer[0].ingress[0].ip, null) : null
}

output "nim_catalog" {
  description = "Resolved NIM catalog and model-to-port contract for in-cluster consumers such as nebius-bionemo-mcp."
  value = {
    for key, model in local.nim_models : key => {
      display_name    = model.display_name
      enabled         = model.enabled
      deployment_name = model.deployment_name
      pod_selector_labels = {
        app = model.app
      }
      service_name       = model.service_name
      service_port       = model.service_port
      service_url        = "http://${model.service_name}.${var.namespace}.svc.cluster.local:${model.service_port}"
      image              = model.image
      version            = model.version
      lb_group           = model.lb_group
      proxy_port         = try(local.nim_proxy_ports[model.deployment_name], null)
      scaling_enabled    = model.scaling.enabled
      scaling_metric     = try(model.scaling.metric_name, null)
      min_replicas       = model.scaling.enabled ? model.scaling.min_replicas : null
      max_replicas       = model.scaling.enabled ? model.scaling.max_replicas : null
      fixed_replica_note = model.scaling.enabled ? null : model.scaling.fixed_reason
    }
  }
}
