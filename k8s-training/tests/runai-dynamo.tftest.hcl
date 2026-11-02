# Infrastructure plans use the real Nebius schema without credentials or preflights.
# Run:ai module checks explicitly use only mocked providers.
provider "nebius" {
  no_credentials           = true
  disable_preflight_checks = true
}
mock_provider "nebius" { alias = "native" }
mock_provider "helm" {}
mock_provider "kubernetes" {}
mock_provider "kubectl" {}
mock_provider "random" {}
mock_provider "http" {}
mock_provider "time" {}
mock_provider "units" {}

variables {
  tenant_id                  = "tenant-test-only"
  parent_id                  = "project-test-only"
  subnet_id                  = "vpcsubnet-test-only"
  region                     = "test-region"
  cpu_nodes_platform         = "cpu-d3"
  cpu_nodes_preset           = "8vcpu-32gb"
  gpu_nodes_platform         = "gpu-h100-sxm"
  gpu_nodes_preset           = "1gpu-16vcpu-200gb"
  iam_token                  = "test-only"
  k8s_version                = "1.35"
  ssh_public_key             = { key = "ssh-ed25519 test-only" }
  infiniband_fabric          = ""
  gpu_nodes_driverfull_image = false
  loki                       = { enabled = false }
}

run "mixed_pools_with_managed_driver" {
  command = plan
  variables {
    cpu_nodes_fixed_count    = 3
    cpu_nodes_platform       = "cpu-d3"
    cpu_nodes_preset         = "8vcpu-32gb"
    cpu_disk_size            = 512
    enable_k8s_node_group_sa = false
    gpu_node_groups          = 2
    gpu_nodes_platform       = "gpu-rtx6000-a"
    gpu_nodes_preset         = "1gpu-24vcpu-218gb"
    gpu_disk_size            = 512
    gpu_nodes_autoscaling    = { enabled = true, min_size = 0, max_size = 2 }
    gpu_node_group_overrides = [
      { labels = { "runai.nebius/pool" = "serving" }, taints = [{ key = "inference", value = "true", effect = "NO_SCHEDULE" }] },
      { labels = { "runai.nebius/pool" = "batch" }, preemptible = true },
    ]
    gpu_nodes_driverfull_image = true
    custom_driver              = false
  }
  plan_options {
    target = [nebius_mk8s_v1_cluster.k8s-cluster, nebius_mk8s_v1_node_group.cpu-only,
    nebius_mk8s_v1_node_group.gpu, module.gpu-operator, module.gpu-operator-custom, module.network-operator]
  }
  assert {
    condition     = nebius_mk8s_v1_node_group.cpu-only.fixed_node_count == 3 && nebius_mk8s_v1_node_group.cpu-only.template.boot_disk.size_gibibytes == 512
    error_message = "The CPU group must retain its three 512 GiB workers."
  }
  assert {
    condition     = nebius_mk8s_v1_node_group.gpu[0].template.preemptible == null && nebius_mk8s_v1_node_group.gpu[1].template.preemptible != null && nebius_mk8s_v1_node_group.gpu[1].template.follows_spot_price != null && nebius_mk8s_v1_node_group.gpu[0].template.metadata.labels["runai.nebius/pool"] == "serving" && nebius_mk8s_v1_node_group.gpu[0].template.taints[0].effect == "NO_SCHEDULE" && nebius_mk8s_v1_node_group.gpu[1].template.taints == null
    error_message = "Regular and Spot pools must retain their own labels and taints, with native Spot-price following."
  }
  assert {
    condition     = local.use_driverfull_gpu && length(module.gpu-operator-custom) == 0 && alltrue([for pool in nebius_mk8s_v1_node_group.gpu : pool.autoscaling.min_node_count == 0 && pool.autoscaling.max_node_count == 2 && pool.template.gpu_settings.drivers_preset == "cuda13.0"])
    error_message = "GPU pools must scale from zero and retain provider-owned native CUDA 13 drivers."
  }
}

run "regional_mixed_accelerators" {
  command = plan
  variables {
    gpu_node_groups = 2
    gpu_node_group_overrides = [
      { platform = "gpu-l40s-a", preset = "1gpu-24vcpu-96gb" },
      { platform = "gpu-h100-sxm", preset = "1gpu-16vcpu-200gb", preemptible = true },
    ]
  }
  plan_options { target = [nebius_mk8s_v1_node_group.gpu] }
  assert {
    condition     = nebius_mk8s_v1_node_group.gpu[0].template.resources.platform == "gpu-l40s-a" && nebius_mk8s_v1_node_group.gpu[1].template.resources.platform == "gpu-h100-sxm"
    error_message = "A region must support different accelerator SKUs without a second cluster implementation."
  }
  assert {
    condition     = nebius_mk8s_v1_node_group.gpu[0].template.resources.preset == "1gpu-24vcpu-96gb" && nebius_mk8s_v1_node_group.gpu[1].template.resources.preset == "1gpu-16vcpu-200gb"
    error_message = "Each GPU pool must retain its own preset."
  }
}

