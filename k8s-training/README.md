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

### GB300 production racks

GB300 uses a separate rack-aware deployment path. Setting `gb300.rack_count` to
a positive whole number replaces the generic GPU node groups with one fixed MK8s
node group and one NVLink instance group per rack. A production rack contains 18
`gpu-gb300` nodes using the `4gpu-112vcpu-800gb` preset, for 72 GPUs per rack.

The GB300 resources require Nebius Terraform provider `0.5.232` or newer and an
explicit zero-surge rollout strategy. Replacing nodes one at a time avoids
requesting capacity above the physical 18-node rack. An InfiniBand fabric is
optional for one rack and required for deployments with two or more racks:

GB300 boot disks default to 1 TiB (1024 GiB). Set `local_nvme = true` to
request the host NVMe devices and combine them into kubelet ephemeral storage.
Local NVMe data is erased when a node is restarted or replaced.

```hcl
node_group_strategy = {
  max_unavailable = { count = 1 }
  max_surge       = { count = 0 }
}
```

#### One rack

One rack creates one 18-node MK8s node group and one 18-node NVLink instance
group. Leave `infiniband_fabric` empty when the rack does not require an
InfiniBand GPU cluster:

```hcl
infiniband_fabric = ""

gb300 = {
  rack_count = 1
}
```

#### Two racks

Two racks create 36 MK8s nodes and two separate NVLink instance groups. Both
racks use the single XDR fabric selected by `infiniband_fabric`:

```hcl
infiniband_fabric = "fabric-4"

gb300 = {
  rack_count                = 2
  boot_disk_size_gibibytes = 1024
  local_nvme                = true
}
```

Use the same syntax for larger deployments; for example, `rack_count = 6`
creates six 18-node groups and six NVLink instance groups. Terraform generates
stable sequential rack keys (`rack-001`, `rack-002`, and so on), so increasing
the count adds racks without changing the addresses of existing racks. Reducing
the count removes the highest-numbered racks.

When configured, all racks attach to the same XDR InfiniBand fabric, while each
rack receives its own NVLink instance group. A fabricless single rack remains
inside its 72-GPU NVLink domain and has no cross-rack InfiniBand connectivity.
The node groups use Ubuntu 24.04 driverfull images with the `cuda13.0` driver
preset. Set `rack_count = 0` or omit `gb300` to disable this path.

Capacity policies such as 17+1 or 16+2 are scheduling and operational policies
over a complete 18-node rack. They do not reduce the MK8s node-group or NVLink
instance-group size. Spare nodes should remain part of the rack and be excluded
from normal workloads through Kubernetes scheduling policy.

IMEX configuration is owned by MK8s Node Infrastructure for driverfull GB300
node groups. Before deploying, confirm the selected Node Infrastructure version
includes managed GB300 IMEX support. Do not install the Omega PoC IMEX
DaemonSet: MK8s writes `/etc/nvidia-imex/nodes_config.cfg` and manages
`nvidia-imex.service`. DRA mode is intentionally not exposed by this template
until MK8s provides a supported public selection path.

The GB300 path currently rejects autoscaling, preemptible or public GPU nodes,
MIG, custom drivers, x86 NUMA presets, the bundled HGX NCCL test, and the
bundled KubeRay profiles. These guardrails keep the initial production path
within the components validated for Grace ARM rack deployments.

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

### Kubernetes RBAC access bindings

Kubernetes RBAC can be managed by Terraform after the access model has been
approved. It is disabled by default.

An approved Nebius Cloud group cluster-admin binding can be declared like this:

```hcl
k8s_rbac_bindings = {
  enabled = true
  cluster_role_bindings = {
    nebius_viewer_cluster_admin = {
      name      = "nebius-cluster-admin"
      role_name = "cluster-admin"
      subjects = [
        {
          kind      = "Group"
          name      = "nebius:viewer"
          api_group = "rbac.authorization.k8s.io"
        }
      ]
    }
  }
}
```

For namespace-only access, create the namespace and bind one of the built-in
ClusterRoles such as `view`, `edit`, or `admin`:

```hcl
k8s_rbac_bindings = {
  enabled = true
  namespaces = {
    workload = {
      name = "workload"
    }
  }
  namespace_role_bindings = {
    workload_admin = {
      name      = "workload-admin"
      namespace = "workload"
      role_kind = "ClusterRole"
      role_name = "admin"
      subjects = [
        {
          kind      = "Group"
          name      = "nebius:viewer"
          api_group = "rbac.authorization.k8s.io"
        }
      ]
    }
  }
}
```

Temporary elevated access should be kept as an explicit Terraform change and
removed after validation so Terraform destroys the binding on the next apply.
This module does not manage Nebius IAM group membership, kubeconfig sharing, or
private endpoint/bastion access.

### Kueue Job queueing

[Kueue](https://kueue.sigs.k8s.io/) provides Kubernetes-native Job queueing. It
controls when Jobs may start, while Kubernetes continues to schedule their
Pods.

Kueue is disabled by default. Enable it in `terraform.tfvars`:

```hcl
kueue = {
  enabled = true
}
```

Terraform installs Kueue on the CPU node group, enables topology-aware
scheduling, and creates a `ResourceFlavor` for each GPU node group.

Queue quotas and namespace access remain configurable through Kueue
`ClusterQueue` and `LocalQueue` resources. See the
[Kueue module guide](../modules/kueue/README.md) and its editable
[GPU queue-policy example](../modules/kueue/examples/topology-aware-gpu-queue.yaml)
for the short setup workflow.

Only Jobs labeled with `kueue.x-k8s.io/queue-name` are managed by Kueue.

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
