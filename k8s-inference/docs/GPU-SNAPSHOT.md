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