run "reserved_and_spot_gpu_pools" {
  command = plan
  variables {
    region                     = "eu-north1"
    gpu_nodes_driverfull_image = true
    gpu_nodes_platform         = "gpu-h100-sxm"
    gpu_nodes_preset           = "8gpu-128vcpu-1600gb"
    gpu_node_groups            = 3
    node_group_strategy = {
      max_surge       = { count = 1 }
      max_unavailable = { count = 0 }
    }
    gpu_nodes_reservation_policy = {
      policy          = "STRICT"
      reservation_ids = ["capacityblockgroup-test-only"]
    }
    gpu_node_group_overrides = [
      { fixed_count = 1, disk_size = 1024 },
      { preset = "1gpu-16vcpu-200gb", preemptible = true, disk_size = 512, autoscaling = { enabled = true, min_size = 0, max_size = 8 } },
      { platform = "gpu-l40s-a", preset = "1gpu-24vcpu-96gb", reservation_policy = { policy = "FORBID" }, disk_size = 256, disk_type = "NETWORK_SSD_NON_REPLICATED", autoscaling = { enabled = true, min_size = 0, max_size = 4 } },
    ]
  }
  plan_options { target = [nebius_mk8s_v1_node_group.gpu] }
  assert {
    condition     = alltrue([for pool in nebius_mk8s_v1_node_group.gpu : pool.strategy.max_surge.count == 1 && pool.strategy.max_unavailable.count == 0])
    error_message = "GPU pools must inherit the existing global rollout strategy."
  }
  assert {
    condition     = nebius_mk8s_v1_node_group.gpu[0].template.reservation_policy.policy == "STRICT" && nebius_mk8s_v1_node_group.gpu[0].template.reservation_policy.reservation_ids[0] == "capacityblockgroup-test-only" && nebius_mk8s_v1_node_group.gpu[1].template.reservation_policy == null && nebius_mk8s_v1_node_group.gpu[1].template.follows_spot_price != null && nebius_mk8s_v1_node_group.gpu[2].template.reservation_policy.policy == "FORBID"
    error_message = "Reserved capacity must stay on its regular pool; a global reservation must never leak into Spot or override another regular pool's policy."
  }
  assert {
    condition     = nebius_mk8s_v1_node_group.gpu[0].fixed_node_count == 1 && nebius_mk8s_v1_node_group.gpu[1].autoscaling.max_node_count == 8 && nebius_mk8s_v1_node_group.gpu[2].autoscaling.max_node_count == 4 && nebius_mk8s_v1_node_group.gpu[0].template.resources.preset != nebius_mk8s_v1_node_group.gpu[1].template.resources.preset && nebius_mk8s_v1_node_group.gpu[2].template.boot_disk.size_gibibytes == 256 && nebius_mk8s_v1_node_group.gpu[2].template.boot_disk.type == "NETWORK_SSD_NON_REPLICATED"
    error_message = "Different H100 sizes and L40S must preserve their independent counts, scaling and boot disks at existing GPU indices."
  }
}

run "reject_explicit_spot_reservation" {
  command = plan
  variables {
    gpu_nodes_preemptible    = true
    gpu_node_group_overrides = [{ reservation_policy = { policy = "FORBID" } }]
  }
  plan_options { target = [nebius_mk8s_v1_node_group.gpu] }
  expect_failures = [var.gpu_node_group_overrides]
}

run "native_runai_driver_ownership" {
  command = plan
  variables {
    gpu_nodes_driverfull_image = true
    gpu_operator_values        = file("tests/gpu-operator-native-values.yaml")
    enable_runai               = true
    runai = {
      role                 = "worker"
      domain               = "worker.example.com"
      control_plane_url    = "https://runai.example.com"
      cluster_uid          = "test-cluster"
      bootstrap_token_file = "provider.tf"
    }
  }
  plan_options { target = [module.gpu-operator, module.device-plugin, module.runai, nebius_mk8s_v1_node_group.gpu] }
  assert {
    condition     = local.use_driverfull_gpu && length(module.gpu-operator) == 0 && length(module.device-plugin) == 1 && alltrue([for pool in nebius_mk8s_v1_node_group.gpu : pool.template.gpu_settings.drivers_preset == "cuda13.0"])
    error_message = "Native Run:ai workers must retain Nebius GPU ownership and the existing Marketplace discovery package, without GPU Operator."
  }
}

