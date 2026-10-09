mock_provider "kubernetes" {}

variables {
  parent_id = "project-test"
  ngc_key   = "test-key"
}

run "default_catalog_is_disabled_and_state_stable" {
  command = plan

  assert {
    condition     = length(kubernetes_deployment_v1.nims) == 16
    error_message = "The built-in catalog must render all 16 existing NIM deployments."
  }

  assert {
    condition     = length(kubernetes_service_v1.nims) == 16
    error_message = "The built-in catalog must render all 16 existing NIM services."
  }

  assert {
    condition     = alltrue([for deployment in kubernetes_deployment_v1.nims : deployment.spec[0].replicas == "0"])
    error_message = "Disabled catalog entries must render zero-replica deployments."
  }

  assert {
    condition     = length(kubernetes_horizontal_pod_autoscaler_v2.nims) == 0
    error_message = "Disabled catalog entries must not render HPAs."
  }

  assert {
    condition     = length(kubernetes_manifest.nim_service_monitor) == 16
    error_message = "Every NIM must render a ServiceMonitor."
  }

  assert {
    condition = one([
      for volume in kubernetes_deployment_v1.nims["openfold3"].spec[0].template[0].spec[0].volume : volume
      if volume.name == "mnt-data"
    ]).host_path[0].path == "/mnt/data"
    error_message = "NIMs must mount the shared filesystem root at /mnt/data."
  }

  assert {
    condition = one([
      for volume in kubernetes_deployment_v1.nims["openfold3"].spec[0].template[0].spec[0].volume : volume
      if volume.name == "mnt-data"
    ]).host_path[0].type == "Directory"
    error_message = "The /mnt/data hostPath must fail when the shared filesystem directory is absent."
  }

  assert {
    condition = one([
      for mount in kubernetes_deployment_v1.nims["openfold3"].spec[0].template[0].spec[0].container[0].volume_mount : mount
      if mount.name == "mnt-data"
    ]).sub_path == "nim"
    error_message = "NIM cache mounts must use subPath nim."
  }

  assert {
    condition     = output.nim_catalog["openfold3"].proxy_port == 8000 && output.nim_catalog["rfdiffusion"].proxy_port == 8010
    error_message = "The protein-apps legacy port range must remain 8000-8010."
  }

  assert {
    condition     = output.nim_catalog["cosmos_reason1_7b"].proxy_port == 8000 && output.nim_catalog["nemotron_nano_12b_v2_vl"].proxy_port == 8004
    error_message = "The Cosmos legacy port range must remain 8000-8004."
  }
}

run "enabled_llm_gets_custom_metric_hpa" {
  command = plan

  variables {
    model_catalog = {
      cosmos_reason2_2b = {
        enabled = true
      }
    }
  }

  assert {
    condition     = kubernetes_deployment_v1.nims["cosmos_reason2_2b"].spec[0].replicas == "1"
    error_message = "An enabled scalable NIM must start at its HPA minimum."
  }

  assert {
    condition     = length(kubernetes_horizontal_pod_autoscaler_v2.nims) == 1
    error_message = "Exactly one HPA must be rendered for the enabled scalable NIM."
  }

  assert {
    condition     = kubernetes_horizontal_pod_autoscaler_v2.nims["cosmos_reason2_2b"].spec[0].metric[0].type == "Pods"
    error_message = "LLM autoscaling must use a per-pod custom metric, not CPU or memory."
  }

  assert {
    condition     = kubernetes_horizontal_pod_autoscaler_v2.nims["cosmos_reason2_2b"].spec[0].metric[0].pods[0].metric[0].name == "vllm_num_requests_running"
    error_message = "The HPA must target the configured vLLM request metric."
  }
}

run "new_catalog_entry_derives_all_resources_and_port" {
  command = plan

  variables {
    model_catalog = {
      catalog_test = {
        display_name    = "Catalog Test"
        enabled         = true
        deployment_name = "catalog-test"
        app             = "catalog-test"
        service_name    = "catalog-test-svc"
        container_name  = "catalog-test"
        image           = "example.invalid/catalog-test"
        version         = "1.0.0"
        lb_group        = "protein-apps"
        resources = {
          limits = {
            cpu              = "1"
            memory           = "1Gi"
            "nvidia.com/gpu" = "1"
          }
          requests = {
            cpu              = "1"
            memory           = "1Gi"
            "nvidia.com/gpu" = "1"
          }
        }
      }
    }
  }

  assert {
    condition     = length(kubernetes_deployment_v1.nims) == 17 && length(kubernetes_service_v1.nims) == 17
    error_message = "A catalog-only NIM addition must derive its Deployment and Service."
  }

  assert {
    condition     = contains(keys(kubernetes_manifest.nim_service_monitor), "catalog_test")
    error_message = "A catalog-only NIM addition must derive its ServiceMonitor."
  }

  assert {
    condition     = output.nim_catalog["catalog_test"].proxy_port == 8011
    error_message = "A new protein-apps NIM must receive the next derived proxy port without manual assignment."
  }

  assert {
    condition     = output.nim_catalog["catalog_test"].service_url == "http://catalog-test-svc.nims.svc.cluster.local:8000"
    error_message = "The exported catalog must provide the in-cluster model endpoint."
  }
}
