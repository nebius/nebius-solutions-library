# Kubernetes for training in Nebius AI

## Features

- Creating a Kubernetes cluster with CPU and GPU nodes.

## Prerequisites

1. Install [Nebius CLI](https://docs.nebius.ai/cli/install/):
   ```bash
   curl -sSL https://artifacts.nebius.cloud/cli/install.sh | bash
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
includes managed GB300 IMEX support. MK8s writes `/etc/nvidia-imex/nodes_config.cfg` and manages
`nvidia-imex.service`. DRA mode is intentionally not exposed by this template
until MK8s provides a supported public selection path.

The GB300 path currently rejects autoscaling, preemptible or public GPU nodes,
MIG, custom drivers, x86 NUMA presets, the bundled HGX NCCL test, and the
bundled KubeRay profiles. These guardrails keep the initial production path
within the components validated for Grace ARM rack deployments.

### Nvidia Multi Instance GPU (MIG) configuration

The current MIG path uses driverless GPU nodes so GPU Operator owns the NVIDIA
driver, Container Toolkit, device plugin, and MIG Manager. Driverfull nodes use
the MK8s-managed static device plugin and are supported for full-GPU workloads,
but not for MIG in this recipe.

```hcl
# MIG configuration
gpu_nodes_driverfull_image = false
mig_strategy               = "mixed"
mig_parted_config          = "all-1g.10gb"

# Keep Toolkit's generated containerd drop-in compatible with the MK8s root
# configuration and restart host containerd through systemd.
gpu_operator_toolkit_config_source = "file"
gpu_operator_toolkit_restart_mode  = "systemd"
```

Changing between driverfull and driverless images updates the node template and
rolls the GPU node group. Review the rollout strategy and available GPU capacity
before applying that change. DRA is a separate allocation path and is not
enabled by these settings.

MK8s applies node-template labels only when it creates a Kubernetes Node; a
later label change is not propagated to existing Nodes and does not by itself
trigger a rollout. The recipe keeps that template label for newly created and
autoscaled nodes, and also runs a Terraform-managed Kubernetes Job when
the desired `mig_parted_config`, target node-group identities, or reconciler
image changes. The Job invalidates the previous
configuration state, applies the desired label to the existing GPU nodes, and
waits up to 15 minutes for every selected node to report
`nvidia.com/mig.config.state=success`. A failed or timed-out MIG configuration
therefore fails the Terraform apply instead of reporting success after label
delivery alone. The Job does not drain workloads or restart MIG Manager; prepare
the affected GPUs before applying and inspect MIG Manager logs when convergence
fails. The Compute instances are not replaced.

The Job is a one-shot apply gate, not a health controller. Same-group node
replacement and scale-out bootstrap from the node-template label and do not
necessarily rerun a completed Job. Verify MIG state and device inventory on
every new node before declaring the fleet ready. A Job identity change also
reissues the profile request: drain affected GPU workloads before applying it.

`mig_reconciler_kubectl_version` pins the Job's client image (default `1.35.0`,
compatible with control planes 1.34–1.36). Override it when needed to remain
within [Kubernetes' supported one-minor version skew](https://kubernetes.io/releases/version-skew-policy/#kubectl).
When `k8s_version` is unset, verify the actual control-plane version yourself.

For clusters where MIG might be enabled or disabled later, keep
`mig_strategy="mixed"`. Setting `mig_parted_config=null` is normalized to the
explicit `all-disabled` configuration, so MIG Manager remains available to
remove existing partitions and can re-enable them in a later apply. Setting
`mig_strategy="none"` removes that management path; first apply `all-disabled`
with `mixed`, verify every node reaches `success`, and only then disable the
strategy in a separate apply. The `none` transition is a distinct, untested
mode rather than a requirement for running full GPUs. Keeping `mixed` with
`all-disabled` leaves one MIG Manager pod on each eligible GPU node and is the
recommended toggle-ready state when the cluster might enable MIG later.

Changing only `mig_strategy` from `mixed` to `none` does not change the image or
the MK8s `template.gpu_settings.dra` field, so the current Terraform data flow
does not imply a Compute node rollout. It does update GPU Operator ownership,
remove the Terraform MIG reconciler and its node-template label, and remove the
MIG-success selector from the DRA kubelet plugin. This lifecycle has not been
validated yet: review the provider's rollout warning in the plan and compare
Node UIDs and provider IDs after a dedicated test before documenting it as an
in-place transition.

### MIG and DRA allocation modes

MIG and DRA are independent choices in this recipe. MIG controls whether and
how physical GPUs are partitioned. DRA controls how Kubernetes advertises and
allocates the resulting full GPUs or MIG devices.

| MIG | DRA | Geometry owner | Allocation owner | Workload interface |
| --- | --- | --- | --- | --- |
| Off | Off | MIG Manager enforcing `all-disabled` | NVIDIA device plugin | `nvidia.com/gpu` limits |
| On | Off | GPU Operator MIG Manager | NVIDIA device plugin | `nvidia.com/mig-*` limits |
| Off | On | MIG Manager enforcing `all-disabled` | NVIDIA DRA driver | `gpu.nvidia.com` claims |
| On | On | GPU Operator MIG Manager | NVIDIA DRA driver | `mig.nvidia.com` claims |

The Off rows assume `mixed` with `all-disabled`, retaining MIG Manager for
later profile changes. With `mig_strategy=null` or `none`, MIG Manager is not
the geometry owner. Use `mixed` for heterogeneous configurations such as
`all-balanced`; `single` requires uniform MIG devices.

When DRA is enabled, the legacy device plugin is not running and its extended
resources (`nvidia.com/gpu` and `nvidia.com/mig-*`) have no usable allocatable
capacity. Zero-valued resource keys may remain on a Node after plugin removal.
The NVIDIA DRA chart can still create both `gpu.nvidia.com` and
`mig.nvidia.com` DeviceClass objects. Their existence alone does not prove that
matching devices are available: inspect `ResourceSlice` contents. DRA-only
publishes full-GPU devices, while static MIG+DRA publishes MIG devices matching
the applied geometry.

For a DRA-only cluster that may later enable MIG, use `mig_strategy="mixed"`
with `mig_parted_config=null`; Terraform applies `all-disabled`, and the DRA
driver publishes full GPUs only after MIG Manager reports success. For MIG-only,
leave `gpu_dra.enabled=false`; GPU Operator manages both the static geometry and
the profile-specific device-plugin resources.

The combined static MIG+DRA path is the integration-sensitive mode. GPU
Operator must create the MIG geometry, while the DRA driver must allocate those
devices without attempting to reconfigure them. This mode has currently been
validated on H100; B200 and B300 require their own compatibility and lifecycle
tests. GB300 is a separate driverfull/NVLink architecture and remains outside
this path.

#### H100 static MIG with DRA

GPU Operator owns the driver, Container Toolkit, and MIG Manager. The NVIDIA
DRA driver is the sole allocator; the legacy NVIDIA device plugin is disabled.
Dynamic MIG reconfiguration is deliberately disabled so there is only one MIG
geometry owner.

```hcl
gpu_nodes_driverfull_image = false
gpu_nodes_platform         = "gpu-h100-sxm"
# Leave null to retain the MK8s backend-selected version on an existing
# cluster. If set explicitly, use 1.34 or a newer Kubernetes minor version.
k8s_version                = null
mig_strategy               = "mixed"
mig_parted_config          = "all-1g.10gb"

gpu_dra = {
  enabled = true
}
```

All DRA modes require an actual Kubernetes patch version of 1.34.2 or newer.
The Nebius provider accepts a minor version such as `1.34` or `1.36`, while
MK8s resolves it to a supported patch release. Leave `k8s_version` unset when
retaining an existing backend-selected version; setting it explicitly can roll
the control plane and node groups. Verify the deployed version separately.
Terraform enables CDI,
sets `template.gpu_settings.dra = true`, labels the GPU nodes for the DRA
kubelet plugin, disables the legacy NVIDIA device plugin, and installs a pinned
NVIDIA DRA driver chart.

The DRA kubelet plugin selects nodes carrying
`nvidia.com/dra-kubelet-plugin=true`. Whenever the recipe retains MIG Manager,
Terraform also adds `nvidia.com/mig.config.state=success` to that selector,
including full-GPU DRA with `mixed` and `all-disabled`. The additional gate
prevents DRA from publishing a stale full-GPU inventory before MIG Manager
finishes. When the MIG state leaves `success` during a later geometry change,
the DaemonSet removes and recreates the node plugin after MIG converges, so a
manual plugin restart is not part of the normal procedure.

ComputeDomain support and NVIDIA DynamicMIG remain explicitly disabled in this
recipe. ComputeDomain is a separate MNNVL/IMEX concern used by GB300-style
workloads. DynamicMIG would transfer MIG geometry ownership to the DRA driver;
it must not be enabled while GPU Operator MIG Manager owns static partitioning.

Enabling or disabling DRA changes the node-group instance template and rolls
every GPU node. With a zero-surge strategy, nodes are replaced according to
`max_unavailable` without requesting spare GPU capacity. Changing only
`mig_parted_config` is reconciled in place; drain affected GPU workloads first,
because active CUDA processes can prevent MIG reconfiguration.

After applying the combined path, production verification must confirm all of
the following:

- every target node is Ready and reports
  `nvidia.com/mig.config.state=success`;
- no legacy NVIDIA device-plugin pod is running on those nodes;
- one NVIDIA DRA kubelet-plugin pod is Ready on every target node;
- `gpu.nvidia.com` driver ResourceSlices contain the expected MIG profile and
  count; `mig.nvidia.com` is the DeviceClass used to select those devices;
- a disposable `ResourceClaimTemplate` selecting
  `device.attributes['gpu.nvidia.com'].profile == '1g.10gb'` schedules and runs
  a CUDA test successfully;
- the same checks still pass after rebooting or replacing one canary node.

Workloads must use DRA `ResourceClaim` or `ResourceClaimTemplate` objects and
the `mig.nvidia.com` DeviceClass. For example, a workload needing four separate
nominal 10 GB instances requests `count: 4` and selects profile `1g.10gb`.
Those devices remain separate; this does not create one 40 GB memory pool or
prove that a single CUDA process can use all of them. Multi-device application
behavior needs its own test.

#### Select MIG devices by memory

For Jobs and replicas that each need their own devices, prefer a
`ResourceClaimTemplate`. Kubernetes creates a separate `ResourceClaim` for each
Pod that references it. This template requests one existing MIG device by
advertised capacity instead of naming its profile:

```yaml
apiVersion: resource.k8s.io/v1
kind: ResourceClaimTemplate
metadata:
  name: mig-at-least-40gb
spec:
  spec:
    devices:
      requests:
        - name: gpu
          exactly:
            deviceClassName: mig.nvidia.com
            allocationMode: ExactCount
            count: 1
            selectors:
              - cel:
                  expression: >-
                    device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("40G")) >= 0