run "gpu_marketplace_values_revision" {
  command = plan
  module { source = "../modules/gpu-operator" }
  variables {
    cluster_id           = "mk8scluster-test-only"
    parent_id            = "project-test-only"
    enable_dcgm_exporter = true
    values               = file("tests/gpu-operator-native-values.yaml")
  }
  assert {
    condition     = nebius_applications_v1alpha1_k8s_release.this.product_slug == "nebius/nvidia-gpu-operator" && nebius_applications_v1alpha1_k8s_release.this.application_name == "gpu-operator" && nebius_applications_v1alpha1_k8s_release.this.namespace == "gpu-operator"
    error_message = "Custom values must retain the original Marketplace application and namespace."
  }
  assert {
    condition     = nebius_applications_v1alpha1_k8s_release.this.sensitive.version != null && length(nebius_applications_v1alpha1_k8s_release.this.sensitive.version) == 64 && nebius_applications_v1alpha1_k8s_release.this.sensitive.set == null && nebius_applications_v1alpha1_k8s_release.this.sensitive.values == null
    error_message = "Marketplace configuration must expose a change revision while keeping its write-only settings out of planned state."
  }
}

run "mixed_fabric_and_ethernet_pools" {
  command = plan
  variables {
    gpu_nodes_driverfull_image = true
    gpu_node_groups            = 3
    infiniband_fabric          = "test-fabric"
    gpu_node_group_overrides = [
      { platform = "gpu-h200-sxm", preset = "8gpu-128vcpu-1600gb" },
      { platform = "gpu-rtx6000-a", preset = "1gpu-24vcpu-218gb", attach_gpu_cluster = false },
      { platform = "gpu-h200-sxm", preset = "1gpu-16vcpu-200gb", attach_gpu_cluster = false },
    ]
  }
  plan_options { target = [nebius_mk8s_v1_node_group.gpu] }
  assert {
    condition     = nebius_mk8s_v1_node_group.gpu[1].template.gpu_cluster == null && nebius_mk8s_v1_node_group.gpu[2].template.gpu_cluster == null && length(nebius_compute_v1_gpu_cluster.fabric_2) == 1
    error_message = "Ethernet pools must not inherit the InfiniBand cluster."
  }
  assert {
    condition     = nebius_mk8s_v1_node_group.gpu[0].template.gpu_settings.drivers_preset == "cuda13.0" && nebius_mk8s_v1_node_group.gpu[0].template.resources.preset != nebius_mk8s_v1_node_group.gpu[2].template.resources.preset
    error_message = "Managed image settings and different presets of the same GPU must be preserved."
  }
}


run "unknown_region_uses_explicit_shapes" {
  command = plan
  variables {
    region             = "unlisted-region"
    infiniband_fabric  = null
    cpu_nodes_platform = "cpu-custom"
    cpu_nodes_preset   = "cpu-custom-shape"
    gpu_nodes_platform = "gpu-custom"
    gpu_nodes_preset   = "1gpu-custom-shape"
  }
  plan_options { target = [nebius_mk8s_v1_node_group.cpu-only, nebius_mk8s_v1_node_group.gpu] }
  assert {
    condition = (
      nebius_mk8s_v1_node_group.cpu-only.template.resources.platform == "cpu-custom" &&
      nebius_mk8s_v1_node_group.cpu-only.template.resources.preset == "cpu-custom-shape" &&
      nebius_mk8s_v1_node_group.gpu[0].template.resources.platform == "gpu-custom" &&
      nebius_mk8s_v1_node_group.gpu[0].template.resources.preset == "1gpu-custom-shape" &&
      nebius_mk8s_v1_node_group.gpu[0].template.gpu_cluster == null &&
      length(nebius_compute_v1_gpu_cluster.fabric_2) == 0
    )
    error_message = "Explicit shapes must support a region outside the inherited defaults without substituting GPU types or attaching a fabric."
  }
}

run "runai_control_plane_composition" {
  command = plan
  module { source = "../modules/runai" }
  variables {
    parent_id         = "project-test-only"
    cluster_id        = "mk8scluster-test-only"
    region            = "test-region"
    network_id        = "vpcnetwork-test-only"
    admin_password    = "test-only-admin-password"
    database_password = "test-only-database-password"
    config            = { role = "control-plane", public_endpoint = false, domain = "runai.example.com" }
  }
  assert {
    condition     = nebius_applications_v1alpha1_k8s_release.control_plane[0].product_slug == "nebius/nvidia-run-ai" && nebius_msp_postgresql_v1alpha1_cluster.this[0].network_id == "vpcnetwork-test-only" && !nebius_msp_postgresql_v1alpha1_cluster.this[0].config.public_access && length(helm_release.worker) == 0
    error_message = "The control plane must compose the published Marketplace package with private PostgreSQL, without a worker."
  }
}

