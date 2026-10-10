# Optional GPU startup snapshot add-on

The fleet can install an independently packaged, NVIDIA-Snapshot-compatible
Helm add-on. The default is disabled; existing workloads keep their current
commands, admission, scaling and draining owners.

The backend chart and model lifecycle qualification belong to the add-on's
repository. This library configures its release, namespace and shared storage.
An image cache or fast weight transfer alone does not qualify process restore.

## Configuration

Set these keys on one chosen region in `terraform.tfvars`:

```hcl
weights_filesystem = {
  enabled = true
  size_gib = 256
  mount_on_system = true
}
gpu_snapshot = {
  enabled = true
  chart = "/absolute/path/to/reviewed/gpu-snapshot-<version>.tgz"
  namespace = "gpu-snapshot-system"
  values_yaml = <<-YAML
    snapshot:
      pageBroker:
        enabled: false
  YAML
}
```

Alternatively provide `repository`, `chart` and a pinned `version`. Authenticate
to a private chart registry using the normal local Helm credential mechanism;
keep credentials out of `values_yaml` and Git.

Run `./stack.sh preflight`, review `./stack.sh plan cloud`, then
`./stack.sh apply cloud`. Enabling system-node filesystem attachment rolls
the system pool with its existing surge/drain strategy. GPU templates remain
unchanged. Preserve running
workloads and plan this before qualification or while the relevant pool is idle.

`./stack.sh plan platform <region-id>` shows the optional namespace and release;
`./stack.sh apply platform <region-id>` installs them. The namespace is dedicated
and privileged because a checkpoint node agent needs host/runtime access; tenant
namespace policies remain unchanged.

The release has no dependency on serving platform waves. An explicitly targeted
snapshot plan can install or upgrade only its namespace and Helm release while
another owner manages unrelated platform drift.

For each selected GPU pool, add `labels = { "serverless2.nebius/snapshot-store" = "true" }`
before qualification, preferably while the pool is at zero. This explicitly
qualifies the agent node set without rolling unrelated warm GPU pools. A backend
may instead use a separately probed shared-filesystem selector.

Set `gpu_snapshot.storage_node_selector` to that attested selector when reusing
already-mounted GPU nodes, for example `{ "topology.kubernetes.io/region" = "eu-west2" }`
after every selected mount is verified. Its default is the explicit
`serverless2.nebius/snapshot-store=true` label. CPU operators additionally require
the system pool's mount-attestation label. Avoid null label overrides: Helm
provider merging can retain them and produce a different manifest from CLI rendering.

The shared checkpoint directory is root-owned at `/mnt/weights/gpu-snapshot`,
separate from weights and customer operation checkpoints. Node labels attest
the shared mount on CPU operator nodes so cleanup/status remain available
when GPU pools scale to zero. A local NVMe/DRAM cache is disposable and must have
its own bounded resources and restore integrity checks in the backend chart.

Opt-in labels must be applied to future pod templates. Use only profiles whose
exact image, weights, configuration, GPU and driver/kernel tuple passed capture,
fresh-pod restore and result-correctness tests. The add-on must cold-start when
a snapshot is missing/incompatible and recover failed restores without starting
a competing model process or scaler.

API-managed model definitions accept optional, static `pod_metadata`:

```json
{"pod_metadata":{"labels":{"example.org/startup-profile":"qualified-science"},
 "annotations":{"example.org/qualification":"evidence/model-immutable.json"}}}
```

Use the label and annotation keys documented by the chosen add-on. The API saves
them in the model definition; endpoint rendering puts them on the KServe
predictor, and Job/JobSet rendering puts them on the actual pod template, including
MultiKueue manager templates. Existing queue, placement and serving metadata are
reserved. Only an administrator can change the definition; invocation inputs
cannot select or replace the profile. Removing the field removes the opt-in on
future templates. The API supports the field; dedicated console controls remain
a separate integration step.

For an endpoint whose model process does not use the Kubernetes API, an
administrator can also set `automount_service_account_token: false`. It renders
the predictor's supported `automountServiceAccountToken` field; omitting it keeps
the previous Kubernetes default. This setting is rejected for run classes because
their existing uploader and execution helpers require service-account access.

These settings do not prepare an opaque process for capture,
remove model credentials, create checkpoint artifacts,
qualify scientific outputs or override the add-on's compatibility checks.
Request-dependent run commands/work mounts and credential-bearing model
containers still require an isolated, request-free initialization adapter. Verify
the actual Knative/Kueue consumer path before claiming an accelerated endpoint.
