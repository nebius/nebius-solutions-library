# Kubernetes fleet with Terragrunt

Terragrunt **1.2.1** orchestrates any number of configured clusters. Each generated
unit runs the existing `k8s-training` **Terraform** root directly, with its own
state. Regional resource definitions, provider configuration and existing
single-region resource addresses stay in that Terraform solution. Cluster names
are stable keys, independent of region; several clusters can share one region.

`terragrunt.stack.hcl` uses native `unit.expansion.for_each` over `clusters`.
`unit/terragrunt.hcl` passes merged inputs to regional Terraform and generates its
S3 backend. No separate Terraform fleet wrapper is required.

## Configure and review

Choose `training.tfvars.example`, `inference.tfvars.example` or
`runai-dynamo.tfvars.example`. Replace project/subnet/reservation identities and
protected file paths, then save the actual inputs outside Git. The profile's
`defaults` are shallow-merged with each cluster; nested objects are replaced as
whole objects. Terragrunt reads these HCL/JSON data files natively. Use absolute
paths for chart values, certificates and other protected files.
For the stack's default filename, copy the chosen profile to `terraform.tfvars`;
this local file is ignored.

Configure an existing versioned Nebius state bucket using
[`backend.hcl.example`](backend.hcl.example). Access keys remain in the operator's
environment/secret manager. Each state key is
`<K8S_FLEET_STATE_PREFIX>/<cluster-key>/terraform.tfstate`; the prefix defaults to
`k8s-fleet`. Native S3 lockfiles protect concurrent updates to each state.
Terraform/foundation owns the bucket; Terragrunt consumes it without bootstrapping
or changing bucket configuration.

Supply a **pinned Git source** containing `//k8s-training`. The double slash keeps
sibling `../modules` paths valid. Git fetches tracked source, excluding ignored
local state, credentials and backend files. Use a reviewed immutable commit
containing the regional inputs used by the selected profile.

From the repository root:

```sh
export K8S_FLEET_CONFIG=/secure/fleet.tfvars
export K8S_FLEET_BACKEND=/secure/backend.hcl
export K8S_TERRAFORM_SOURCE='git::https://github.com/nebius/nebius-solutions-library.git//k8s-training?ref=REPLACE_WITH_REVIEWED_COMMIT'
export K8S_FLEET_STATE_PREFIX=demo-fleet
# Supply native Nebius auth, TF_VAR_iam_token and S3 credentials separately.
terragrunt --working-dir=k8s-multi-region stack generate
terragrunt --working-dir=k8s-multi-region stack run -- plan
```

Generation expands all entries under `.terragrunt-stack/clusters/<key>`; it does
not provision infrastructure. Review plans and apply through the operator's
normal deployment process. For one cluster, run Terragrunt from its generated
unit directory. Do not source the regional `environment.sh` inside Terragrunt:
the fleet already supplies backend/project settings. Avoid ambient per-cluster
`TF_VAR_*` overrides when running several units together.

Keep each cluster entry until that cluster has been destroyed through its own
state. Removing an entry does not destroy its resources. Existing states require
a separately reviewed adoption/migration; no state operations or moved blocks
run automatically. Regional provider lockfiles and saved plans belong with the
operator's protected release records.

## Independently configured pools

Each cluster accepts the [regional pool inputs](../k8s-training/README.md#independently-configured-regional-pools).
Keep GPU list indices stable across updates. Each cluster uses one CPU node group,
configured through the existing flat CPU inputs. Autoscaling counts **nodes**,
not GPUs: two eight-GPU nodes provide sixteen GPUs. Capacity
reservations apply to regular GPU pools; Spot pools omit reservation settings.
Check the regional catalog, quotas and managed-image compatibility before deployment.

GPU and Network Operators use Nebius Marketplace, which selects their chart
versions. Supply custom YAML strings through `gpu_operator_values` and
`network_operator_values`. Keep `gpu_nodes_driverfull_image=true` to use
Nebius's preinstalled drivers. Managed-image guards use typed Marketplace `set`
overrides, which take precedence over the YAML and disable operator-installed
GPU drivers and duplicate GPU components.

## Run:ai workers

The Run:ai/Dynamo profile connects three example worker clusters to one existing
control plane. First provision that separate CPU cluster using
[`runai-control-plane.tfvars.example`](../k8s-training/runai-control-plane.tfvars.example).
Register every worker through the native API, then supply its distinct UUID,
fresh bootstrap token, DNS/TLS and regional operator/chart/storage inputs.
Terragrunt handles fleet execution; Terraform installs the shared components.
Dynamo reuses Run:ai's KAI owner.

References: [native stack expansion](https://docs.terragrunt.com/reference/hcl/blocks/#expansion),
[Terragrunt state backends](https://docs.terragrunt.com/features/units/state-backend/),
[Nebius Terraform state](https://docs.nebius.com/terraform-provider/store-terraform-state).
