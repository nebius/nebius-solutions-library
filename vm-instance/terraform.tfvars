#parent_id      = "" # The project-id in this context
#subnet_id      = "" # Use the command "nebius vpc v1alpha1 network list" to see the subnet id


#preset = "16vcpu-64gb"
#platform = "cpu-d3"
#preset = "8gpu-192vcpu-2768gb"
preset            = "1gpu-24vcpu-346gb"
platform          = "gpu-b300-sxm"
boot_image_family = "ubuntu24.04-cuda13.0"

users = [
  {
    user_name    = "tux",
    ssh_key_path = "~/.ssh/id_rsa.pub"
  },
  {
    user_name      = "tux2",
    ssh_public_key = "<SSH KEY STRING>"
  }
]

public_ip      = true
instance_count = 2
preemptible    = false

shared_filesystem_id = ""
mount_bucket         = ""

fabric = "" # For the 8-GPU B300 preset, set this to "eu-west2-a" to create a GPU cluster.