run "runai_official_worker" {
  command = plan
  providers = {
    nebius     = nebius.native
    helm       = helm
    kubernetes = kubernetes
    kubectl    = kubectl
  }
  module { source = "../modules/runai" }
  variables {
    parent_id       = "project-test-only"
    cluster_id      = "mk8scluster-test-only"
    region          = "test-region"
    bootstrap_token = "test-only-bootstrap"
    config = {
      domain            = "worker.example.com"
      control_plane_url = "https://runai.example.com"
      cluster_uid       = "test-cluster"
      serving = {
        inference_domain = "*.models.example.com"
        spec             = <<-YAML
          version: "1.23.0"
          config:
            domain: {models.example.com: ""}
        YAML
      }
    }
  }
  assert {
    condition     = helm_release.worker[0].repository == "https://helm.ngc.nvidia.com/nvidia/runai-oss" && helm_release.worker[0].version == "3.0.17" && nebius_applications_v1alpha1_k8s_release.ingress[0].product_slug == "nebius/nginx-ingress-controller" && nebius_applications_v1alpha1_k8s_release.prometheus_operator[0].product_slug == "bitnami/kube-prometheus"
    error_message = "Workers must use the unchanged official chart and native Marketplace prerequisites."
  }
  assert {
    condition     = kubernetes_secret_v1.bootstrap[0].data_wo == null && kubernetes_secret_v1.bootstrap[0].data_wo_revision == 1 && local.worker_values.controlPlane.bootstrapToken == "@/runai/config/client_secret"
    error_message = "Bootstrap credentials must remain write-only and use the stock Secret-file contract."
  }
  assert {
    condition     = yamldecode(kubectl_manifest.serving[0].yaml_body).kind == "KnativeServing" && yamldecode(kubectl_manifest.serving_ingress[0].yaml_body).spec.ingressClassName == "nginx" && yamldecode(kubectl_manifest.serving_ingress[0].yaml_body).spec.tls[0].hosts[0] == "*.models.example.com"
    error_message = "Optional serving must retain native manifests and its separately configured TLS ingress."
  }
}

run "unregistered_worker_prerequisites_only" {
  command = plan
  module { source = "../modules/runai" }
  variables {
    parent_id  = "project-test-only"
    cluster_id = "mk8scluster-test-only"
    region     = "test-region"
    config     = { domain = "worker.example.com", cluster_uid = "" }
  }
  assert {
    condition     = length(helm_release.worker) == 0 && length(kubernetes_secret_v1.bootstrap) == 0 && length(nebius_applications_v1alpha1_k8s_release.ingress) == 1 && length(nebius_applications_v1alpha1_k8s_release.prometheus_operator) == 1
    error_message = "Worker registration must be staged after its real endpoint and prerequisites exist."
  }
}

run "override_only_spot_cap" {
  command = plan
  variables {
    gpu_node_groups                  = 2
    gpu_nodes_spot_pricing_policy_id = "pricingpolicy-gpu-test"
    gpu_node_group_overrides = [
      {},
      { platform = "gpu-l40s-a", preset = "1gpu-24vcpu-96gb", preemptible = true },
    ]
  }
  override_data {
    target = data.nebius_billing_v1_pricing_policy.gpu_spot[0]
    values = {
      parent_id             = "project-test-only"
      compute_instance_spec = { v1 = { platform = "gpu-l40s-a" } }
    }
  }
  plan_options { target = [nebius_mk8s_v1_node_group.gpu] }
  assert {
    condition     = nebius_mk8s_v1_node_group.gpu[0].template.spot_pricing_policy == null && nebius_mk8s_v1_node_group.gpu[1].template.spot_pricing_policy.id == "pricingpolicy-gpu-test"
    error_message = "A cap must apply to an override-only Spot pool without constraining the regular pool's GPU type."
  }
}

run "reject_mixed_spot_skus_with_one_cap" {
  command = plan
  variables {
    gpu_node_groups                  = 2
    gpu_nodes_preemptible            = true
    gpu_nodes_spot_pricing_policy_id = "pricingpolicy-gpu-test"
    gpu_node_group_overrides         = [{}, { platform = "gpu-l40s-a", preset = "1gpu-24vcpu-96gb" }]
  }
  override_data {
    target = data.nebius_billing_v1_pricing_policy.gpu_spot[0]
    values = {
      parent_id             = "project-test-only"
      compute_instance_spec = { v1 = { platform = "gpu-h100-sxm" } }
    }
  }
  plan_options { target = [nebius_mk8s_v1_node_group.gpu] }
  expect_failures = [terraform_data.gpu_spot_pricing_validation]
}
