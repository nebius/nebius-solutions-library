locals {
  platforms = {
    cpu-e2         = "cpu-e2",
    cpu-d3         = "cpu-d3"
    gpu-h100-sxm   = "gpu-h100-sxm"
    gpu-h200-sxm   = "gpu-h200-sxm"
    gpu-b200-sxm   = "gpu-b200-sxm"
    gpu-b200-sxm-a = "gpu-b200-sxm-a"
    gpu-b300-sxm   = "gpu-b300-sxm"
    gpu-gb300      = "gpu-gb300"
  }

  cpu_platform_by_platform = {
    (local.platforms.cpu-e2)         = "amd64"
    (local.platforms.cpu-d3)         = "amd64"
    (local.platforms.gpu-h100-sxm)   = "amd64"
    (local.platforms.gpu-h200-sxm)   = "amd64"
    (local.platforms.gpu-b200-sxm)   = "amd64"
    (local.platforms.gpu-b200-sxm-a) = "amd64"
    (local.platforms.gpu-b300-sxm)   = "amd64"
    (local.platforms.gpu-gb300)      = "arm64"
  }

  platform_regions = tomap({
    (local.platforms.cpu-e2) = [
      local.regions.eu-north1,
    ]
    (local.platforms.cpu-d3) = [
      local.regions.eu-north1,
      local.regions.eu-north2,
      local.regions.eu-west1,
      local.regions.eu-west2,
      local.regions.me-west1,
      local.regions.uk-south1,
      local.regions.us-central1,
      local.regions.us-north1,
    ]
    (local.platforms.gpu-h100-sxm) = [
      local.regions.eu-north1,
    ]
    (local.platforms.gpu-h200-sxm) = [
      local.regions.eu-north1,
      local.regions.eu-north2,
      local.regions.eu-west1,
    ]
    (local.platforms.gpu-b200-sxm) = [
      local.regions.us-central1,
    ]
    (local.platforms.gpu-b200-sxm-a) = [
      local.regions.me-west1,
    ]
    (local.platforms.gpu-b300-sxm) = [
      local.regions.eu-west2,
      local.regions.uk-south1,
      local.regions.us-north1,
    ]
    (local.platforms.gpu-gb300) = [
      local.regions.eu-north1,
    ]
  })
}
