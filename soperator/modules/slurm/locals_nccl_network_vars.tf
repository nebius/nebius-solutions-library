locals {
  worker_nccl_network_devices_by_platform = {
    gpu-h100-sxm = "mlx5_0:1,mlx5_1:1,mlx5_2:1,mlx5_3:1,mlx5_4:1,mlx5_5:1,mlx5_6:1,mlx5_7:1"
    gpu-h200-sxm = "mlx5_0:1,mlx5_1:1,mlx5_2:1,mlx5_3:1,mlx5_4:1,mlx5_5:1,mlx5_6:1,mlx5_7:1"
    gpu-b200-sxm = "mlx5_4:1,mlx5_5:1,mlx5_6:1,mlx5_7:1,mlx5_8:1,mlx5_9:1,mlx5_10:1,mlx5_11:1"
    gpu-b300-sxm = "mlx5_4:1,mlx5_5:1,mlx5_6:1,mlx5_7:1,mlx5_8:1,mlx5_9:1,mlx5_10:1,mlx5_11:1"
    gpu-gb300    = "mlx5_0:1,mlx5_1:1,mlx5_2:1,mlx5_3:1"
  }

  worker_nccl_network_vars_file_name = "90-nccl-network-vars.sh"

  worker_nccl_network_vars = {
    for nodeset in var.worker_nodesets : nodeset.name => {
      config_map_name = lower(replace("${nodeset.name}-nccl-network-vars", "/[^0-9A-Za-z.-]/", "-"))
      file_name       = local.worker_nccl_network_vars_file_name
      content = join("\n", [
        "# Managed by Terraform.",
        "export UCX_NET_DEVICES=${local.worker_nccl_network_devices_by_platform[nodeset.platform]}",
        "export NCCL_IB_HCA=${local.worker_nccl_network_devices_by_platform[nodeset.platform]}",
        "",
      ])
    }
    if contains(keys(local.worker_nccl_network_devices_by_platform), nodeset.platform)
  }
}
