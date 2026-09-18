# Kubernetes for training in Nebius AI

## Features

- Creating a Kubernetes cluster with CPU and GPU nodes.

## Prerequisites

1. Install [Nebius CLI](https://docs.nebius.ai/cli/install/):
   ```bash
   curl -sSL https://storage.eu-north1.nebius.cloud/cli/install.sh | bash
   ```

2. Reload your shell session:

   ```bash
   exec -l $SHELL
   ```

   or

   ```bash
   source ~/.bashrc
   ```

3. [Configure Nebius CLI](https://docs.nebius.com/cli/configure/) (it is recommended to use [service account](https://docs.nebius.com/iam/service-accounts/manage/) for configuration)

4. Install JQ:
   - MacOS:
     ```bash
     brew install jq
     ```
   - Debian based distributions:
     ```bash
     sudo apt install jq -y
     ```

## Usage

To deploy a Kubernetes cluster, follow these steps:

1. Configure `NEBIUS_TENANT_ID`, `NEBIUS_PROJECT_ID` and `NEBIUS_REGION` in environment.sh.

2. Load environment variables:
   ```bash
   source ./environment.sh
   ```

3. Initialize Terraform:
   ```bash
   terraform init
   ```

4. Replace the placeholder content
   in `terraform.tfvars` with configuration values that meet your specific
   requirements. See the details [below](#configuration-variables).

5. Preview the deployment plan:
   ```bash
   terraform plan
   ```
6. Apply the configuration:
   ```bash
   terraform apply
   ```
   Wait for the operation to complete.

## Configuration variables

These are the basic configurations required to deploy Kubernetes for training in Nebius AI. Edit the configurations as necessary in the `terraform.tfvars` file.

Additional configurable variables can be found in the `variables.tf` file.

### SSH configuration

```hcl
# SSH config
ssh_user_name  = "" # Username you want to use to connect to the nodes
ssh_public_key = {
  key  = "Enter your public SSH key here" OR
  path = "Enter the path to your public SSH key here"
}
```

### Kubernetes nodes

```hcl
# K8s nodes
cpu_nodes_count  = 3 # Number of CPU nodes
cpu_nodes_preset = "16vcpu-64gb" # CPU node preset
gpu_nodes_count  = 1 # Number of GPU nodes

gpu_nodes_preset = "8gpu-128vcpu-1600gb" # The GPU node preset. Only nodes with 8 GPU can be added to gpu cluster with infiniband connection.

```

### Nvidia Multi Instance GPU (MIG) configuration

```hcl
# MIG configuration
mig_strategy = "single" # If set, possible values include 'single', 'mixed', 'none'
mig_parted_config = "all-disabled" # If set, value will be checked against allowed for the selected 'gpu_nodes_platform'
```

See [NVIDIA documentation for different MIG strategies](https://docs.nvidia.com/datacenter/cloud-native/kubernetes/latest/index.html#testing-with-different-strategies) and [MIG partitioning configurations for different GPU platforms](https://docs.nvidia.com/datacenter/tesla/mig-user-guide/).

### Observability options

```hcl
# Observability
enable_nebius_o11y_agent = true  # Enable or disable Nebius Observability Agent deployment for metrics and logs from user workloads.
enable_grafana           = true  # Enable or disable Grafana® solution by Nebius used together with Nebius Observability Agent
enable_prometheus        = false # Enable or disable Prometheus and Grafana deployment for local metric storage (not using Nebius observability stack)
enable_loki              = false # Enable or disable Loki deployment for local logs storage (not using Nebius observability stack)

### Storage configuration

```hcl
# Storage
## Filestore - recommended
enable_filestore     = true # Enable or disable Filestore integration with true or false
filestore_disk_size  = 100 * (1024 * 1024 * 1024) #Set the Filestore disk size in bytes. The multiplication makes it easier to set the size in GB, giving you a total of 100 GB
filestore_block_size = 4096 # Set the Filestore block size in bytes
```

You can use Filestore to add external storage to K8s clusters, this allows you to create a Read-Write-Many HostPath PVCs in a K8s cluster. Use the following paths: `/mnt/filestore` for Filestore.

For more information on how to access storage in K8s, refer [here](#accessing-storage).

### Local NVMe ephemeral storage

For the supported B300 platform (`gpu-b300-sxm`, preset `8gpu-192vcpu-2768gb`), enable local disks with:

```hcl
gpu_enable_local_disks = true
```

The GPU node template sets `local_disks.config.kubelet_ephemeral = true` and requests local disk passthrough. This corresponds to the CLI flags `--template-local-disks-config-kubelet-ephemeral` and `--template-local-disks-passthrough-group-requested` in the [Nebius node-group reference](https://docs.nebius.com/cli/reference/mk8s/node-group/create).

MK8S combines the local disks into a volume for kubelet ephemeral storage. The shared Kubernetes cloud-init template contains no custom RAID creation, formatting, or mounting commands. The former `/scratch` mount is not created. The obsolete `local_nvme_drives_path` variable has been removed; remove it from existing tfvars files if present.

Workloads can use disk-backed `emptyDir` volumes and specify `ephemeral-storage` requests and limits. This storage is temporary, not persistent application storage. Existing workloads using `/scratch` must be updated before rolling out this change. Review `terraform plan` for node replacement or rollout before applying; changing the template does not migrate existing data.

### Shared filesystem CSI automation

When a shared filesystem is present, either because this stack created it or because `existing_filestore` was provided, Terraform can also install the Nebius Shared Filesystem CSI driver and promote its StorageClass to the cluster default.

```hcl
enable_filestore                                  = true
existing_filestore                                = "" # or an existing filesystem ID
filestore_mount_path                              = "/mnt/data"
filesystem_csi = {
  chart_version                       = "0.1.5"
  namespace                           = "kube-system"
  make_default_storage_class          = true
  previous_default_storage_class_name = "compute-csi-default-sc"
}
```

This Terraform automation installs the CSI driver and configures the StorageClass only. Verification, pod-level validation, and cleanup remain in `filesystem-csi-validation/` as an explicit opt-in workflow.

## Connecting to the cluster

### Preparing the environment

- Install kubectl ([instructions](https://kubernetes.io/docs/tasks/tools/#kubectl))
- Install the Nebius AI CLI ([instructions](https://docs.nebius.ai/cli/install))
- Install jq ([instructions](https://jqlang.github.io/jq/download/))

### Adding credentials to the kubectl configuration file

1. Perform the following command from the terraform deployment folder:

```bash
nebius mk8s v1 cluster get-credentials --id $(cat terraform.tfstate | jq -r '.resources[] | select(.type == "nebius_mk8s_v1_cluster") | .instances[].attributes.id') --external
```

### Add credentials to the kubectl configuration file
1. Run the following command from the terraform deployment folder:
   ```bash
   nebius mk8s v1 cluster get-credentials --id $(cat terraform.tfstate | jq -r '.resources[] | select(.type == "nebius_mk8s_v1_cluster") | .instances[].attributes.id') --external
   ```
2. Verify the kubectl configuration after adding the credentials:

   ```bash
   kubectl config view
   ```

   The output should look like this:

   ```bash
   apiVersion: v1
   clusters:
     - cluster:
       certificate-authority-data: DATA+OMITTED
   ```

### Connect to the cluster
Show cluster information:

```bash
kubectl cluster-info
```

Get pods:

```bash
kubectl get pods -A
```

## Observability

Observability stack by default use Nebius Observability Agent deployment for metrics and logs storage. and Grafana® solution by Nebius.

To access Grafana GUI:
```
Nebius Web GUI > Main menu > Applications > grafana-solution-by-nebius > Endpoints + Create > Copy URL
```
Open browser to newly created URL with username “admin” and password from output of “terraform output grafana_password”

## Accessing storage

### Using mounted StorageClass

To use mounted storage, you need to manually create Persistent Volumes (PVs). Use the template below to create a PV and PVC.
Replace `<SIZE>` and `<HOST-PATH>` variables with your specific values.

```yaml
kind: PersistentVolume
apiVersion: v1
metadata:
  name: external-storage-persistent-volume
spec:
  storageClassName: csi-mounted-fs-path-sc
  capacity:
    storage: "<SIZE>"
  accessModes:
    - ReadWriteMany
  hostPath:
    path: "<HOST-PATH>" # "/mnt/data/<sub-directory>"

---

kind: PersistentVolumeClaim
apiVersion: v1
metadata:
  name: external-storage-persistent-volumeclaim
spec:
  storageClassName: csi-mounted-fs-path-sc
  accessModes:
    - ReadWriteMany
  resources:
    requests:
      storage: "<SIZE>"
```

## CSI limitations:
- FS should be mounted to all NodeGroups, because PV attachmend to pod runniing on Node without FS will fail
- One PV may fill up to all common FS size
- FS size will not be autoupdated if PV size exceed it spec size
- FS size for now can't be updated through API, only through NEBOPS. (thread)
- volumeMode: Block  - is not possible

## Good to know:
- read-write many mode PV will work
- MSP started testing that solution to enable early integration with mk8s.
=======
