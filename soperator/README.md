# Slurm on Kubernetes with Soperator - Installation Guide

Welcome! This guide will help you set up a Slurm cluster running on Kubernetes using Nebius Cloud.
The entire setup process is automated with Terraform, allowing you to deploy your cluster with a few commands.

## Why Run Slurm on Kubernetes?

Our solution offers several key benefits:

- **Effortless Scaling**: Add or remove nodes instantly without manual bootstrapping
- **Built-in High Availability**: Automatic pod restarts and self-healing capabilities
- **Unified Storage**: Shared root filesystem across all nodes - no more version sync headaches
- **Enhanced Security**: Isolated environments prevent accidental system breakage
- **Automated GPU Health Checks**: Regular NCCL tests ensure optimal GPU performance

## Prerequisites

Before starting, ensure you have these tools installed:

- [Terraform](https://developer.hashicorp.com/terraform/tutorials/aws-get-started/install-cli)
- [Nebius CLI](https://nebius.com/docs/cli/quickstart)
- [kubectl](https://kubernetes.io/docs/tasks/tools/)
- [jq](https://jqlang.github.io/jq/download/)
- coreutils: 
  - macOS: `brew install coreutils`
  - Ubuntu: `sudo apt-get install coreutils`

## Installation Steps

### 1. Get Terraform Files

The recommended way: download and unpack [the latest release](https://github.com/nebius/nebius-solutions-library/releases?q=%22Soperator+Terraform+recipe%22&expanded=true):
```bash
tar -xvf soperator-tf-*.**.**-*.tar.gz
```

An alternative way: checkout a release git tag:
```bash
git fetch --all --tags && git checkout tags/soperator-[VERSION]
```

### 2. Create Your Installation Directory

Assuming you are in the repository root or unpacked directory.

```bash
cd soperator
export INSTALLATION_NAME=<your-name> # e.g. company name
mkdir -p installations/$INSTALLATION_NAME
cd installations/$INSTALLATION_NAME
cp -r ../example/* ../example/.* .
```

### 3. Set Up Your Environment

Set your `NEBIUS_TENANT_ID` and `NEBIUS_PROJECT_ID` in the `.envrc` file, then run:

```bash
source .envrc
```

This command loads environment variables and performs several important setup tasks:
- Authenticates with Nebius CLI and exports IAM token
- Creates/retrieves service account for Terraform
- Configures Object Storage access for the Terraform state
- Exports environment variables with resource IDs 

Check that NEBIUS_IAM_TOKEN is valid:
```bash
nebius iam whoami
```

By default, `public_o11y_enabled` is true, so you need to either disable it or specify `nebius` CLI profile 
for o11y. This option defines whether you want to collect all infra logs in nebius o11y or not.
By default, `soperator-telemetry` is used as a profile for public o11y setup. You can redefine it by setting 
`NEBIUS_OLLY_PROFILE` variable.

### 4. (Optional) Create Storage Infrastructure

Create a "jail" filesystem in the Nebius Console. Jail is a shared filesystem for all Slurm nodes.
It is called "jail" because it resembles [FreeBSD jail mechanism](https://en.wikipedia.org/wiki/FreeBSD_jail).

This step is required for those who want to persist their jail data after the cluster deletion.
You can offload storage creation to the Terraform script instead, but it will be deleted with the cluster unless you set `forbid_deletion = true` in the filesystem `spec`.

![Create Filesystem 1](imgs/create_fs_1.png)
![Create Filesystem 2](imgs/create_fs_2.png)

> [!NOTE] 
> - For storage > 2 TiB: Contact Nebius Support (in the web console) to enable multitablet functionality
> - Note down the filesystem ID for your terraform configuration
> - **Attaching an existing filesystem as a Jail will cause the cluster to overwrite data. Use submounts to avoid this behavior!**
> ![Create Filesystem 2](imgs/create_fs_3.png)

### 5. Configure Your Cluster

Edit `terraform.tfvars` with your requirements:

```hcl
# Name of the company. It is used for context name of the cluster in .kubeconfig file.
company_name = "<YOUR-COMPANY-NAME>"

# ...

# Use your manually created jail filesystem
filestore_jail = {
  existing = {
    id = "computefilesystem-<YOUR-FILESYSTEM-ID>"
  }
}

# Or create the jail filesystem with Terraform and protect it from deletion
# filestore_jail = {
#   spec = {
#     size_gibibytes       = 2048
#     block_size_kibibytes = 4
#     forbid_deletion      = true
#   }
# }

# ...

# Configuration of Slurm Worker node sets.
slurm_nodeset_workers = [{
  size                    = <TOTAL_NODES_NUMBER> # Recommended value for soperator development is 2.
  max_unavailable_percent = 50
  resource = {
    platform = "gpu-h100-sxm" # For a CPU-only cluster, see https://docs.nebius.com/compute/virtual-machines/types.
    preset   = "8gpu-128vcpu-1600gb"
  }
  boot_disk = {
    type                 = "NETWORK_SSD"
    size_gibibytes       = 2048
    block_size_kibibytes = 4
  }
  gpu_cluster = {
    infiniband_fabric = "" # Contact support for the correct value.
  }
}]

# Add your SSH public key here to connect to the Slurm cluster. 
slurm_login_ssh_root_public_keys = [
  "ssh-rsa AAAAB3N... your-key"
]
```

`k8s_cluster_node_ssh_access_users` is for connecting to the K8S cluster itself.
You probably don't need this unless you want to manage the K8S cluster manually.

> [!NOTE] 
> - For large clusters: Use larger presets for CPU-only nodes
> - Adjust storage sizes based on your needs
> - Contact support to increase quotas if needed
> - Ensure SSH keys are added to the correct location

### 6. Deploy Your Cluster

#### 6.a. Init terraform

```bash
terraform init
```

#### 6.b. (Optional) Set Up Terraform Workspace

This is a required step if you want to have several Soperator clusters in one Terraform state storage.

```bash
terraform workspace list # Explore existing workspaces.
terraform workspace new <MY-CLUSTER-NAME>
```

#### 6.c. Deploy the Slurm Cluster

```bash
terraform apply
```

This will take ~40 min for a small GPU cluster (2 nodes with 8 GPUs each).

### 7. (Optional) Verify Kubernetes Setup
- List kubectl contexts to verify that the new cluster was added
```bash
kubectl config get-contexts
```

The new context should be named `nebius-<your-company-name>-slurm`.

- Set the new context if it is not the current one
```bash
kubectl config use-context nebius-<your-company-name>-slurm
```

- Verify that you can list the pods in the cluster and there are no pods in the error state
```bash
kubectl get pods --all-namespaces
```

- Verify all resources show green status in the console

### 8. Get Cluster Connection Details

Get the Slurm cluster IP address
```bash
export SLURM_IP=$(terraform state show module.login_script.terraform_data.lb_service_ip | grep 'input' | grep -oE '[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+' | head -n 1)
ssh root@$SLURM_IP -i ~/.ssh/<private_key>
```

or connect using the login script:

```bash
./login.sh -k ~/.ssh/<private_key>
```

### 9. (Optional) Destroy the Cluster and Retain Terraform-Created Shared Filesystems

If Terraform created the shared filesystems from `spec` blocks and you want to delete the cluster while keeping the data, first protect the filesystems and then remove only those retained filesystems from Terraform state before running `terraform destroy`.

1. Enable deletion protection on each Terraform-created shared filesystem that must be retained.

```hcl
filestore_jail = {
  spec = {
    size_gibibytes       = 2048
    block_size_kibibytes = 4
    forbid_deletion      = true
  }
}

filestore_jail_submounts = [{
  name       = "data"
  mount_path = "/mnt/data"
  spec = {
    size_gibibytes       = 4096
    block_size_kibibytes = 32
    forbid_deletion      = true
  }
}]
```

2. Apply the deletion-protection change before removing anything from state.

```bash
terraform plan
terraform apply
```

Confirm the plan only updates deletion protection on the filesystems you intend to retain.

3. Record the retained filesystem IDs and the exact Terraform resource addresses.

```bash
terraform state list | grep 'module.filestore.nebius_compute_v1_filesystem'
terraform state show 'module.filestore.nebius_compute_v1_filesystem.jail[0]'
terraform state show 'module.filestore.nebius_compute_v1_filesystem.jail_submount["data"]'
```

4. Back up the Terraform state according to your team's state-management process. For a local backup, store the state file securely because it may contain sensitive values.

```bash
terraform state pull > terraform-state-before-retaining-filesystems.tfstate
```

5. Remove only the retained filesystems from Terraform state. This makes Terraform forget those filesystems so the cluster destroy does not try to delete them.

```bash
terraform state rm 'module.filestore.nebius_compute_v1_filesystem.jail[0]'
terraform state rm 'module.filestore.nebius_compute_v1_filesystem.jail_submount["data"]'
```

If you also created and want to retain controller spool or accounting filesystems, remove their resource addresses as well:

```bash
terraform state rm 'module.filestore.nebius_compute_v1_filesystem.controller_spool[0]'
terraform state rm 'module.filestore.nebius_compute_v1_filesystem.accounting[0]'
```

6. Verify the destroy plan before applying it.

```bash
terraform plan -destroy
```

Confirm that the retained `nebius_compute_v1_filesystem` resources are not listed for deletion. If a retained filesystem still appears in the destroy plan, stop and check the state addresses before continuing.

7. Destroy the remaining cluster resources.

```bash
terraform destroy
```

8. To reuse the retained filesystems in a future cluster, configure them as `existing` filesystems with the IDs recorded earlier.

```hcl
filestore_jail = {
  existing = {
    id = "computefilesystem-<RETAINED-JAIL-ID>"
  }
}

filestore_jail_submounts = [{
  name       = "data"
  mount_path = "/mnt/data"
  existing = {
    id = "computefilesystem-<RETAINED-DATA-ID>"
  }
}]
```

`forbid_deletion = true` prevents accidental provider-side deletion, but removing the retained filesystems from Terraform state is what allows `terraform destroy` to complete without trying to delete them.

## (Optional) Test Your Installation

See the process of delivery and running of tests in [test](./test/README.md).