```

Create the template in the same namespace as the Job. The Job's Pod references
the template, and its container references the Pod-local claim name `gpu`:

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: mig-memory-example
spec:
  backoffLimit: 0
  template:
    spec:
      restartPolicy: Never
      resourceClaims:
        - name: gpu
          resourceClaimTemplateName: mig-at-least-40gb
      containers:
        - name: workload
          image: your-registry/your-cuda-application:your-tag
          resources:
            claims:
              - name: gpu
```

Replace the image with your application image compatible with the installed
GPU driver. Each Pod gets its own claim and allocation; the generated claim is
garbage-collected when its Pod is deleted, not merely when the Job completes.
Use a direct `ResourceClaim` instead when you deliberately need a claim with an
independent lifecycle or shared access across Pods. See the
[Kubernetes DRA workload guide](https://v1-36.docs.kubernetes.io/docs/tasks/configure-pod-container/assign-resources/allocate-devices-dra/).

Like a PVC, a `ResourceClaim` expresses what the workload needs rather than
choosing a specific physical device. A template automates creating these
requests; the DRA driver publishes `ResourceSlice` inventory automatically.
Neither the template nor the claim creates GPU capacity or changes our static
MIG layout.

A minimum allows any larger matching device;
a 20 GB minimum can therefore select a 40 GB slice. To require a 20–30 GB range, use:

```text
device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("20G")) >= 0 &&
device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("30G")) < 0
```

`G` means decimal GB and `Gi` means binary GiB. Nominal profile names are not
exact allocatable memory amounts: inspect the capacity advertised in
`ResourceSlice` objects before choosing a threshold. A request stays Pending
if no available device matches. Selection reserves a whole existing device; it does
not carve that many bytes or combine smaller devices.

#### Version and platform qualification

The standalone NVIDIA DRA chart defaults to the pinned version `0.4.1`.
The [Marketplace release resource](https://docs.nebius.com/terraform-provider/reference/resources/applications_v1alpha1_k8s_release)
exposes no Operator/chart version selector. Its `sensitive.version` values
digest only triggers configuration updates; it is not a software-version pin.
Confirm the installed versions on a new cluster before treating it as the same
configuration. Pinning operand image tags alone does not pin the Operator chart,
CRDs or defaults. A directly managed Helm release is a separate installation
path that requires its own ownership and lifecycle validation; do not enable
`custom_driver` as a drop-in workaround for the combined MIG+DRA setup.

Leave `test_mode=false` for MIG or DRA: the bundled NCCL test requests legacy
full-GPU resources and is not a validation workload for either allocation mode.

Combined-mode validation is platform-generic: H200, B200 and B300 are not
blocked merely because only H100 has completed live qualification. Driverless
ownership, Kubernetes version and platform-specific MIG profile checks still
apply. These platforms need hardware tests before being declared production
qualified; their memory capacities and profile names differ from H100.
The separate GB300 rack path still needs an implemented DRA ownership handoff
from its MK8s-managed drivers, runtime and IMEX; this is a current recipe
limitation, not a permanent hardware exclusion.

GPU Operator 26.7 adds a managed DRA workflow based on `GPUCluster`, which
cannot coexist with `ClusterPolicy` and is not a supported in-place migration
from this deployment. Its static-MIG and runtime ownership must be designed
separately; changing a chart version alone does not implement that workflow.
An Operator upgrade is not itself a switch to `GPUCluster`: its creation is
opt-in. Retaining `ClusterPolicy` with standalone DRA is a separate compatibility
path that has not been qualified by this recipe; do not assume the current
configuration is incompatible solely because the newer workflow exists.
ComputeDomain support predates 26.7 and is disabled in this recipe. See the
[26.7 DRA installation guide](https://docs.nvidia.com/datacenter/cloud-native/gpu-operator/26.7/dra-intro-install.html)
and [version support matrix](https://docs.nvidia.com/datacenter/cloud-native/gpu-operator/26.7/platform-support.html).

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
  chart_repository                    = "oci://cr.nebius.cloud/mk8s/helm"
  chart_version                       = "0.1.5"
  image_repository                    = "cr.nebius.cloud/mk8s/csi-mounted-fs-path"
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
