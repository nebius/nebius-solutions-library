# charts/endpoint

One KServe `InferenceService` (Knative mode) for an explicit model container:
GPU, pool placement, scale-to-zero, probes, volumes, optional PVCs and
LocalModelCache. `values.yaml` documents every key; `values.schema.json`
validates them.

## Two ways to pass values

**Plain**: the keys of `values.yaml` at the top level (`name`, `image`,
`pool`, `resources`, `scaling`, ...). Used for ad-hoc rendering and tests.

**Catalog mode** (how the platform deploys): a catalog entry
`catalog/models/<id>.yaml` is the values file and `cluster=<name>` is set:

```
helm template <id> charts/endpoint -f catalog/models/<id>.yaml --set cluster=eu-south1
```

The chart then takes `runtime` as the base values, merges
`deployments.<cluster>` over it (maps deep-merged, lists replaced; `paused`,
`price_per_gpu_hour` and `parameters` are not chart values and are dropped),
sets `name` from `id`, `namespace` from the entry (default `models`) and the
labels `serverless2.nebius/task|mode|cluster`. Entries without `runtime` are
run classes and have no endpoint.

## Model weights on the shared filesystem

Each region has a Nebius Compute shared filesystem (Terraform
`weights_filesystem` per region in `fleet.yaml`) that every GPU node
mounts at `/mnt/weights` (virtiofs, ReadWriteMany). A model opts in with

```yaml
weights:
  sharedFilesystem: { enabled: true, path: <model>, mountPath: /weights }   # hostPath /mnt/weights/<model>
  env: { HF_HOME: /weights }                                                 # whatever the runtime reads
```

The chart mounts the cluster's ReadWriteMany claim `weights-shared` (a static
hostPath PersistentVolume over `/mnt/weights`,
`clusters/<cluster>/apps/overlays/scheduling/weights-pv.yaml`) with
`subPath: <path>`, adds an init container that makes the directory writable
(subPath directories are created root-owned) and the env vars. Knative allows
hostPath volumes only read-only, so the PVC route is used (feature flags
`podspec-persistent-volume-claim` and `-write`). Runtimes fill their cache on
the first start and every later cold start is warm; `ops/seed-weights` fills
it ahead of time (docs/OPERATIONS.md "Model weights"). `storageUri`/KServe
model agents are not used: this chart runs explicit containers, so the
weights path is just a directory the container reads.

The API renders every endpoint with this chart (nothing in Terraform renders endpoints). Each render includes a native SecurityPolicy selecting the model's external routes and using the shared API authorizer. Use `envFrom` for Secret references. Model-write reconciliation persists retry state before applying resources; see `docs/OPERATIONS.md`.
