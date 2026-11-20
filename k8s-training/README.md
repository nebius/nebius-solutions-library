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

Nebius Terraform provider `0.6.70` or newer is required for all configurations,
including those with DRA disabled. In existing working directories with an older
locked provider, run `terraform init -upgrade` before `terraform plan`.

## Usage

To deploy a Kubernetes cluster, follow these steps:

1. Copy the example variables file, then replace the placeholders with your values:
   ```bash
   cp terraform.tfvars.example terraform.tfvars
   ```
   Your local `terraform.tfvars` is ignored by Git. See [configuration variables](#configuration-variables) for details.

2. Configure `NEBIUS_TENANT_ID`, `NEBIUS_PROJECT_ID` and `NEBIUS_REGION` in `environment.sh`.

3. Load environment variables (the cluster name defaults to `k8s-training`):
   ```bash
   source ./environment.sh
   ```

   Subnet discovery prefers a subnet whose name starts with `default` in a
   network whose name starts with `default`, then any subnet in such a network,
   then the first subnet in the project. The script lists all pages and prints
   the selected ID. Projects with custom network or subnet names remain supported.
   To select a specific subnet and skip both VPC list calls, set its ID first:

   ```bash
   export NEBIUS_VPC_SUBNET_ID="vpcsubnet-..."
   source ./environment.sh
   ```

   A non-empty `NEBIUS_VPC_SUBNET_ID` is preserved when sourcing the script again.
   Unset it before sourcing to repeat discovery, including when changing projects.

4. Initialize Terraform:
   ```bash
   terraform init
   ```

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

These are the basic configurations required to deploy Kubernetes for training in Nebius AI. Edit the configurations as necessary in your local `terraform.tfvars` file (copied from `terraform.tfvars.example`).

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

### Preemptible node pricing

Regular GPU node groups continue to use Nebius's implicit on-demand default.
This solution only sets a pricing model explicitly for preemptible GPU nodes,
so existing on-demand and Capacity Block Group configurations are left
unchanged. Spot pricing is available only for GPU platforms.

Set `gpu_nodes_preemptible` to `true` to create a preemptible GPU node group. By
default, its VMs accept the current spot price:

```hcl
gpu_nodes_preemptible = true
```

> **Upgrade note:** Existing preemptible GPU node groups created by an earlier
> version of this solution did not set a pricing model explicitly. Adding the
> follow-spot pricing model changes the node-group template and may trigger a
> node roll-out on the next apply. Review the Terraform plan first and allow for
> workload disruption or limited spot capacity while replacement nodes start.

To cap the accepted spot price, create a Nebius Billing pricing policy outside
this solution and pass its ID for the applicable node group:

```hcl
gpu_nodes_preemptible            = true
gpu_nodes_spot_pricing_policy_id = "pricingpolicy-..."
```

This solution does not create the pricing policy. Supply the ID of a pre-existing
policy as a literal value in `terraform.tfvars`. The policy can be created by a
separate Terraform configuration, in the Nebius console, or with the CLI. If a
separate Terraform configuration creates it, pass that configuration's output
ID into this solution; a `.tfvars` file cannot directly reference a resource in
another Terraform root. Keeping policy creation separate allows a billing or
platform team to own and reuse one policy across multiple VM or MK8s deployments.

A pricing policy ID requires `gpu_nodes_preemptible` to be `true`; otherwise
planning fails. The same pricing choice applies to every VM created in the node
group. When an ID is set, Terraform reads the policy and verifies that it belongs
to `parent_id` and targets the selected GPU platform. The identity running
Terraform therefore needs permission to read Billing pricing policies. These
checks catch configuration mismatches but do not guarantee spot capacity.

Pricing-policy behavior to consider:

- Without a policy, nodes follow the current spot price. It can change every
  15 minutes, while remaining below the regular PAYG price for the GPU platform.
- If a policy's maximum price is below the current spot price, scheduling is
  blocked until the price falls or the maximum is raised.
- A policy's maximum price can change only when its `status.running_vm_count` is
  zero. For a shared policy, every running consumer must be stopped first.
- A policy can be shared only by deployments in the same project that use the
  same GPU platform.

### GPU capacity reservation

`gpu_nodes_reservation_policy` controls capacity reservation placement for **generic GPU node groups only**. It does not apply to CPU or GB300 node groups. The same policy and ordered reservation IDs apply to every generic GPU node group.

Leave this variable unset or `null` to inherit the provider default. To require reserved capacity without selecting specific capacity block groups:

```hcl
gpu_nodes_reservation_policy = {
  policy = "STRICT"
}
```

| `policy` | Behavior | `reservation_ids` |
|---|---|---|
| `STRICT` | Use the specified capacity block groups, or suitable available blocks when no IDs are supplied. Fail if capacity is unavailable; do not fall back to PAYG. | Optional. |
| `AUTO` | Try specified reservations, then available capacity blocks, then fall back to pay-as-you-go (PAYG). | Optional. |
| `FORBID` | Use PAYG capacity only; do not use reservations. | Omit or leave empty. |

`reservation_ids` is an ordered list of capacity block group IDs. When the list is non-empty, explicitly set `policy` to `AUTO` or `STRICT`. Omit the list, set it to `null`, or use `[]` when you do not want to select specific groups. Individual entries must not be null, empty, or whitespace-only strings. An empty list does not force PAYG; use `policy = "FORBID"` for that.

This policy is part of the GPU node template, so changing it can trigger node recreation. Review the Terraform plan and the existing [`node_group_strategy` configuration](./variables.tf) before applying it, especially when reserved capacity leaves no room for surge nodes.

### Kubernetes NAT gateway

Create NAT gateway nodes with a reserved public-IP pool for private workers.

```hcl
enable_egress_gateway = false
enable_nat_gateway    = true
cpu_nodes_public_ips  = false
gpu_nodes_public_ips  = false
```

See the [NAT gateway module](../modules/k8s-nat-gateway/README.md) for
requirements, configuration options, and routing cutover.

### GB300 production racks

GB300 uses a separate rack-aware deployment path. Setting `gb300.rack_count` to
a positive whole number replaces the generic GPU node groups with one fixed MK8s
node group and one NVLink instance group per rack. A production rack contains 18
`gpu-gb300` nodes using the `4gpu-112vcpu-800gb` preset, for 72 GPUs per rack.

This recipe requires Nebius Terraform provider `0.6.70` or newer and an
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

#### Optional GPU DRA

Set `gb300.dra = true` to enable Dynamic Resource Allocation (DRA). This recipe
requires an explicit `k8s_version` of `1.36` or later when DRA is enabled:

```hcl
k8s_version = "1.36"

gb300 = {
  rack_count = 2
  dra        = true
}
```

Keep the zero-surge strategy and, for multiple racks, the InfiniBand fabric
configuration shown above. DRA applies to every rack created by `rack_count`.
The recipe installs one NVIDIA GPU DRA Helm release (`0.4.1`) per cluster, with
its kubelet plugins selected onto all DRA-enabled GB300 nodes. MK8s disables its
built-in device plugin and provides managed DRANet on fabric-attached GPU nodes.
The recipe also skips its separate device-plugin installation.

After applying, check driver readiness and advertised devices:

```bash
kubectl -n nvidia-dra-driver-gpu get deployments,daemonsets
kubectl get deviceclasses
kubectl get resourceslices
# For nodes attached to an InfiniBand GPU cluster:
kubectl -n kube-system get daemonset dranet
```

The NVIDIA controller should be available and its kubelet DaemonSet should be
ready on all `18 * rack_count` GPU nodes. For fabric-attached racks, DRANet should
also be ready on those nodes. DRA advertises GPUs through ResourceSlices.

Workloads must request GPUs through ResourceClaims instead of `nvidia.com/gpu`
limits. InfiniBand workloads also need DRANet device claims using an advertised
DeviceClass. Multi-node NVLink workloads need a ComputeDomain and channel claims
so NVIDIA DRA can manage workload IMEX. Create the ComputeDomain and wait for its
generated channel ResourceClaimTemplate before submitting the workload. Use
ARM64-compatible container images. MPIJobs additionally require MPI Operator.
The recipe does not create workload claims or ComputeDomains. See the
[public MK8s DRA setting](https://docs.nebius.com/terraform-provider/reference/resources/mk8s_v1_node_group#nestedatt--template--gpu_settings)
and [NVIDIA DRA documentation](https://github.com/NVIDIA/k8s-dra-driver-gpu/tree/v0.4.1).

DRA is disabled by default. With `dra = false`, the existing device-plugin and
MK8s-managed static IMEX path remains in use. When upgrading an existing non-DRA
deployment, check whether the plan changes an unset `gpu_settings.dra` to `false`.
If it does, treat it as a node-group update and schedule a maintenance window.

Prefer selecting DRA when creating the node groups. To enable it on an existing
deployment, schedule a maintenance window. The device-plugin removal and node
rollout are not ordered, so GPU allocation can be unavailable during the change:

1. Pause GPU workload controllers or job submissions, then stop existing GPU
   workloads so legacy GPU pods are not recreated during the migration.
2. Set `k8s_version` to `1.36` or later and `gb300.dra = true`. Review
   `terraform plan`, retaining the zero-surge strategy, then run `terraform apply`.
3. Wait for the node rollout to finish and all GB300 nodes to become Ready.
   Verify NVIDIA DRA, DRANet where applicable, and advertised devices using the
   readiness checks above.
4. Update workload manifests to use the required ResourceClaims and
   ComputeDomains, then resume controllers or resubmit workloads.

The GB300 path currently rejects autoscaling, preemptible or spot-priced nodes,
public GPU nodes, MIG, custom drivers, x86 NUMA presets, the bundled HGX NCCL
test, and the bundled KubeRay profiles. These guardrails keep the initial
production path within the components validated for Grace ARM rack deployments.

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

For the first login, open the endpoint and sign in as `admin` with the password
from `terraform output -raw grafana_password`. Grafana uses this value when it
creates the admin account in a new database.

To change the password on an existing installation, sign in as `admin`, open
the user menu, and select **Change password**. Verify the new password in a
private browser window before closing your current session. Terraform still
reports its generated bootstrap password. Replacing `random_password.grafana_password`
alone changes that output without updating the release or the existing login.
If you have lost access, use Grafana's reset procedure against the same database.
[Grafana admin password recovery](https://grafana.com/docs/grafana/latest/administration/cli/#reset-admin-password)

### Output compatibility

Grafana authenticates datasource queries with the Observability bearer token
issued by the Nebius CLI. The module retains a separate IAM access key,
`grafana_key`, for callers that use the AWS-compatible credentials in the
`grafana_service_account` output (`access_key_id` and `secret_access_key`).
The `id` field still identifies the service account. Built-in Grafana needs
no migration. If another application reads the removed `access_token` field,
migrate it before upgrading the stack that publishes this output.

#### Migrating a token consumer

1. Find callers that read the token through a module reference, Terraform
   remote state, or a CI script that parses `terraform output`. For an
   independently deployed application, use its own service account's static
   key issued with `--service observability` and deliver it through that
   application's existing secret store. If
   the consumer intentionally shares Grafana's key, the current token is in
   `grafana_access_token.txt` in the producer's Terraform working directory.
   [Recover the file](#recovering-grafana-credentials-on-another-machine) if needed.
2. Replace the old reference with the consumer's secret input. A runtime
   application can read a mounted secret file. For a Terraform consumer, use
   a sensitive, ephemeral variable in a supported destination, such as a
   provider's write-only credential argument. If the resource only accepts a
   regular argument, configure the application to read a runtime secret
   instead. [Terraform ephemeral values](https://developer.hashicorp.com/terraform/language/manage-sensitive-data)

   ```hcl
   variable "observability_access_token" {
     type      = string
     sensitive = true
     ephemeral = true
   }
   ```

   For example, in a separate Terraform consumer that manages a Nebius Grafana
   release, set `"grafana.nebius.accessToken" = var.observability_access_token`
   in its existing `sensitive.set` map. If the key itself changes, include its
   non-secret key ID in the release's `sensitive.version` calculation so
   Terraform resends the write-only value. Supply
   `TF_VAR_observability_access_token` from the consumer's secret store at
   both plan and apply. An existing deployment that shares Grafana's key can
   supply it from the producer's file in a Bash subshell:

   ```bash
   (
     set +x
     set -eu
     : "${CONSUMER_ROOT:?Set the downstream Terraform directory}"
     test -s ./grafana_access_token.txt  # Run from the producer's working directory.
     TF_VAR_observability_access_token=$(<./grafana_access_token.txt)
     export TF_VAR_observability_access_token
     terraform -chdir="$CONSUMER_ROOT" plan
     terraform -chdir="$CONSUMER_ROOT" apply
   )
   ```

3. Confirm the consumer has loaded the intended credential revision, then run
   a known working Observability query **from the migrated consumer**.
   Once it succeeds, upgrade the producer and confirm the consumer still
   works. For a consumer in the same Terraform root, you can also supply the
   credential before planning and review both changes in one plan.
   If the two share a key, update the consumer on every Grafana key
   rotation and verify both applications before revoking the previous key.

### Rotating the Grafana Observability key

Use a separate Terraform checkout for each deployment. Workspaces in the same
checkout share the Grafana token filenames. Rotation and destroy use the files
in the current directory and could revoke another deployment's key.

The token generator tracks the IAM access key's resource ID in
`triggers_replace`. Replacing the generator rotates the Observability token
and keeps the IAM access key. Forcing replacement of `grafana_key` itself
under its existing name fails. The generator's `create_before_destroy` also
applies to its IAM key dependency, so Terraform requests the same name while
the old key still owns it. IAM returns `AlreadyExists` before rotation begins.

Normal Terraform plans do not rotate the key. To request one intentional
rotation, create and review a saved plan that explicitly replaces only the
Grafana token generator:

```bash
terraform plan \
  -replace='module.o11y.terraform_data.grafana_access_token[0]' \
  -out=grafana-rotation.tfplan

terraform show grafana-rotation.tfplan
terraform apply grafana-rotation.tfplan
```

The plan must show replacement of only the Grafana token-generation
`terraform_data` resource and an update of the Grafana application; it must not
replace the cluster or node groups. The rotation creates the replacement before
retiring the old Terraform instance, preserves the previous credential locally
and restarts Grafana with the new token. A subsequent normal plan does not issue
another key.

The following Git-ignored files are created with owner-only permissions:

```text
grafana_access_token.txt
grafana_access_token.metadata.json
grafana_access_token.previous.txt
grafana_access_token.previous.metadata.json
```

After apply, validate all four provisioned datasources in Grafana: **Nebius
Services**, **Nebius Monitoring**, **Nebius Logging** and **Nebius Tracing**.
Do not rotate again while the `previous` files exist. If validation fails,
stop and retain both credentials for diagnosis; do not revoke either key or
request another replacement. This workflow does not automatically roll Grafana
back to the previous credential. Resolve the failed datasource update through
an explicit, reviewed recovery before retrying or finalizing the rotation.

New Observability keys carry the labels `solutions-library-app=grafana` and
`mk8s-cluster-id=<cluster-id>` at issuance. If both current credential files
are missing, an active key with both labels for the current cluster and service
account blocks issuance. A cluster rebuild that reuses the key leaves its
original cluster label in place. If the files are later lost, use the original
cluster ID when identifying the old key for manual revocation after validation.

If the token file is missing, follow [credential recovery](#recovering-grafana-credentials-on-another-machine)
before planning or rotating.

After validation succeeds, invalidate the previous key and remove its local
backup from the checkout used for that rotation:

```bash
set -euo pipefail

PREVIOUS_METADATA="grafana_access_token.previous.metadata.json"
PREVIOUS_TOKEN="grafana_access_token.previous.txt"
test -s "$PREVIOUS_METADATA"
PREVIOUS_KEY_ID="$(jq -r '.id // empty' "$PREVIOUS_METADATA")"

if [[ -n "$PREVIOUS_KEY_ID" ]]; then
  nebius iam static-key delete --id "$PREVIOUS_KEY_ID"
else
  # Compatibility path for credentials created before key metadata was recorded.
  test -s "$PREVIOUS_TOKEN"
  nebius iam static-key revoke --token "$(cat "$PREVIOUS_TOKEN")"
fi

# This line is reached only after the remote delete or revoke succeeds.
rm -f -- "$PREVIOUS_TOKEN" "$PREVIOUS_METADATA"
```

`terraform destroy` invalidates any current or pending previous Grafana
Observability keys before removing their local credential files. It stops with
an error rather than silently orphaning a key if neither the current metadata
nor token file is available on the machine running the destroy.

### Recovering Grafana credentials on another machine

A normal `terraform plan` reads `grafana_access_token.txt`. Initialize the new
checkout against the existing backend and select the deployment's workspace.
If you have a trusted backup of the current key, securely copy the token and
its metadata (when present), plus both `previous` files from any unfinished rotation. Restore
owner-only permissions (`0600`). If the backup may predate the last rotation,
use the provisioning Secret procedure below.

**If the original files are lost and Grafana is running**, an operator with
read access to its provisioning Secret can restore the same active key. First
confirm the four Nebius datasources work and match your Kubernetes context to
the cluster shown by `terraform output -json kube_cluster`. In the Marketplace
Grafana Deployment in namespace `o11y`, find the volume mounted at
`/etc/grafana/provisioning/datasources/datasources.yaml` and its Secret name.
Set `KUBE_CONTEXT` and `DATASOURCE_SECRET` to the verified names. Run this Bash
block in a fresh checkout using `kubectl`, Mike Farah's `yq` v4, `jq`, and an
authenticated Nebius CLI. Preserve any surviving credential files separately.

```bash
(
  set +x
  set -euo pipefail
  umask 077
  : "${KUBE_CONTEXT:?Set the deployment context}" "${DATASOURCE_SECRET:?Set the datasource Secret name}"
  if compgen -G 'grafana_access_token*' >/dev/null; then
    echo "Recovery requires a checkout without Grafana credential files" >&2
    exit 1
  fi
  tmp=$(mktemp -d)
  trap 'rm -rf -- "$tmp"' EXIT
  sa=$(terraform output -json grafana_service_account | jq -er '.id | strings | select(length > 0)')

  kubectl --context "$KUBE_CONTEXT" -n o11y get secret "$DATASOURCE_SECRET" \
    -o jsonpath='{.data.datasources\.yaml}' |
    jq -Rer '@base64d' | yq -o=json '.' |
    jq -er '["Nebius Services","Nebius Monitoring","Nebius Logging","Nebius Tracing"] as $names |
      [.datasources[] | select(.name as $n | $names | index($n) != null)] as $sources |
      ($sources | map(.secureJsonData.httpHeaderValue1) | unique) as $headers |
      if ($sources | map(.name) | sort) == ($names | sort) and ($headers | length) == 1
      then $headers[0] | strings | capture("^Bearer (?<token>[^[:space:]{}$]+)$").token
      else error("Expected the same credential on all four Nebius datasources") end' \
    > "$tmp/token"

  jq -Rs '{token: rtrimstr("\n")}' "$tmp/token" > "$tmp/lookup.json"
  nebius iam static-key find --file "$tmp/lookup.json" --format json |
    jq -e --arg sa "$sa" '(.static_key // .) |
      select(.status.active == true and .spec.service == "OBSERVABILITY" and
             .spec.account.service_account.id == $sa) |
      .metadata | select(.id | strings | length > 0) | {id, name}' > "$tmp/metadata"

  mv "$tmp/metadata" grafana_access_token.metadata.json
  mv "$tmp/token" grafana_access_token.txt
)
```

**Validate either recovery path.** Run `terraform plan -detailed-exitcode`.
A steady deployment returns `0`. Exit `1` indicates an error. Exit `2` means
Terraform found changes that need review. In Grafana, run a known working
query against each of Nebius Services, Monitoring, Logging and Tracing.
The plan checks local readability. The queries check live datasource access.

If the files were lost during a rotation, keep both keys active. While both are
valid, a successful query alone cannot identify which key Grafana used.
Recover the previous key ID from a trusted issuance record or the project
administrator, including for older unlabeled keys. Confirm which key is serving
Grafana before finalizing the previous key.
If that cannot be established, retain both and involve the project
administrator before another rotation or destroy.

If the Secret is unavailable or IAM cannot confirm the key, keep any working
credential active and have the deployment administrator recover a trusted copy.
Terraform state and an IAM key listing cannot reconstruct the token.

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

## Terragrunt

When using this solution as a Git source, select the subdirectory with
`git::https://github.com/nebius/nebius-solutions-library.git//k8s-training?ref=<commit>`.
The double slash keeps the sibling `modules` directory available. Pin `ref` to a
commit that includes the example-file migration.

Supply deployment values through Terragrunt `inputs` and configure authentication
and remote state in your Terragrunt configuration. Do not copy the example into
Terragrunt's downloaded source: a local `terraform.tfvars` can override the values
passed through `inputs`.

## Updating an existing checkout

The tracked `terraform.tfvars` has been renamed to `terraform.tfvars.example`.
Before updating an older checkout, back up your customized `terraform.tfvars` outside
this repository. After updating, restore it as your local `terraform.tfvars` and
compare it with the new example for added settings. Do not overwrite an existing
local configuration with the copy command above.

## Cluster names and existing Terraform state

`environment.sh` defaults an unset or empty `TF_VAR_cluster_name` to
`k8s-training`, matching Terraform's default. It does not parse `terraform.tfvars`,
so it also works when inputs are supplied through Terragrunt. A `cluster_name`
assignment in your local tfvars file takes precedence over the environment variable.

The backend key defaults to `k8s-training.tfstate` for every cluster name. This
preserves the state location of existing deployments, including clusters with
custom names. The script prints the resolved bucket and key before writing
`terraform_backend_override.tf`. It warns when `TF_VAR_cluster_name` is non-default
and `K8S_TF_STATE_KEY` is unset. Existing custom-named deployments can explicitly
set `K8S_TF_STATE_KEY=k8s-training.tfstate` to acknowledge their legacy state key.

For each **additional cluster**, explicitly set a different `K8S_TF_STATE_KEY`.
Otherwise, deployments in the same project and tenant use the same state key.
Keep each cluster's Terraform working directory separate. For example:

```bash
export TF_VAR_cluster_name="training-b"
export K8S_TF_STATE_KEY="training-b.tfstate"
source ./environment.sh
terraform init
```

Supply the same cluster name through your tfvars file or Terragrunt inputs. If
using `TF_VAR_cluster_name` as the source of the name, remove the `cluster_name`
assignment from your local tfvars file. State keys may contain slash-separated
prefixes, but standalone `.` and `..` path segments are rejected.

For an existing deployment, retain its exact bucket and key; changing the cluster
name alone does not migrate state. If intentionally changing the key, back up the
state first and use `terraform init -migrate-state` while the working directory
still has its previous backend configuration in `.terraform`. Review the migration
prompt and subsequent plan before applying. Unsetting `K8S_TF_STATE_KEY` restores
the historical `k8s-training.tfstate` key, not a cluster-specific key.

### Observability upgrades and testing

Kubernetes training passes its cluster name to the observability module, which
prefixes the Grafana and Loki access-key names. Existing module callers that omit
`cluster_name` retain the legacy names. Source inspection of Nebius provider
[0.6.23][access-key-schema-0623] and [0.6.72][access-key-schema-0672] shows no
replacement modifier on `name`; their update implementations pass resource
metadata to the API ([0.6.23][access-key-update-0623],
[0.6.72][access-key-update-0672]). This supports an expected in-place rename, but
does not demonstrate an upgrade against existing state. No live upgrade plan was
run. Review the plan with your selected provider version before upgrading; no
Terraform resource address changes, so a `moved` block is not needed.

The published [API name schema][resource-metadata] has no restriction excluding
dots. Acceptance of the prefixed names by the live API has not been tested.

The example keeps `loki.enabled = false`, so copying it does not deploy Loki.
Set it to `true` to enable Loki. CI separately plans a fixture with Loki enabled
and mocked providers. The test harness copies the access-key resources and their
IAM dependencies directly from this module, so it checks the production resource
definitions without provisioning a cluster or changing the example's default.
These plans start with empty state and check names, not upgrade or replacement
behavior. The fixture excludes ephemeral MysteryBox resources, which Terraform
1.11 cannot mock; secret retrieval and application deployment are not covered.
Run the same check locally with
`python3 modules/o11y/tests/check_access_keys.py` from the repository root.

[access-key-schema-0623]: https://github.com/nebius/terraform-provider-nebius/blob/v0.6.23/generated/nebius/iam/v2/access_key_service.tfgen.go#L206-L211
[access-key-schema-0672]: https://github.com/nebius/terraform-provider-nebius/blob/v0.6.72/generated/nebius/iam/v2/access_key_service.tfgen.go#L210-L217
[access-key-update-0623]: https://github.com/nebius/terraform-provider-nebius/blob/v0.6.23/generated/nebius/iam/v2/access_key_service.tfgen.go#L452-L463
[access-key-update-0672]: https://github.com/nebius/terraform-provider-nebius/blob/v0.6.72/generated/nebius/iam/v2/access_key_service.tfgen.go#L469-L480
[resource-metadata]: https://github.com/nebius/api/blob/main/nebius/common/v1/metadata.proto#L22-L25
