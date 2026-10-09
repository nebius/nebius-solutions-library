mock_provider "kubernetes" {}

variables {
  parent_id = "project-test"
  ngc_key   = "test-key"
}

run "default_catalog_is_disabled_and_state_stable" {
  command = plan

  assert {
    condition     = length(kubernetes_deployment_v1.nims) == 20
    error_message = "The built-in catalog must render all 20 built-in NIM deployments."
  }

  assert {
    condition     = length(kubernetes_service_v1.nims) == 20
    error_message = "The built-in catalog must render all 20 built-in NIM services."
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
    condition     = length(kubernetes_manifest.nim_service_monitor) == 20
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

  assert {
    condition = alltrue([
      for key in ["evo2_40b", "qwen3-next-80b-a3b-instruct"] :
      kubernetes_deployment_v1.nims[key].spec[0].template[0].spec[0].container[0].resources[0].limits["nvidia.com/gpu"] == "2" &&
      kubernetes_deployment_v1.nims[key].spec[0].template[0].spec[0].container[0].resources[0].requests["nvidia.com/gpu"] == "2"
    ])
    error_message = "Evo2-40B and Qwen3 Next must retain their two-GPU defaults for supported H100 profiles."
  }
}

run "new_models_have_complete_routing_and_startup_configuration" {
  command = plan

  variables {
    model_catalog = {
      alphafold2_multimer = { enabled = true }
      maisi               = { enabled = true }
      vista3d             = { enabled = true }
      nemotron_3_nano     = { enabled = true }
    }
  }

  assert {
    condition = alltrue([
      for key in ["alphafold2_multimer", "maisi", "vista3d", "nemotron_3_nano"] :
      kubernetes_deployment_v1.nims[key].spec[0].replicas == "1" &&
      kubernetes_service_v1.nims[key].spec[0].selector.app == kubernetes_deployment_v1.nims[key].spec[0].template[0].metadata[0].labels.app &&
      kubernetes_service_v1.nims[key].spec[0].port[0].target_port == "8000" &&
      contains(keys(kubernetes_manifest.nim_service_monitor), key)
    ])
    error_message = "Each enabled new NIM must have one replica, a matching internal Service, and a ServiceMonitor."
  }

  assert {
    condition     = length(kubernetes_horizontal_pod_autoscaler_v2.nims) == 0
    error_message = "New NIMs must remain fixed-replica until their autoscaling metrics have been validated."
  }

  assert {
    condition = alltrue([
      for key, image in {
        alphafold2_multimer = "nvcr.io/nim/deepmind/alphafold2-multimer:1.0.0"
        maisi               = "nvcr.io/nim/nvidia/maisi:1.0.1"
        vista3d             = "nvcr.io/nim/nvidia/vista3d:1.0.0"
        nemotron_3_nano     = "nvcr.io/nim/nvidia/nemotron-3-nano:1.7.0-variant"
      } : kubernetes_deployment_v1.nims[key].spec[0].template[0].spec[0].container[0].image == image &&
      kubernetes_deployment_v1.nims[key].spec[0].template[0].spec[0].container[0].command == null
    ])
    error_message = "New models must use documented pinned image tags and their image's default entrypoint."
  }

  assert {
    condition = alltrue([
      for key, port in { maisi = 8011, vista3d = 8012, alphafold2_multimer = 8013, nemotron_3_nano = 8014 } :
      output.nim_catalog[key].proxy_port == port &&
      one([for p in kubernetes_service_v1.model_lbs["protein-apps"].spec[0].port : p if p.name == kubernetes_deployment_v1.nims[key].metadata[0].name]).port == port &&
      contains([for p in kubernetes_deployment_v1.tcp_proxy["protein-apps"].spec[0].template[0].spec[0].container[0].port : p.container_port], port) &&
      strcontains(kubernetes_config_map_v1.tcp_proxy["protein-apps"].data["nginx.conf"], "listen ${port};") &&
      strcontains(kubernetes_config_map_v1.tcp_proxy["protein-apps"].data["nginx.conf"], "server ${kubernetes_service_v1.nims[key].metadata[0].name}.nims.svc.cluster.local:8000;") &&
      jsondecode(one([for env in kubernetes_deployment_v1.metadata_service.spec[0].template[0].spec[0].container[0].env : env if env.name == "NIM_PORTS_JSON"]).value)[kubernetes_deployment_v1.nims[key].metadata[0].name] == port
    ])
    error_message = "The new NIM ports must agree across outputs, LoadBalancer, nginx, container ports, and metadata."
  }

  assert {
    condition = alltrue([
      for env_name in ["NGC_API_KEY", "NGC_CLI_API_KEY"] :
      one([for env in kubernetes_deployment_v1.nims["alphafold2_multimer"].spec[0].template[0].spec[0].container[0].env : env if env.name == env_name]).value_from[0].secret_key_ref[0].name == "ngc-api-key" &&
      one([for env in kubernetes_deployment_v1.nims["alphafold2_multimer"].spec[0].template[0].spec[0].container[0].env : env if env.name == env_name]).value_from[0].secret_key_ref[0].key == "NGC_API_KEY"
    ])
    error_message = "AlphaFold2-Multimer requires NGC_CLI_API_KEY sourced from the existing NGC secret."
  }

  assert {
    condition = (
      kubernetes_deployment_v1.nims["alphafold2_multimer"].spec[0].template[0].spec[0].container[0].resources[0].requests.cpu == "24" &&
      kubernetes_deployment_v1.nims["alphafold2_multimer"].spec[0].template[0].spec[0].container[0].resources[0].limits.cpu == "24"
    )
    error_message = "AlphaFold2-Multimer must meet NVIDIA's 24-core minimum."
  }
}

run "evo2_h200_can_override_gpu_count_without_changing_other_models" {
  command = plan

  variables {
    model_catalog = {
      evo2_40b = {
        enabled = true
        resources = {
          limits   = { "nvidia.com/gpu" = "1" }
          requests = { "nvidia.com/gpu" = "1" }
        }
      }
    }
  }

  assert {
    condition = (
      kubernetes_deployment_v1.nims["evo2_40b"].spec[0].template[0].spec[0].container[0].resources[0].limits["nvidia.com/gpu"] == "1" &&
      kubernetes_deployment_v1.nims["evo2_40b"].spec[0].template[0].spec[0].container[0].resources[0].requests["nvidia.com/gpu"] == "1" &&
      kubernetes_deployment_v1.nims["evo2_40b"].spec[0].template[0].spec[0].container[0].resources[0].requests.memory == "256Gi" &&
      kubernetes_deployment_v1.nims["qwen3-next-80b-a3b-instruct"].spec[0].template[0].spec[0].container[0].resources[0].requests["nvidia.com/gpu"] == "2"
    )
    error_message = "The H200 GPU override must merge with Evo2's host resources and preserve Qwen's GPU allocation."
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
    condition     = length(kubernetes_deployment_v1.nims) == 21 && length(kubernetes_service_v1.nims) == 21
    error_message = "A catalog-only NIM addition must derive its Deployment and Service."
  }

  assert {
    condition     = contains(keys(kubernetes_manifest.nim_service_monitor), "catalog_test")
    error_message = "A catalog-only NIM addition must derive its ServiceMonitor."
  }

  assert {
    condition     = output.nim_catalog["catalog_test"].proxy_port == 8015
    error_message = "A new protein-apps NIM must receive the next derived proxy port without manual assignment."
  }

  assert {
    condition     = output.nim_catalog["catalog_test"].service_url == "http://catalog-test-svc.nims.svc.cluster.local:8000"
    error_message = "The exported catalog must provide the in-cluster model endpoint."
  }
}
