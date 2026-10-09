# VM Instances
This Terraform configuration script provisions cloud instances with specific hardware requirements and performs some configuration tasks, such as user setup, storage management, and AWS S3 integration. It supports a flexible, cloud-agnostic setup where users can specify various hardware presets, add extra storage, configure SSH access, and mount shared filesystems.

## Features
* Create multiple vm instances with or without public ip, and with ssh access
* Add multiple users
* Connect shared file system to all instances
* Installing and configuration of aws cli for s3 access
* Mount S3 Bucket to all instances
* Attach extra storage to all instances
* Add VMs to a GPU cluster and connect them over Infiniband

## Configuring Terraform for Nebius Cloud

- Install [Nebius CLI](https://docs.nebius.com/cli/install/).
- Add environment variables for Terraform authentication in Nebuis Cloud.


Run `source ./.envrc.sh` if you use bash, or `source ./.envrc.zsh` if you use zsh.

These scripts export the Terraform variables used by this module, create the Object Storage bucket used for Terraform state, and let you select the tenant and project where you want to deploy.

If you want to configure S3 credentials for bucket mounting outside of Terraform, use `source ./s3_keys.sh`.

## Usage

Update the placeholder values in `terraform.tfvars`, then run:


```
terraform init
terraform plan
terraform apply
```


## Configuration Variables - Examples

### Example 1: Basic Configuration with One User

`public_ip = true` enables a public IP on each instance created by this module.
*Consider setting `public_ip = false` if you do not require public IP addresses.
*Consider using [Bastion](https://github.com/nebius/nebius-solution-library/tree/main/bastion) solution if you need to manage a set of virtual machines in the same network.

```
preset = "16vcpu-64gb"
platform = "cpu-e2"

users = [
  {
    user_name = "admin",
    ssh_key_path = "~/.ssh/id_rsa.pub"
  }
]

public_ip = true
instance_count = 1
```

### Example 2: Instances with the same shared filesystem

1. First, you'll have to create the shared file system in the console under Compute - Shared Filesystems. Check here for more information: https://docs.nebius.com/compute/storage/manage.
2. Copy and paste the id (`computefilesystem-xxxxx`) into "shared_filesystem_id" in `terraform.tfvars`
3. Increase the number of hosts by increasing `instance_count`

```
preset = "16vcpu-64gb"
platform = "cpu-e2"

users = [
  {
    user_name = "admin",
    ssh_key_path = "~/.ssh/id_rsa.pub"
  }
]
shared_filesystem_mount = "/mnt/share"  # optional
shared_filesystem_id = "computefilesystem-xxxxx"
public_ip = true
instance_count = 2
```
The filesystem will be mounted to `/mnt/share` by default. You can change that by setting ```shared_filesystem_mount```

### Example 3: Working with aws s3. 

AWS cli will be installed and credentials will be configured on instance creation.

run 
```
aws s3 ls
```

to see available buckets

If you prefer, you can also mount a bucket into your file system.

```
preset = "16vcpu-64gb"
platform = "cpu-e2"

users = [
  {
    user_name = "admin",
    ssh_key_path = "~/.ssh/id_rsa.pub"
  }
]
mount_bucket = "my-bucket-name"
s3_mount_path = "/mnt/s3" # optional
public_ip = true
instance_count = 2
```

This will mount the bucket into the given directory, or into `/mnt/s3` if no directory is given. 

### Example 4: Provision vms inside a GPU Cluster


```
preset = "8gpu-128vcpu-1600gb"
platform = "gpu-h100-sxm"

users = [
  {
    user_name = "admin",
    ssh_key_path = "~/.ssh/id_rsa.pub"
  }
]

public_ip = true
instance_count = 2

fabric = "fabric-6"

```
This will create a GPU cluster and add all vms inside there. This gives them the possibility to connect over Infiniband. 

It is not possible to change that for already running instances


### B300 local NVMe scratch storage

```hcl
platform                       = "gpu-b300-sxm"
preset                         = "8gpu-192vcpu-2768gb"
enable_local_disks             = true
local_disks_mount_mode         = "raid0" # or "raw" for individual ext4 mounts
local_nvme_drives_path         = "/scratch"
local_disks_expected_count     = 6 # Confirm the device count for your allocation.
local_disks_dependent_services = ["training.service"] # Use your actual workload services.
```

Local disks are ephemeral: API stop/start loses their contents. The boot service
recreates blank storage and mounts it without persisting filesystem UUIDs in
fstab. It retries discovery and refuses unexpected counts, root backing disks,
partitions, mixed RAID signatures, unrelated arrays, and conflicting mounts.
The default count remains six for compatibility; it is not autodetected.

Use a dedicated `/scratch`, `/mnt/<name>`, or `/srv/<name>` directory. Symlink
paths are rejected. RAID mode mounts one ext4 filesystem; raw mode mounts each
device at `disk-0`, `disk-1`, etc. Raw indices follow device enumeration and are
not persistent disk identities. Mount roots use sticky world-writable permissions.

List each workload service in `local_disks_dependent_services` to install
`Requires=` and `After=` dependencies on `local-nvme.service`. A failed preparation
then prevents those services from starting. This does not gate arbitrary processes
or stop services already running when cloud-init first installs the configuration;
start workloads only after cloud-init completes successfully. On an existing VM,
stop workloads before updating the service and its dependencies.

Cloud-init changes are intended for newly provisioned VMs; updating Terraform user
data does not reliably reinstall scripts on an existing VM. For manual migration,
remove only the old scratch entries from `/etc/fstab` before rebooting, and remove
only the old scratch ARRAY entry from `/etc/mdadm/mdadm.conf` (rebuild initramfs if
it contained that entry). Preserve unrelated mounts and arrays. The new script does
not rewrite either file. Switching between raw and RAID modes requires deliberate
cleanup of the previous storage; the script refuses to erase existing signatures.

Validate on a disposable B300 VM with first boot, guest reboot, and API stop/start
before using this for workloads. Terraform rendering checks cannot validate device
presentation or RAID behavior on the host.
