ssh_user_name: ubuntu
# Public key of the user
ssh_public_key: 
k8s_cluster:
  cpu_nodes_count: 3
  gpu_nodes_count_per_group: 2
  gpu_node_groups: 1
  cpu_nodes_platform: "cpu-d3"
  cpu_nodes_preset: "4vcpu-16gb"
  gpu_nodes_platform: "gpu-h100-sxm"
  gpu_nodes_preset: "1gpu-16vcpu-200gb"
  enable_gpu_cluster: false
  infiniband_fabric: "fabric-2"
  gpu_nodes_driverfull_image: true
  enable_k8s_node_group_sa: true
  enable_prometheus: true
  enable_loki: false
  loki_access_key_id: ""
  loki_secret_key: ""
  gpu_health_cheker: false
kubeflow:
  kubeflow_hostname: deploykf.example.com
