locals {
  lb_groups = {
    protein-apps = {
      app                   = "nims-proxy"
      config_map_name       = "nginx-tcp-proxy-config"
      deployment_name       = "nims-proxy"
      service_name          = "nims-gateway"
      base_port             = 8000
      legacy_order          = ["openfold3", "boltz2", "evo2_40b", "msa_search", "openfold2", "genmol", "molmim", "diffdock", "qwen3-next-80b-a3b-instruct", "proteinmpnn", "rfdiffusion"]
      extra_upstreams       = ["metadata"]
      proxy_timeout         = "600s"
      proxy_connect_timeout = "10s"
    }

    cosmos = {
      app                   = "cosmos-proxy"
      config_map_name       = "cosmos-tcp-proxy-config"
      deployment_name       = "cosmos-proxy"
      service_name          = "cosmos-gateway"
      base_port             = 8000
      legacy_order          = ["cosmos_reason1_7b", "cosmos_reason2_8b", "cosmos_reason2_2b", "cosmos_embed1", "nemotron_nano_12b_v2_vl"]
      extra_upstreams       = []
      proxy_timeout         = "600s"
      proxy_connect_timeout = "10s"
    }
  }

  proxy_nim_models = {
    for name, model in local.nim_models : name => model
    if model.proxy.enabled
  }

  extra_proxy_upstreams = {
    metadata = {
      display_name          = "metadata-service"
      upstream_name         = "metadata"
      listen_port           = 8080
      service_name          = kubernetes_service_v1.metadata_service.metadata[0].name
      service_port          = 8080
      service_port_name     = "metadata"
      proxy_timeout         = "30s"
      proxy_connect_timeout = "5s"
    }
  }

  lb_group_model_keys = {
    for group, config in local.lb_groups : group => concat(
      [
        for key in config.legacy_order : key
        if contains(keys(local.proxy_nim_models), key) && local.proxy_nim_models[key].lb_group == group
      ],
      sort([
        for key, model in local.proxy_nim_models : key
        if model.lb_group == group && !contains(config.legacy_order, key)
      ])
    )
  }

  lb_group_upstreams = {
    for group, model_keys in local.lb_group_model_keys : group => concat(
      [
        for index, key in model_keys : {
          display_name          = local.nim_models[key].display_name
          deployment_name       = local.nim_models[key].deployment_name
          upstream_name         = local.nim_models[key].proxy.upstream_name == null ? key : local.nim_models[key].proxy.upstream_name
          listen_port           = local.lb_groups[group].base_port + index
          service_name          = local.nim_models[key].service_name
          service_port          = local.nim_models[key].service_port
          service_port_name     = local.nim_models[key].proxy.service_port_name == null ? local.nim_models[key].deployment_name : local.nim_models[key].proxy.service_port_name
          proxy_timeout         = local.lb_groups[group].proxy_timeout
          proxy_connect_timeout = local.lb_groups[group].proxy_connect_timeout
        }
      ],
      [
        for key in local.lb_groups[group].extra_upstreams : local.extra_proxy_upstreams[key]
      ]
    )
  }

  nim_proxy_ports = merge([
    for group, model_keys in local.lb_group_model_keys : {
      for index, key in model_keys : local.nim_models[key].deployment_name => local.lb_groups[group].base_port + index
    }
  ]...)

  lb_group_ports = {
    for group, upstreams in local.lb_group_upstreams : group => {
      for upstream in upstreams : upstream.service_port_name => upstream
    }
  }
}

resource "kubernetes_config_map_v1" "tcp_proxy" {
  for_each = local.lb_groups

  depends_on = [kubernetes_namespace_v1.nims]

  metadata {
    name      = each.value.config_map_name
    namespace = var.namespace
  }

  data = {
    "nginx.conf" = <<-EOF
      worker_processes auto;
      error_log /dev/stderr info;

      events {
        worker_connections 1024;
      }

      stream {
        log_format basic '$remote_addr [$time_local] '
                         '$protocol $status $bytes_sent $bytes_received '
                         '$session_time "$upstream_addr"';
        access_log /dev/stdout basic;

      %{for upstream in local.lb_group_upstreams[each.key]}
        # Port ${upstream.listen_port} -> ${upstream.display_name}
        upstream ${upstream.upstream_name} {
          server ${upstream.service_name}.${var.namespace}.svc.cluster.local:${upstream.service_port};
        }
        server {
          listen ${upstream.listen_port};
          proxy_pass ${upstream.upstream_name};
          proxy_timeout ${upstream.proxy_timeout};
          proxy_connect_timeout ${upstream.proxy_connect_timeout};
        }

      %{endfor}
      }
    EOF
  }
}

resource "kubernetes_deployment_v1" "tcp_proxy" {
  for_each = local.lb_groups

  depends_on = [kubernetes_namespace_v1.nims]

  metadata {
    name      = each.value.deployment_name
    namespace = var.namespace
  }

  spec {
    replicas = 2

    selector {
      match_labels = {
        app = each.value.app
      }
    }

    template {
      metadata {
        labels = {
          app = each.value.app
        }
      }

      spec {
        container {
          name  = "nginx"
          image = "nginx:1.27-alpine"

          resources {
            limits = {
              cpu    = "500m"
              memory = "256Mi"
            }
            requests = {
              cpu    = "100m"
              memory = "128Mi"
            }
          }

          dynamic "port" {
            for_each = local.lb_group_ports[each.key]
            content {
              container_port = port.value.listen_port
            }
          }

          volume_mount {
            name       = "nginx-config"
            mount_path = "/etc/nginx/nginx.conf"
            sub_path   = "nginx.conf"
          }
        }

        volume {
          name = "nginx-config"
          config_map {
            name = kubernetes_config_map_v1.tcp_proxy[each.key].metadata[0].name
          }
        }
      }
    }
  }
}

resource "kubernetes_service_v1" "model_lbs" {
  for_each = local.lb_groups

  depends_on = [kubernetes_namespace_v1.nims]

  metadata {
    name      = each.value.service_name
    namespace = var.namespace
  }

  spec {
    selector = {
      app = each.value.app
    }

    type = var.proxy_service_type

    dynamic "port" {
      for_each = local.lb_group_ports[each.key]
      content {
        name        = port.value.service_port_name
        port        = port.value.listen_port
        target_port = port.value.listen_port
        protocol    = "TCP"
      }
    }
  }
}
