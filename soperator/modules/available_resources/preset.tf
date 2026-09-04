locals {
  presets = {
    p-2c-8g      = "2vcpu-8gb"
    p-4c-16g     = "4vcpu-16gb"
    p-8c-32g     = "8vcpu-32gb"
    p-16c-64g    = "16vcpu-64gb"
    p-32c-128g   = "32vcpu-128gb"
    p-48c-192g   = "48vcpu-192gb"
    p-64c-256g   = "64vcpu-256gb"
    p-80c-320g   = "80vcpu-320gb"
    p-96c-384g   = "96vcpu-384gb"
    p-128c-512g  = "128vcpu-512gb"
    p-160c-640g  = "160vcpu-640gb"
    p-192c-768g  = "192vcpu-768gb"
    p-224c-896g  = "224vcpu-896gb"
    p-256c-1024g = "256vcpu-1024gb"

    p-1g-16c-200g   = "1gpu-16vcpu-200gb"
    p-1g-20c-224g   = "1gpu-20vcpu-224gb"
    p-1g-24c-346g   = "1gpu-24vcpu-346gb"
    p-8g-128c-1600g = "8gpu-128vcpu-1600gb"
    p-8g-160c-1792g = "8gpu-160vcpu-1792gb"
    p-8g-192c-2768g = "8gpu-192vcpu-2768gb"

    p-4gpu-112vcpu-800g = "4gpu-112vcpu-800gb"
  }

  presets_cpu = {
    c-2vcpu-8gb = {
      cpu_cores_raw          = 2
      cpu_cores              = 2 * local.reserve.cpu.coefficient - local.reserve.cpu.count
      memory_gibibytes_raw   = 8
      memory_gibibytes       = 8 * local.reserve.ram.coefficient - local.reserve.ram.count
      gpus                   = 0
      gpu_cluster_compatible = false
      sufficient = {
        (module.labels.name_nodeset_system)     = false
        (module.labels.name_nodeset_controller) = true
        (module.labels.name_nodeset_worker)     = false
        (module.labels.name_nodeset_login)      = false
        (module.labels.name_nodeset_accounting) = false
        (module.labels.name_nodeset_nfs)        = true
        weka                                    = false
      }
    }
    c-4vcpu-16gb = {
      cpu_cores_raw          = 4
      cpu_cores              = 4 * local.reserve.cpu.coefficient - local.reserve.cpu.count
      memory_gibibytes_raw   = 16
      memory_gibibytes       = 16 * local.reserve.ram.coefficient - local.reserve.ram.count
      gpus                   = 0
      gpu_cluster_compatible = false
      sufficient = {
        (module.labels.name_nodeset_system)     = false
        (module.labels.name_nodeset_controller) = true
        (module.labels.name_nodeset_worker)     = false
        (module.labels.name_nodeset_login)      = false
        (module.labels.name_nodeset_accounting) = true
        (module.labels.name_nodeset_nfs)        = true
        weka                                    = false
      }
    }
    c-8vcpu-32gb = {
      cpu_cores_raw          = 8
      cpu_cores              = 8 * local.reserve.cpu.coefficient - local.reserve.cpu.count
      memory_gibibytes_raw   = 32
      memory_gibibytes       = 32 * local.reserve.ram.coefficient - local.reserve.ram.count
      gpus                   = 0
      gpu_cluster_compatible = false
      sufficient = {
        (module.labels.name_nodeset_system)     = true
        (module.labels.name_nodeset_controller) = true
        (module.labels.name_nodeset_worker)     = true
        (module.labels.name_nodeset_login)      = false
        (module.labels.name_nodeset_accounting) = true
        (module.labels.name_nodeset_nfs)        = true
        weka                                    = false
      }
    }
    c-16vcpu-64gb = {
      cpu_cores_raw          = 16
      cpu_cores              = 16 * local.reserve.cpu.coefficient - local.reserve.cpu.count
      memory_gibibytes_raw   = 64
      memory_gibibytes       = 64 * local.reserve.ram.coefficient - local.reserve.ram.count
      gpus                   = 0
      gpu_cluster_compatible = false
      sufficient = {
        (module.labels.name_nodeset_system)     = true
        (module.labels.name_nodeset_controller) = true
        (module.labels.name_nodeset_worker)     = true
        (module.labels.name_nodeset_login)      = true
        (module.labels.name_nodeset_accounting) = true
        (module.labels.name_nodeset_nfs)        = true
        weka                                    = false
      }
    }
    c-32vcpu-128gb = {
      cpu_cores_raw          = 32
      cpu_cores              = 32 * local.reserve.cpu.coefficient - local.reserve.cpu.count
      memory_gibibytes_raw   = 128
      memory_gibibytes       = 128 * local.reserve.ram.coefficient - local.reserve.ram.count
      gpus                   = 0
      gpu_cluster_compatible = false
      sufficient = {
        (module.labels.name_nodeset_system)     = true
        (module.labels.name_nodeset_controller) = true
        (module.labels.name_nodeset_worker)     = true
        (module.labels.name_nodeset_login)      = true
        (module.labels.name_nodeset_accounting) = true
        (module.labels.name_nodeset_nfs)        = true
        weka                                    = true
      }
    }
    c-48vcpu-192gb = {
      cpu_cores_raw          = 48
      cpu_cores              = 48 * local.reserve.cpu.coefficient - local.reserve.cpu.count
      memory_gibibytes_raw   = 192
      memory_gibibytes       = 192 * local.reserve.ram.coefficient - local.reserve.ram.count
      gpus                   = 0
      gpu_cluster_compatible = false
      sufficient = {
        (module.labels.name_nodeset_system)     = true
        (module.labels.name_nodeset_controller) = true
        (module.labels.name_nodeset_worker)     = true
        (module.labels.name_nodeset_login)      = true
        (module.labels.name_nodeset_accounting) = true
        (module.labels.name_nodeset_nfs)        = true
        weka                                    = true
      }
    }
    c-64vcpu-256gb = {
      cpu_cores_raw          = 64
      cpu_cores              = 64 * local.reserve.cpu.coefficient - local.reserve.cpu.count
      memory_gibibytes_raw   = 256
      memory_gibibytes       = 256 * local.reserve.ram.coefficient - local.reserve.ram.count
      gpus                   = 0
      gpu_cluster_compatible = false
      sufficient = {
        (module.labels.name_nodeset_system)     = true
        (module.labels.name_nodeset_controller) = true
        (module.labels.name_nodeset_worker)     = true
        (module.labels.name_nodeset_login)      = true
        (module.labels.name_nodeset_accounting) = true
        (module.labels.name_nodeset_nfs)        = true
        weka                                    = true
      }
    }
    c-80vcpu-320gb = {
      cpu_cores_raw          = 80
      cpu_cores              = 80 * local.reserve.cpu.coefficient - local.reserve.cpu.count
      memory_gibibytes_raw   = 320
      memory_gibibytes       = 320 * local.reserve.ram.coefficient - local.reserve.ram.count
      gpus                   = 0
      gpu_cluster_compatible = false
      sufficient = {
        (module.labels.name_nodeset_system)     = true
        (module.labels.name_nodeset_controller) = true
        (module.labels.name_nodeset_worker)     = true
        (module.labels.name_nodeset_login)      = true
        (module.labels.name_nodeset_accounting) = true
        (module.labels.name_nodeset_nfs)        = true
        weka                                    = true
      }
    }
    c-96vcpu-384gb = {
      cpu_cores_raw          = 96
      cpu_cores              = 96 * local.reserve.cpu.coefficient - local.reserve.cpu.count
      memory_gibibytes_raw   = 384
      memory_gibibytes       = 384 * local.reserve.ram.coefficient - local.reserve.ram.count
      gpus                   = 0
      gpu_cluster_compatible = false
      sufficient = {
        (module.labels.name_nodeset_system)     = true
        (module.labels.name_nodeset_controller) = true
        (module.labels.name_nodeset_worker)     = true
        (module.labels.name_nodeset_login)      = true
        (module.labels.name_nodeset_accounting) = true
        (module.labels.name_nodeset_nfs)        = true
        weka                                    = true
      }
    }
    c-128vcpu-512gb = {
      cpu_cores_raw          = 128
      cpu_cores              = 128 * local.reserve.cpu.coefficient - local.reserve.cpu.count
      memory_gibibytes_raw   = 512
      memory_gibibytes       = 512 * local.reserve.ram.coefficient - local.reserve.ram.count
      gpus                   = 0
      gpu_cluster_compatible = false
      sufficient = {
        (module.labels.name_nodeset_system)     = true
        (module.labels.name_nodeset_controller) = true
        (module.labels.name_nodeset_worker)     = true
        (module.labels.name_nodeset_login)      = true
        (module.labels.name_nodeset_accounting) = true
        (module.labels.name_nodeset_nfs)        = true
        weka                                    = true
      }
    }
    c-160vcpu-640gb = {
      cpu_cores_raw          = 160
      cpu_cores              = 160 * local.reserve.cpu.coefficient - local.reserve.cpu.count
      memory_gibibytes_raw   = 640
      memory_gibibytes       = 640 * local.reserve.ram.coefficient - local.reserve.ram.count
      gpus                   = 0
      gpu_cluster_compatible = false
      sufficient = {
        (module.labels.name_nodeset_system)     = true
        (module.labels.name_nodeset_controller) = true
        (module.labels.name_nodeset_worker)     = true
        (module.labels.name_nodeset_login)      = true
        (module.labels.name_nodeset_accounting) = true
        (module.labels.name_nodeset_nfs)        = true
        weka                                    = true
      }
    }
    c-192vcpu-768gb = {
      cpu_cores_raw          = 192
      cpu_cores              = 192 * local.reserve.cpu.coefficient - local.reserve.cpu.count
      memory_gibibytes_raw   = 768
      memory_gibibytes       = 768 * local.reserve.ram.coefficient - local.reserve.ram.count
      gpus                   = 0
      gpu_cluster_compatible = false
      sufficient = {
        (module.labels.name_nodeset_system)     = true
        (module.labels.name_nodeset_controller) = true
        (module.labels.name_nodeset_worker)     = true
        (module.labels.name_nodeset_login)      = true
        (module.labels.name_nodeset_accounting) = true
        (module.labels.name_nodeset_nfs)        = true
        weka                                    = true
      }
    }
    c-224vcpu-896gb = {
      cpu_cores_raw          = 224
      cpu_cores              = 224 * local.reserve.cpu.coefficient - local.reserve.cpu.count
      memory_gibibytes_raw   = 896
      memory_gibibytes       = 896 * local.reserve.ram.coefficient - local.reserve.ram.count
      gpus                   = 0
      gpu_cluster_compatible = false
      sufficient = {
        (module.labels.name_nodeset_system)     = true
        (module.labels.name_nodeset_controller) = true
        (module.labels.name_nodeset_worker)     = true
        (module.labels.name_nodeset_login)      = true
        (module.labels.name_nodeset_accounting) = true
        (module.labels.name_nodeset_nfs)        = true
        weka                                    = true
      }
    }
    c-256vcpu-1024gb = {
      cpu_cores_raw          = 256
      cpu_cores              = 256 * local.reserve.cpu.coefficient - local.reserve.cpu.count
      memory_gibibytes_raw   = 1024
      memory_gibibytes       = 1024 * local.reserve.ram.coefficient - local.reserve.ram.count
      gpus                   = 0
      gpu_cluster_compatible = false
      sufficient = {
        (module.labels.name_nodeset_system)     = true
        (module.labels.name_nodeset_controller) = true
        (module.labels.name_nodeset_worker)     = true
        (module.labels.name_nodeset_login)      = true
        (module.labels.name_nodeset_accounting) = true
        (module.labels.name_nodeset_nfs)        = true
        weka                                    = true
      }
    }
  }

  presets_gpu = {
    g-1gpu-16vcpu-200gb = {
      cpu_cores_raw          = 16
      cpu_cores              = 16 * local.reserve.cpu.coefficient - local.reserve.cpu.count
      memory_gibibytes_raw   = 200
      memory_gibibytes       = 200 * local.reserve.ram.coefficient - local.reserve.ram.count
      gpus                   = 1
      gpu_cluster_compatible = false
      sufficient = {
        (module.labels.name_nodeset_system)     = true
        (module.labels.name_nodeset_controller) = true
        (module.labels.name_nodeset_worker)     = true
        (module.labels.name_nodeset_login)      = true
        (module.labels.name_nodeset_accounting) = true
        (module.labels.name_nodeset_nfs)        = true
        weka                                    = false
      }
    }
    g-8gpu-128vcpu-1600gb = {
      cpu_cores_raw          = 128
      cpu_cores              = 128 * local.reserve.cpu.coefficient - local.reserve.cpu.count
      memory_gibibytes_raw   = 1600
      memory_gibibytes       = 1600 * local.reserve.ram.coefficient - local.reserve.ram.count
      gpus                   = 8
      gpu_cluster_compatible = true
      sufficient = {
        (module.labels.name_nodeset_system)     = true
        (module.labels.name_nodeset_controller) = true
        (module.labels.name_nodeset_worker)     = true
        (module.labels.name_nodeset_login)      = true
        (module.labels.name_nodeset_accounting) = true
        (module.labels.name_nodeset_nfs)        = true
        weka                                    = true
      }
    }
    g-1gpu-20vcpu-224gb = {
      cpu_cores_raw          = 20
      cpu_cores              = 20 * local.reserve.cpu.coefficient - local.reserve.cpu.count
      memory_gibibytes_raw   = 224
      memory_gibibytes       = 224 * local.reserve.ram.coefficient - local.reserve.ram.count
      gpus                   = 1
      gpu_cluster_compatible = false
      sufficient = {
        (module.labels.name_nodeset_system)     = true
        (module.labels.name_nodeset_controller) = true
        (module.labels.name_nodeset_worker)     = true
        (module.labels.name_nodeset_login)      = true
        (module.labels.name_nodeset_accounting) = true
        (module.labels.name_nodeset_nfs)        = true
        weka                                    = false
      }
    }
    g-8gpu-160vcpu-1792gb = {
      cpu_cores_raw          = 160
      cpu_cores              = 160 * local.reserve.cpu.coefficient - local.reserve.cpu.count
      memory_gibibytes_raw   = 1792
      memory_gibibytes       = 1792 * local.reserve.ram.coefficient - local.reserve.ram.count
      gpus                   = 8
      gpu_cluster_compatible = true
      sufficient = {
        (module.labels.name_nodeset_system)     = true
        (module.labels.name_nodeset_controller) = true
        (module.labels.name_nodeset_worker)     = true
        (module.labels.name_nodeset_login)      = true
        (module.labels.name_nodeset_accounting) = true
        (module.labels.name_nodeset_nfs)        = true
        weka                                    = true
      }
    }
    g-1gpu-24vcpu-346gb = {
      cpu_cores_raw          = 24
      cpu_cores              = 24 * local.reserve.cpu.coefficient - local.reserve.cpu.count
      memory_gibibytes_raw   = 346
      memory_gibibytes       = 346 * local.reserve.ram.coefficient - local.reserve.ram.count
      gpus                   = 1
      gpu_cluster_compatible = false
      sufficient = {
        (module.labels.name_nodeset_system)     = true
        (module.labels.name_nodeset_controller) = true
        (module.labels.name_nodeset_worker)     = true
        (module.labels.name_nodeset_login)      = true
        (module.labels.name_nodeset_accounting) = true
        (module.labels.name_nodeset_nfs)        = true
        weka                                    = false
      }
    }
    g-8gpu-192vcpu-2768gb = {
      cpu_cores_raw          = 192
      cpu_cores              = 192 * local.reserve.cpu.coefficient - local.reserve.cpu.count
      memory_gibibytes_raw   = 2768
      memory_gibibytes       = 2768 * local.reserve.ram.coefficient - local.reserve.ram.count
      gpus                   = 8
      gpu_cluster_compatible = true
      sufficient = {
        (module.labels.name_nodeset_system)     = true
        (module.labels.name_nodeset_controller) = true
        (module.labels.name_nodeset_worker)     = true
        (module.labels.name_nodeset_login)      = true
        (module.labels.name_nodeset_accounting) = true
        (module.labels.name_nodeset_nfs)        = true
        weka                                    = true
      }
    }
    g-4gpu-112vcpu-800gb = {
      cpu_cores_raw          = 112
      cpu_cores              = 112 * local.reserve.cpu.coefficient - local.reserve.cpu.count
      memory_gibibytes_raw   = 800
      memory_gibibytes       = 800 * local.reserve.ram.coefficient - local.reserve.ram.count
      gpus                   = 4
      gpu_cluster_compatible = true
      sufficient = {
        (module.labels.name_nodeset_system)     = false
        (module.labels.name_nodeset_controller) = false
        (module.labels.name_nodeset_worker)     = true
        (module.labels.name_nodeset_login)      = true
        (module.labels.name_nodeset_accounting) = false
        (module.labels.name_nodeset_nfs)        = false
        weka                                    = true
      }
    }
  }

  # Allow-list patterns: "${region}/${platform}/${preset}".
  # Use "*" in any section to match every value for that section.
  local_nvme_supported_true_region_platform_preset = toset([
    # gpu-b300-sxm
    "${local.regions.uk-south1}/${local.platforms.gpu-b300-sxm}/${local.presets.p-8g-192c-2768g}",
    # gpu-gb300 in all regions and presets
    "*/${local.platforms.gpu-gb300}/*",
  ])

  presets_by_platforms_raw = tomap({
    (local.platforms.cpu-e2) = tomap({
      (local.presets.p-2c-8g)    = local.presets_cpu.c-2vcpu-8gb
      (local.presets.p-4c-16g)   = local.presets_cpu.c-4vcpu-16gb
      (local.presets.p-8c-32g)   = local.presets_cpu.c-8vcpu-32gb
      (local.presets.p-16c-64g)  = local.presets_cpu.c-16vcpu-64gb
      (local.presets.p-32c-128g) = local.presets_cpu.c-32vcpu-128gb
      (local.presets.p-48c-192g) = local.presets_cpu.c-48vcpu-192gb
      (local.presets.p-64c-256g) = local.presets_cpu.c-64vcpu-256gb
      (local.presets.p-80c-320g) = local.presets_cpu.c-80vcpu-320gb
    })

    (local.platforms.cpu-d3) = tomap({
      (local.presets.p-2c-8g)      = local.presets_cpu.c-2vcpu-8gb
      (local.presets.p-4c-16g)     = local.presets_cpu.c-4vcpu-16gb
      (local.presets.p-8c-32g)     = local.presets_cpu.c-8vcpu-32gb
      (local.presets.p-16c-64g)    = local.presets_cpu.c-16vcpu-64gb
      (local.presets.p-32c-128g)   = local.presets_cpu.c-32vcpu-128gb
      (local.presets.p-48c-192g)   = local.presets_cpu.c-48vcpu-192gb
      (local.presets.p-64c-256g)   = local.presets_cpu.c-64vcpu-256gb
      (local.presets.p-96c-384g)   = local.presets_cpu.c-96vcpu-384gb
      (local.presets.p-128c-512g)  = local.presets_cpu.c-128vcpu-512gb
      (local.presets.p-160c-640g)  = local.presets_cpu.c-160vcpu-640gb
      (local.presets.p-192c-768g)  = local.presets_cpu.c-192vcpu-768gb
      (local.presets.p-224c-896g)  = local.presets_cpu.c-224vcpu-896gb
      (local.presets.p-256c-1024g) = local.presets_cpu.c-256vcpu-1024gb
    })

    (local.platforms.gpu-h100-sxm) = tomap({
      (local.presets.p-1g-16c-200g)   = local.presets_gpu.g-1gpu-16vcpu-200gb
      (local.presets.p-8g-128c-1600g) = local.presets_gpu.g-8gpu-128vcpu-1600gb
    })

    (local.platforms.gpu-h200-sxm) = tomap({
      (local.presets.p-1g-16c-200g)   = local.presets_gpu.g-1gpu-16vcpu-200gb
      (local.presets.p-8g-128c-1600g) = local.presets_gpu.g-8gpu-128vcpu-1600gb
    })

    (local.platforms.gpu-b200-sxm) = tomap({
      (local.presets.p-1g-20c-224g)   = local.presets_gpu.g-1gpu-20vcpu-224gb
      (local.presets.p-8g-160c-1792g) = local.presets_gpu.g-8gpu-160vcpu-1792gb
    })

    (local.platforms.gpu-b200-sxm-a) = tomap({
      (local.presets.p-1g-20c-224g)   = local.presets_gpu.g-1gpu-20vcpu-224gb
      (local.presets.p-8g-160c-1792g) = local.presets_gpu.g-8gpu-160vcpu-1792gb
    })

    (local.platforms.gpu-b300-sxm) = tomap({
      (local.presets.p-1g-24c-346g)   = local.presets_gpu.g-1gpu-24vcpu-346gb
      (local.presets.p-8g-192c-2768g) = local.presets_gpu.g-8gpu-192vcpu-2768gb
    })

    (local.platforms.gpu-gb300) = tomap({
      (local.presets.p-4gpu-112vcpu-800g) = local.presets_gpu.g-4gpu-112vcpu-800gb
    })
  })

  local_nvme_supported_by_region_platform_preset = tomap({
    for region in [for _, region in local.regions : region] : region => tomap({
      for platform, presets in local.presets_by_platforms_raw : platform => tomap({
        for preset, _ in presets : preset => anytrue([
          for candidate in setproduct(
            [region, "*"],
            [platform, "*"],
            [preset, "*"],
          ) : contains(local.local_nvme_supported_true_region_platform_preset, join("/", candidate))
        ])
      })
    })
  })

  presets_by_platforms = tomap({
    for platform, presets in local.presets_by_platforms_raw : platform => tomap({
      for preset, resources in presets : preset => merge(resources, {
        cpu_platform = local.cpu_platform_by_platform[platform]
        local_nvme_supported = anytrue([
          for region in [for _, region in local.regions : region] : try(local.local_nvme_supported_by_region_platform_preset[region][platform][preset], false)
        ])
        local_nvme_supported_by_region = tomap({
          for region in [for _, region in local.regions : region] : region => try(local.local_nvme_supported_by_region_platform_preset[region][platform][preset], false)
        })
      })
    })
  })
}
