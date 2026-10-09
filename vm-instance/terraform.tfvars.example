parent_id      = "project-u02aad0nig009q3hnm4gda" # The project-id in this context
subnet_id      = "vpcsubnet-u02en3xntb0pqj9zwb" # Use the command "nebius vpc v1alpha1 network list" to see the subnet id


#preset = "16vcpu-64gb"
#platform = "cpu-d3"
#preset = "8gpu-128vcpu-1600gb"
preset   = "8gpu-192vcpu-2768gb"
platform = "gpu-b300-sxm"

users = [
  {
    user_name    = "ubuntu",
    ssh_key_path = "~/.ssh/id_rsa.pub"
  },
]

public_ip      = true
instance_count = 1
preemptible    = true

shared_filesystem_id = ""
mount_bucket         = ""
enable_local_disks = true # Only B300 supportes local disk

fabric = "us-north1-a"
