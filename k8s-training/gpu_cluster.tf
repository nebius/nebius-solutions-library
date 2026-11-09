resource "nebius_compute_v1_gpu_cluster" "fabric_2" {
  count = local.enable_gpu_cluster ? 1 : 0

  infiniband_fabric = local.infiniband_fabric
  parent_id         = var.parent_id
  name              = join("-", [local.infiniband_fabric, local.release-suffix])
}
