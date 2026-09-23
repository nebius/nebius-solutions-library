resource "nebius_compute_v1_gpu_cluster" "fabric_2" {
  for_each = var.enable_gpu_cluster ? local.cluster_config : {}

  infiniband_fabric = each.value.infiniband_fabric
  parent_id         = each.value.parent_id
  name              = join("-", [each.value.infiniband_fabric, local.release-suffix, each.key])
}
