locals {
  root_security_context = {
    run_as_user  = 0
    run_as_group = 0
  }

  nim_start_command = ["/bin/bash", "-c", "/opt/nim/start_server.sh"]

  resources_1gpu_16cpu_128gi = {
    limits = {
      cpu              = "16"
      memory           = "128Gi"
      "nvidia.com/gpu" = "1"
    }
    requests = {
      cpu              = "16"
      memory           = "128Gi"
      "nvidia.com/gpu" = "1"
    }
  }

  resources_1gpu_8cpu_64gi = {
    limits = {
      cpu              = "8"
      memory           = "64Gi"
      "nvidia.com/gpu" = "1"
    }
    requests = {
      cpu              = "8"
      memory           = "64Gi"
      "nvidia.com/gpu" = "1"
    }
  }

  resources_2gpu_32cpu_256gi = {
    limits = {
      cpu              = "32"
      memory           = "256Gi"
      "nvidia.com/gpu" = "2"
    }
    requests = {
      cpu              = "32"
      memory           = "256Gi"
      "nvidia.com/gpu" = "2"
    }
  }

  fixed_replica_scaling = {
    enabled      = false
    fixed_reason = "No request/inference metric has been validated for HPA use."
  }

  vllm_request_scaling = {
    enabled       = true
    fixed_reason  = null
    min_replicas  = 1
    max_replicas  = 3
    metric_type   = "Pods"
    metric_name   = "vllm_num_requests_running"
    source_metric = "vllm:num_requests_running"
    target_type   = "AverageValue"
    threshold     = "2"
  }

  model_defaults = {
    kind               = "nim"
    enabled            = false
    replicas           = 1
    container_port     = 8000
    service_port       = 8000
    service_type       = "ClusterIP"
    cache_mount_path   = "/opt/nim/.cache"
    cache_sub_path     = "nim"
    mount_cache        = true
    shared_memory_size = "16Gi"
    command            = null
    security_context   = null
    env                = {}
    labels             = {}
    resources = {
      limits   = {}
      requests = {}
    }
    scaling = local.fixed_replica_scaling
    service_monitor = {
      enabled = true
      path    = "/v1/metrics"
      port    = "http"
    }
    proxy = {
      enabled           = true
      upstream_name     = null
      service_port_name = null
    }
  }

  default_model_catalog = {
    openfold3 = {
      display_name       = "OpenFold3"
      deployment_name    = "openfold3"
      app                = "openfold3"
      service_name       = "openfold3-svc"
      container_name     = "openfold3"
      image              = "nvcr.io/nim/openfold/openfold3"
      version            = "latest"
      command            = local.nim_start_command
      security_context   = local.root_security_context
      resources          = local.resources_1gpu_16cpu_128gi
      shared_memory_size = "64Gi"
      lb_group           = "protein-apps"
      scaling = merge(local.fixed_replica_scaling, {
        fixed_reason = "Structure-prediction backend does not expose a validated per-pod request queue metric for HPA."
      })
      proxy = {
        upstream_name     = "openfold3"
        service_port_name = "openfold3"
      }
    }

    boltz2 = {
      display_name       = "Boltz2"
      deployment_name    = "boltz2"
      app                = "boltz2"
      service_name       = "boltz2-svc"
      container_name     = "boltz2"
      image              = "nvcr.io/nim/mit/boltz2"
      version            = "latest"
      command            = local.nim_start_command
      security_context   = local.root_security_context
      resources          = local.resources_1gpu_16cpu_128gi
      shared_memory_size = "64Gi"
      lb_group           = "protein-apps"
      scaling = merge(local.fixed_replica_scaling, {
        fixed_reason = "Structure-prediction backend does not expose a validated per-pod request queue metric for HPA."
      })
      proxy = {
        upstream_name     = "boltz2"
        service_port_name = "boltz2"
      }
    }

    evo2_40b = {
      display_name       = "Evo2-40B"
      deployment_name    = "evo2-40b"
      app                = "evo2-40b"
      service_name       = "evo2-40b-svc"
      container_name     = "evo2-40b"
      image              = "nvcr.io/nim/arc/evo2-40b"
      version            = "latest"
      command            = local.nim_start_command
      security_context   = local.root_security_context
      resources          = local.resources_2gpu_32cpu_256gi
      shared_memory_size = "16Gi"
      lb_group           = "protein-apps"
      scaling = merge(local.fixed_replica_scaling, {
        fixed_reason = "Sequence model backend has not been validated with a stable custom metric for HPA."
      })
      proxy = {
        upstream_name     = "evo2_40b"
        service_port_name = "evo2-40b"
      }
    }

    msa_search = {
      display_name       = "MSA Search"
      deployment_name    = "msa-search"
      app                = "msa-search"
      service_name       = "msa-search-svc"
      container_name     = "evo2-40b"
      image              = "nvcr.io/nim/colabfold/msa-search"
      version            = "latest"
      command            = local.nim_start_command
      security_context   = local.root_security_context
      resources          = local.resources_1gpu_16cpu_128gi
      shared_memory_size = "16Gi"
      lb_group           = "protein-apps"
      scaling = merge(local.fixed_replica_scaling, {
        fixed_reason = "MSA jobs are batch-style requests without a validated request queue metric for HPA."
      })
      proxy = {
        upstream_name     = "msa_search"
        service_port_name = "msa-search"
      }
    }

    openfold2 = {
      display_name       = "OpenFold2"
      deployment_name    = "openfold2"
      app                = "openfold2"
      service_name       = "openfold2-svc"
      container_name     = "openfold2"
      image              = "nvcr.io/nim/openfold/openfold2"
      version            = "latest"
      command            = local.nim_start_command
      security_context   = local.root_security_context
      resources          = local.resources_1gpu_16cpu_128gi
      shared_memory_size = "64Gi"
      lb_group           = "protein-apps"
      scaling = merge(local.fixed_replica_scaling, {
        fixed_reason = "Structure-prediction backend does not expose a validated per-pod request queue metric for HPA."
      })
      proxy = {
        upstream_name     = "openfold2"
        service_port_name = "openfold2"
      }
    }

    genmol = {
      display_name       = "GenMol"
      deployment_name    = "genmol"
      app                = "genmol"
      service_name       = "genmol-svc"
      container_name     = "genmol"
      image              = "nvcr.io/nim/nvidia/genmol"
      version            = "latest"
      command            = local.nim_start_command
      security_context   = local.root_security_context
      resources          = local.resources_1gpu_16cpu_128gi
      shared_memory_size = "16Gi"
      lb_group           = "protein-apps"
      scaling = merge(local.fixed_replica_scaling, {
        fixed_reason = "Generative chemistry backend has not been validated with a stable custom metric for HPA."
      })
      proxy = {
        upstream_name     = "genmol"
        service_port_name = "genmol"
      }
    }

    molmim = {
      display_name       = "MolMIM"
      deployment_name    = "molmim"
      app                = "molmim"
      service_name       = "molmim-svc"
      container_name     = "molmim"
      image              = "nvcr.io/nim/nvidia/molmim"
      version            = "1.0.0"
      resources          = local.resources_1gpu_16cpu_128gi
      shared_memory_size = "16Gi"
      lb_group           = "protein-apps"
      scaling = merge(local.fixed_replica_scaling, {
        fixed_reason = "MolMIM uses its default entrypoint and has not been validated with a stable custom metric for HPA."
      })
      proxy = {
        upstream_name     = "molmim"
        service_port_name = "molmim"
      }
    }

    diffdock = {
      display_name       = "DiffDock"
      deployment_name    = "diffdock"
      app                = "diffdock"
      service_name       = "diffdock-svc"
      container_name     = "diffdock"
      image              = "nvcr.io/nim/mit/diffdock"
      version            = "latest"
      command            = local.nim_start_command
      security_context   = local.root_security_context
      resources          = local.resources_1gpu_16cpu_128gi
      shared_memory_size = "16Gi"
      lb_group           = "protein-apps"
      scaling = merge(local.fixed_replica_scaling, {
        fixed_reason = "Docking backend has not been validated with a stable custom metric for HPA."
      })
      proxy = {
        upstream_name     = "diffdock"
        service_port_name = "diffdock"
      }
    }

    qwen3-next-80b-a3b-instruct = {
      display_name       = "Qwen3 Next 80B A3B Instruct"
      deployment_name    = "qwen3-next-80b-a3b-instruct"
      app                = "qwen3-next-80b-a3b-instruct"
      service_name       = "qwen3-svc"
      container_name     = "qwen3-next-80b-a3b-instruct"
      image              = "nvcr.io/nim/qwen/qwen3-next-80b-a3b-instruct"
      version            = "latest"
      command            = local.nim_start_command
      security_context   = local.root_security_context
      resources          = local.resources_2gpu_32cpu_256gi
      shared_memory_size = "16Gi"
      lb_group           = "protein-apps"
      scaling = merge(local.vllm_request_scaling, {
        max_replicas = 2
      })
      proxy = {
        upstream_name     = "qwen3"
        service_port_name = "qwen3"
      }
    }

    proteinmpnn = {
      display_name       = "ProteinMPNN"
      deployment_name    = "proteinmpnn"
      app                = "proteinmpnn"
      service_name       = "proteinmpnn-svc"
      container_name     = "proteinmpnn"
      image              = "nvcr.io/nim/ipd/proteinmpnn"
      version            = "1.0.2"
      security_context   = local.root_security_context
      resources          = local.resources_1gpu_16cpu_128gi
      shared_memory_size = "16Gi"
      lb_group           = "protein-apps"
      scaling = merge(local.fixed_replica_scaling, {
        fixed_reason = "Protein design backend has not been validated with a stable custom metric for HPA."
      })
      proxy = {
        upstream_name     = "proteinmpnn"
        service_port_name = "proteinmpnn"
      }
    }

    rfdiffusion = {
      display_name       = "RFdiffusion"
      deployment_name    = "rfdiffusion"
      app                = "rfdiffusion"
      service_name       = "rfdiffusion-svc"
      container_name     = "rfdiffusion"
      image              = "nvcr.io/nim/ipd/rfdiffusion"
      version            = "2.2.0"
      security_context   = local.root_security_context
      resources          = local.resources_1gpu_16cpu_128gi
      shared_memory_size = "16Gi"
      lb_group           = "protein-apps"
      scaling = merge(local.fixed_replica_scaling, {
        fixed_reason = "Protein design backend has not been validated with a stable custom metric for HPA."
      })
      proxy = {
        upstream_name     = "rfdiffusion"
        service_port_name = "rfdiffusion"
      }
    }

    cosmos_reason1_7b = {
      display_name       = "Cosmos-Reason1-7B"
      deployment_name    = "cosmos-reason1-7b"
      app                = "cosmos-reason1-7b"
      service_name       = "cosmos-reason1-7b-svc"
      container_name     = "cosmos-reason1-7b"
      image              = "nvcr.io/nim/nvidia/cosmos-reason1-7b"
      version            = "latest"
      security_context   = local.root_security_context
      resources          = local.resources_1gpu_16cpu_128gi
      shared_memory_size = "32Gi"
      lb_group           = "cosmos"
      env = {
        VLLM_MAX_TOTAL_VIDEO_PIXELS = "100000000"
      }
      scaling = local.vllm_request_scaling
      proxy = {
        upstream_name     = "cosmos_reason1_7b"
        service_port_name = "cosmos-reason1-7b"
      }
    }

    cosmos_reason2_8b = {
      display_name       = "Cosmos-Reason2-8B"
      deployment_name    = "cosmos-reason2-8b"
      app                = "cosmos-reason2-8b"
      service_name       = "cosmos-reason2-8b-svc"
      container_name     = "cosmos-reason2-8b"
      image              = "nvcr.io/nim/nvidia/cosmos-reason2-8b"
      version            = "1.6.0"
      security_context   = local.root_security_context
      resources          = local.resources_1gpu_16cpu_128gi
      shared_memory_size = "32Gi"
      lb_group           = "cosmos"
      env = {
        VLLM_MAX_TOTAL_VIDEO_PIXELS = "100000000"
      }
      scaling = local.vllm_request_scaling
      proxy = {
        upstream_name     = "cosmos_reason2_8b"
        service_port_name = "cosmos-reason2-8b"
      }
    }

    cosmos_reason2_2b = {
      display_name       = "Cosmos-Reason2-2B"
      deployment_name    = "cosmos-reason2-2b"
      app                = "cosmos-reason2-2b"
      service_name       = "cosmos-reason2-2b-svc"
      container_name     = "cosmos-reason2-2b"
      image              = "nvcr.io/nim/nvidia/cosmos-reason2-2b"
      version            = "1.6.0"
      security_context   = local.root_security_context
      resources          = local.resources_1gpu_8cpu_64gi
      shared_memory_size = "16Gi"
      lb_group           = "cosmos"
      env = {
        VLLM_MAX_TOTAL_VIDEO_PIXELS = "100000000"
      }
      scaling = local.vllm_request_scaling
      proxy = {
        upstream_name     = "cosmos_reason2_2b"
        service_port_name = "cosmos-reason2-2b"
      }
    }

    cosmos_embed1 = {
      display_name       = "Cosmos-Embed1"
      deployment_name    = "cosmos-embed1"
      app                = "cosmos-embed1"
      service_name       = "cosmos-embed1-svc"
      container_name     = "cosmos-embed1"
      image              = "nvcr.io/nim/nvidia/cosmos-embed1"
      version            = "1.0.0"
      security_context   = local.root_security_context
      resources          = local.resources_1gpu_8cpu_64gi
      shared_memory_size = "16Gi"
      lb_group           = "cosmos"
      env = {
        NVIDIA_DRIVER_CAPABILITIES = "all"
      }
      scaling = merge(local.fixed_replica_scaling, {
        fixed_reason = "Embedding backend request metrics have not been validated for HPA in this module."
      })
      proxy = {
        upstream_name     = "cosmos_embed1"
        service_port_name = "cosmos-embed1"
      }
    }

    nemotron_nano_12b_v2_vl = {
      display_name       = "Nemotron Nano 12B v2 VL"
      deployment_name    = "nemotron-nano-12b-v2-vl"
      app                = "nemotron-nano-12b-v2-vl"
      service_name       = "nemotron-nano-12b-v2-vl-svc"
      container_name     = "nemotron-nano-12b-v2-vl"
      image              = "nvcr.io/nim/nvidia/nemotron-nano-12b-v2-vl"
      version            = "1.6.0"
      security_context   = local.root_security_context
      resources          = local.resources_1gpu_16cpu_128gi
      shared_memory_size = "32Gi"
      lb_group           = "cosmos"
      env = {
        VLLM_MAX_TOTAL_VIDEO_PIXELS = "100000000"
      }
      scaling = local.vllm_request_scaling
      proxy = {
        upstream_name     = "nemotron_nano_12b_v2_vl"
        service_port_name = "nemotron-nano-12b-v2-vl"
      }
    }

    bionemo = {
      kind             = "bionemo_notebook"
      display_name     = "BioNeMo Notebook"
      enabled          = false
      replicas         = 1
      deployment_name  = "bionemo"
      app              = "bionemo-notebook"
      service_name     = "bionemo-svc"
      container_name   = "notebook"
      image            = "nvcr.io/nvidia/clara/bionemo-framework"
      version          = "nightly"
      container_port   = 8888
      service_port     = 8888
      service_type     = "LoadBalancer"
      cache_mount_path = "/workspace/bionemo/"
      cache_sub_path   = "bionemo"
      mount_cache      = true
      command = [
        "jupyter", "lab",
        "--allow-root",
        "--ip=0.0.0.0",
        "--port=8888",
        "--no-browser",
        "--NotebookApp.token=",
        "--NotebookApp.allow_origin=*",
        "--ContentsManager.allow_hidden=True",
        "--notebook-dir=/workspace/bionemo"
      ]
      resources = local.resources_1gpu_16cpu_128gi
      scaling = merge(local.fixed_replica_scaling, {
        fixed_reason = "BioNeMo is a notebook workload, not a NIM /v1/metrics inference endpoint."
      })
      service_monitor = {
        enabled = false
        path    = null
        port    = null
      }
      proxy = {
        enabled           = false
        upstream_name     = null
        service_port_name = null
      }
    }
  }

  model_catalog_keys = setunion(toset(keys(local.default_model_catalog)), toset(keys(var.model_catalog)))

  model_catalog = {
    for name in local.model_catalog_keys : name => merge(
      local.model_defaults,
      try(local.default_model_catalog[name], {}),
      try(var.model_catalog[name], {}),
      {
        resources = {
          limits = merge(
            try(local.model_defaults.resources.limits, {}),
            try(local.default_model_catalog[name].resources.limits, {}),
            try(var.model_catalog[name].resources.limits, {})
          )
          requests = merge(
            try(local.model_defaults.resources.requests, {}),
            try(local.default_model_catalog[name].resources.requests, {}),
            try(var.model_catalog[name].resources.requests, {})
          )
        }
        env = merge(
          try(local.model_defaults.env, {}),
          try(local.default_model_catalog[name].env, {}),
          try(var.model_catalog[name].env, {})
        )
        scaling = merge(
          try(local.model_defaults.scaling, {}),
          try(local.default_model_catalog[name].scaling, {}),
          try(var.model_catalog[name].scaling, {})
        )
        service_monitor = merge(
          try(local.model_defaults.service_monitor, {}),
          try(local.default_model_catalog[name].service_monitor, {}),
          try(var.model_catalog[name].service_monitor, {})
        )
        proxy = merge(
          try(local.model_defaults.proxy, {}),
          try(local.default_model_catalog[name].proxy, {}),
          try(var.model_catalog[name].proxy, {})
        )
      }
    )
  }

  nim_models = {
    for name, model in local.model_catalog : name => model
    if model.kind == "nim"
  }

  bionemo_model = local.model_catalog["bionemo"]
}
