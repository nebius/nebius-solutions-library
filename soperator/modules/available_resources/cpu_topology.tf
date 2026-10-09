locals {
  cpu_topologies = {
    c-2vcpu-8gb = {
      boards            = 1
      sockets_per_board = 1
      cores_per_socket  = 1
      threads_per_core  = 2
      cpus              = 2
    }
    c-4vcpu-16gb = {
      boards            = 1
      sockets_per_board = 1
      cores_per_socket  = 2
      threads_per_core  = 2
      cpus              = 4
    }
    c-8vcpu-32gb = {
      boards            = 1
      sockets_per_board = 1
      cores_per_socket  = 4
      threads_per_core  = 2
      cpus              = 8
    }
    c-16vcpu-64gb = {
      boards            = 1
      sockets_per_board = 1
      cores_per_socket  = 8
      threads_per_core  = 2
      cpus              = 16
    }
    c-32vcpu-128gb = {
      boards            = 1
      sockets_per_board = 1
      cores_per_socket  = 16
      threads_per_core  = 2
      cpus              = 32
    }
    c-48vcpu-192gb-e2 = {
      boards            = 1
      sockets_per_board = 2
      cores_per_socket  = 12
      threads_per_core  = 2
      cpus              = 48
    }
    c-48vcpu-192gb-d3 = {
      boards            = 1
      sockets_per_board = 1
      cores_per_socket  = 24
      threads_per_core  = 2
      cpus              = 48
    }
    c-64vcpu-256gb-e2 = {
      boards            = 1
      sockets_per_board = 2
      cores_per_socket  = 16
      threads_per_core  = 2
      cpus              = 64
    }
    c-64vcpu-256gb-d3 = {
      boards            = 1
      sockets_per_board = 1
      cores_per_socket  = 32
      threads_per_core  = 2
      cpus              = 64
    }
    c-80vcpu-320gb = {
      boards            = 1
      sockets_per_board = 2
      cores_per_socket  = 20
      threads_per_core  = 2
      cpus              = 80
    }
    c-96vcpu-384gb = {
      boards            = 1
      sockets_per_board = 1
      cores_per_socket  = 48
      threads_per_core  = 2
      cpus              = 96
    }
    c-128vcpu-512gb = {
      boards            = 1
      sockets_per_board = 1
      cores_per_socket  = 64
      threads_per_core  = 2
      cpus              = 128
    }
    c-160vcpu-640gb = {
      boards            = 1
      sockets_per_board = 2
      cores_per_socket  = 40
      threads_per_core  = 2
      cpus              = 160
    }
    c-192vcpu-768gb = {
      boards            = 1
      sockets_per_board = 2
      cores_per_socket  = 48
      threads_per_core  = 2
      cpus              = 192
    }
    c-224vcpu-896gb = {
      boards            = 1
      sockets_per_board = 2
      cores_per_socket  = 56
      threads_per_core  = 2
      cpus              = 224
    }
    c-256vcpu-1024gb = {
      boards            = 1
      sockets_per_board = 2
      cores_per_socket  = 64
      threads_per_core  = 2
      cpus              = 256
    }

    g-1gpu-16vcpu-200gb = {
      boards            = 1
      sockets_per_board = 1
      cores_per_socket  = 8
      threads_per_core  = 2
      cpus              = 16
    }
    g-1gpu-20vcpu-224gb = {
      boards            = 1
      sockets_per_board = 1
      cores_per_socket  = 10
      threads_per_core  = 2
      cpus              = 20
    }
    g-1gpu-24vcpu-346gb = {
      boards            = 1
      sockets_per_board = 1
      cores_per_socket  = 12
      threads_per_core  = 2
      cpus              = 24
    }
    g-8gpu-128vcpu-1600gb = {
      boards            = 1
      sockets_per_board = 2
      cores_per_socket  = 32
      threads_per_core  = 2
      cpus              = 128
    }
    g-8gpu-160vcpu-1792gb = {
      boards            = 1
      sockets_per_board = 2
      cores_per_socket  = 40
      threads_per_core  = 2
      cpus              = 160
    }
    g-8gpu-192vcpu-2768gb = {
      boards            = 1
      sockets_per_board = 2
      cores_per_socket  = 48
      threads_per_core  = 2
      cpus              = 192
    }
    g-4gpu-112vcpu-800gb = {
      boards            = 1
      sockets_per_board = 2
      cores_per_socket  = 56
      threads_per_core  = 1
      cpus              = 112
    }
  }

  cpu_topologies_by_platforms = tomap({
    (local.platforms.cpu-e2) = tomap({
      (local.presets.p-2c-8g)    = local.cpu_topologies.c-2vcpu-8gb
      (local.presets.p-4c-16g)   = local.cpu_topologies.c-4vcpu-16gb
      (local.presets.p-8c-32g)   = local.cpu_topologies.c-8vcpu-32gb
      (local.presets.p-16c-64g)  = local.cpu_topologies.c-16vcpu-64gb
      (local.presets.p-32c-128g) = local.cpu_topologies.c-32vcpu-128gb
      (local.presets.p-48c-192g) = local.cpu_topologies.c-48vcpu-192gb-e2
      (local.presets.p-64c-256g) = local.cpu_topologies.c-64vcpu-256gb-e2
      (local.presets.p-80c-320g) = local.cpu_topologies.c-80vcpu-320gb
    })

    (local.platforms.cpu-d3) = tomap({
      (local.presets.p-2c-8g)      = local.cpu_topologies.c-2vcpu-8gb
      (local.presets.p-4c-16g)     = local.cpu_topologies.c-4vcpu-16gb
      (local.presets.p-8c-32g)     = local.cpu_topologies.c-8vcpu-32gb
      (local.presets.p-16c-64g)    = local.cpu_topologies.c-16vcpu-64gb
      (local.presets.p-32c-128g)   = local.cpu_topologies.c-32vcpu-128gb
      (local.presets.p-48c-192g)   = local.cpu_topologies.c-48vcpu-192gb-d3
      (local.presets.p-64c-256g)   = local.cpu_topologies.c-64vcpu-256gb-d3
      (local.presets.p-96c-384g)   = local.cpu_topologies.c-96vcpu-384gb
      (local.presets.p-128c-512g)  = local.cpu_topologies.c-128vcpu-512gb
      (local.presets.p-160c-640g)  = local.cpu_topologies.c-160vcpu-640gb
      (local.presets.p-192c-768g)  = local.cpu_topologies.c-192vcpu-768gb
      (local.presets.p-224c-896g)  = local.cpu_topologies.c-224vcpu-896gb
      (local.presets.p-256c-1024g) = local.cpu_topologies.c-256vcpu-1024gb
    })

    (local.platforms.gpu-h100-sxm) = tomap({
      (local.presets.p-1g-16c-200g)   = local.cpu_topologies.g-1gpu-16vcpu-200gb
      (local.presets.p-8g-128c-1600g) = local.cpu_topologies.g-8gpu-128vcpu-1600gb
    })

    (local.platforms.gpu-h200-sxm) = tomap({
      (local.presets.p-1g-16c-200g)   = local.cpu_topologies.g-1gpu-16vcpu-200gb
      (local.presets.p-8g-128c-1600g) = local.cpu_topologies.g-8gpu-128vcpu-1600gb
    })

    (local.platforms.gpu-b200-sxm) = tomap({
      (local.presets.p-1g-20c-224g)   = local.cpu_topologies.g-1gpu-20vcpu-224gb
      (local.presets.p-8g-160c-1792g) = local.cpu_topologies.g-8gpu-160vcpu-1792gb
    })

    (local.platforms.gpu-b200-sxm-a) = tomap({
      (local.presets.p-1g-20c-224g)   = local.cpu_topologies.g-1gpu-20vcpu-224gb
      (local.presets.p-8g-160c-1792g) = local.cpu_topologies.g-8gpu-160vcpu-1792gb
    })

    (local.platforms.gpu-b300-sxm) = tomap({
      (local.presets.p-1g-24c-346g)   = local.cpu_topologies.g-1gpu-24vcpu-346gb
      (local.presets.p-8g-192c-2768g) = local.cpu_topologies.g-8gpu-192vcpu-2768gb
    })

    (local.platforms.gpu-gb300) = tomap({
      (local.presets.p-4gpu-112vcpu-800g) = local.cpu_topologies.g-4gpu-112vcpu-800gb
    })
  })
}
