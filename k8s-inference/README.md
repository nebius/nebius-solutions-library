# Kubernetes inference fleet on Nebius

*For architects and platform engineers who deploy an inference platform for a customer, and for the
people who run it afterwards.*

This Terraform solution brings up a complete, multi-region GPU inference platform in a Nebius tenant from
one file, `terraform.tfvars`. You get one API for models, three ways to run them (always-on endpoints,
queued endpoint calls, long jobs with checkpoints), queueing across regions by cost, fast cold starts
through a per-region image cache, tenants with API keys and budgets, and the usual operations stack.
Everything inside the clusters is an upstream Helm chart or a small manifest; the own code is the API,
the cost dispatcher and the job runner (about 3,000 lines of Python and shell).

| You get | How it is built |
|---|---|
| One API for models: invoke, poll, fetch, cancel, resume | `services/api` behind API keys (LiteLLM) and an Envoy gateway; `docs/API.md` |
| Endpoints that scale to zero, with a warm floor if you want one | KServe on Knative, one `InferenceService` per model and region |
| Queued endpoint calls and long jobs that survive spot preemption | Kubernetes Jobs with a checkpoint volume; Kueue admits them, `docs/JOBS.md` |
| Multi-node jobs over InfiniBand | JobSet on a pool whose nodes form a Nebius GPU cluster; optional per pool |
| N regions, one queue | Kueue MultiKueue on a control cluster; a dispatcher sends each job to the cheapest free pool of the GPU class the model prefers, `docs/SCHEDULING.md` |
| Fast pod starts | Zot pull-through cache plus Spegel peer-to-peer in every cluster, one logical registry host, pre-pull per pool, a shared weights filesystem; `docs/IMAGES.md` |
| Tenants | a namespace per cluster, a bucket and identity per region, API keys with budgets, Pod Security baseline, optional GPU quota and image allow-list |
| Spot, on-demand and reserved capacity | per pool: spot with a price cap, on-demand, or capacity blocks used first |
| Operations | Grafana, Prometheus, Loki, Alertmanager, OpenCost with Nebius prices, Postgres backups, spot-node recovery; `docs/OBSERVABILITY.md`, `docs/OPERATIONS.md` |

## How it is put together

```
terraform.tfvars ──▶ ./stack.sh ──▶ cloud      one state: clusters, pools, filesystems, static IPs, identities, registry, buckets
                                ──▶ platform   one state per cluster: Helm releases and manifests (workers first, then control)
                                ──▶ models     one state per cluster: tenants, endpoints, API keys, acceptance probe
```

- **Control cluster** (CPU only, the default): the customer API, LiteLLM and Postgres, the console, the
  Kueue MultiKueue manager with the cost dispatcher and price feed, Grafana. `control_plane.dedicated = false`
  is the single-cluster mode for a proof of concept: those services run on the one region's system pool.
- **Region clusters** (one per Nebius region, one project each): GPU pools, Knative/KServe endpoints, an
  Envoy gateway with key checks, a regional API, the image cache, DCGM, OpenCost, Loki, the weights filesystem.

`docs/ARCHITECTURE.md` has the component diagram and the request paths; `docs/REQUIREMENTS.md` maps every
requirement to the mechanism that implements it.

## Prerequisites

- A Nebius tenant with one project per region (the control cluster can share the first region's project),
  a subnet in each, and quotas for the pools you plan: GPUs per platform, network-ssd for filesystems and
  caches, one public IP per cluster.
- The Nebius CLI with a profile that has `editor` on those projects. Terraform 1.11 or newer, `helm`,
  `kubectl`, and `docker buildx` or `crane` for the platform images.
- Optional: an NVIDIA NGC key for NIM images, a Hugging Face token for gated weights. They are read from
  environment variables, never written into the tfvars.

## Quick start

About 45 minutes from an empty project to the first model call. Every step is one command.

```sh
cp terraform.tfvars.example terraform.tfvars   # 1. name, CLI profile, projects, subnets, pools, tenants, models
stack/bootstrap/state-bucket.sh                # 2. once: the Terraform state bucket and its credentials
./stack.sh preflight                           # 3. every pool against the Nebius compatibility matrix and quotas
./stack.sh apply cloud                         # 4. clusters, pools, filesystems, IPs, identities, registry (about 15 min)
tools/images.sh build                          # 5. build and push the five platform images to the fleet's registry
./stack.sh apply                               # 6. platform on every cluster, then tenants, models and the acceptance probe
./stack.sh output models control               # 7. URLs; tenant keys: ./stack.sh output models control -json tenant_keys
```

Then call a model:

```sh
API=$(./stack.sh output models control -raw api_url); KEY=<a tenant key from step 7>
curl -H "Authorization: Bearer $KEY" $API/v1/models
curl -X POST -H "Authorization: Bearer $KEY" -H 'content-type: application/json' \
  -d '{"mode":"run","input":{"message":"hello"}}' $API/v1/models/hello-run:invoke
```

`./stack.sh apply` runs the stages in dependency order. Each stage can also run alone (`./stack.sh plan
platform hub`), every state lives in the bucket, and `./stack.sh tf <stage> [<id>] -- <args>` runs any
Terraform command against a stage. `make check` renders everything and runs the tests without touching
the cloud (run `make venv` once first: it creates the Python virtualenv the tests use); CI runs the same gate.

## The one file

`terraform.tfvars.example` explains every key in place; the schema with its validations is
`stack/config/variables.tf`.

| Section | What it defines |
|---|---|
| `name`, `nebius_profile`, `labels`, `protect_data` | prefix of every cloud resource, the CLI profile, the destroy guard for data |
| `control_plane` | the dedicated CPU cluster (project, subnet, system pool, API allow-list), or single-cluster mode |
| `regions.<region>` | project, subnet, weights filesystem, `pools`: platform, preset, GPU class, capacity (spot with price cap, on-demand, reserved), min and max nodes, local NVMe, InfiniBand |
| `images` | the logical registry host, the source registry, cache size and retention, upstream registries, platform image tags |
| `prices` | USD per GPU-hour per platform: the scheduling order and the cost reports |
| `edge` | public or internal gateways, optional domain, allowed source CIDRs, ACME email, API rate limit |
| `observability` | retention, alert webhook, cost export |
| `secrets` | the names of the environment variables that carry NGC, Hugging Face and registry credentials |
| `tenants.<name>` | regions, output retention, keys with budgets, optional GPU quota, fair share, dedicated pools, image allow-list |
| `models` | ids from the bundled catalog (`catalog/models`) and your own entries in the same schema |
| `acceptance` | the probe that runs after the models stage: one run and one endpoint call |

### Vocabulary

- **fleet**: everything one `terraform.tfvars` describes. **region**: one Nebius region, one project, one
  cluster; its short **cluster id** names things (`hub`, `eu-west2`, `control`).
- **pool**: a GPU node group with one platform and preset. **GPU class**: the name a model asks for
  (`h100`, `b300`); a class can be served by several pools and regions. **profile**: the queue a model is
  admitted through, `prefer-<class>` or `default`.
- **tenant**: a customer or team with its own namespace, bucket and keys. **model**: a catalog entry, either
  an **endpoint** (always on, scales to zero) or a **run class** (a job template). **run**: one job.

## Day 2

- **Add a model**: a catalog entry (`catalog/models/<id>.yaml`, or `models.entries` in the tfvars) with
  `deployments.<cluster id>`; `./stack.sh apply platform <id>` then `./stack.sh apply models <id>`.
- **Add a region**: a `regions.<region>` block, then `./stack.sh apply`.
- **Add a tenant or a key**: `tenants.<name>`, then `./stack.sh apply models <id>` per cluster, control last.
- **Grow the weights filesystem or the cache**: change the size; a filesystem change rolls the GPU pools.
- **Rotate a secret**: every credential is a Terraform resource, so `-replace` it and re-apply the stage
  (`docs/OPERATIONS.md`, "Rotation and take-over").
- **Upgrade a chart**: the version in `clusters/common/apps/<component>.yaml`, then `./stack.sh apply platform <id>`.
- **Take over an existing fleet**: the same tfvars and state bucket credentials; `./stack.sh plan` shows no changes.

## Limits worth knowing

- Local NVMe exists only on the 8-GPU B300 preset today (`docs/FLEET.md`, "Local NVMe"); other presets
  reject `local_nvme = true`.
- InfiniBand pools need 8-GPU presets and at least one node up (`min_nodes >= 1`), and all pods of one
  multi-node run land in one pool. `docs/JOBS.md`, "Multi-node runs".
- A node scaled from zero advertises about 80% of its boot disk minus 32 GiB as ephemeral storage. Size
  `boot_disk_gib` for the largest emptyDir a pod asks for, or keep `min_nodes >= 1`.
- Reserved pools roll with zero surge; a full reservation cannot surge.
- Hostnames are `<service>.<ip>.sslip.io` until you set `edge.domain`.

## Cost

Idle, a control cluster plus two regions (six small system nodes, three 256 GiB filesystems, three static
IPs) costs about USD 2.4 per hour. GPU pools scale to zero unless `min_nodes` keeps spares; endpoints scale
to zero unless they have a floor. The whole fresh-deploy test (an H100 endpoint, a GROMACS run, B300 dispatch
tests, 3.5 hours) cost about USD 10 (`docs/VERIFICATION.md`).

## Destroying

`./stack.sh destroy` runs models, platform and cloud in reverse. With `protect_data = true` (the default)
the weights filesystems and tenant buckets refuse to be destroyed: set it to `false` and apply first, or
keep them and remove the rest. The state bucket is outside Terraform (`stack/bootstrap/state-bucket.sh`).

## Security model

Customers hold API keys with a budget, a model allow-list and a rate limit at the gateway. Endpoints are
reachable only with a key. Runs are Jobs in a tenant namespace with Pod Security `baseline`, no
capabilities, no privilege escalation, the default seccomp profile and a tenant-local network policy.
`edge.source_cidrs` limits who reaches the public listeners, `allowed_cidrs` who reaches the Kubernetes
APIs. Generated credentials live only in the state bucket (sensitive outputs) and in the clusters'
Secrets. `docs/SECURITY-PREREVIEW.md` lists what was reviewed and what is accepted.

## Troubleshooting

- `terraform` or `kubectl` times out on a cluster right after `apply cloud`: the IP you run from is not in
  that cluster's `allowed_cidrs`. Enforcement is per region; list your egress IP everywhere.
- `apply cloud` fails with `spec.local_disks.passthrough_group.requested is invalid`: the preset ships no
  local disks (see Limits).
- A `vpc.ipv4-address.public.count` quota error: the region's project has no free public IP for the gateway.
- A run stays `QUEUED` with no queue on its Workload: the catalog entry prefers a GPU class no pool declares;
  `preflight` reports such entries.
- `destroy platform <id>` waits on the Kueue release for ten minutes: delete the ClusterRoles
  `kueue-batch-admin-role` and `kueue-batch-user-role` on that cluster and it finishes.
- `cilium-operator` with one replica Pending: Nebius runs two; keep `system_pool.node_count = 2`.
- The first pull of a multi-GB model image in a region takes minutes (the cache syncs it once); the
  pre-pull warms catalog images per pool.

## Repository layout

| Path | Content |
|---|---|
| `terraform.tfvars.example`, `stack.sh`, `Makefile` | the one input, the stage driver, short names for the everyday commands |
| `stack/config` | the schema (`variables.tf` with validations) and the derived view shared by every stage |
| `stack/cloud`, `stack/platform`, `stack/models` | the three stages; `stack/modules/{cluster,tenant-region}` the reusable modules |
| `stack/bootstrap`, `stack/scripts` | state bucket bootstrap, preflight, small helpers |
| `clusters/common/apps`, `clusters/common/values`, `clusters/common/manifests` | the chart list with pinned versions, Helm values, shared manifests |
| `charts/fleet`, `charts/endpoint`, `charts/tenant` | queues and cache from the fleet definition; one endpoint; one tenant namespace |
| `catalog/models` | the bundled model catalog (endpoints and run classes) |
| `services/api`, `services/dispatcher`, `services/jobs`, `services/ops`, `ui` | the own code: API, dispatcher and price feed, job runner, ops image, console |
| `tools/` | build or copy the platform images, the quality gate, the library sync |
| `docs/` | architecture, API, jobs, scheduling, images, edge, observability, operations, requirements, verification, security pre-review |

## Everyday commands

`make help` lists them: `make venv` (once, the test virtualenv), `make check` (render everything and run the
tests, no cloud access), `make preflight`, `make plan`, `make apply`, `make destroy`, `make images`. Every
target is a script you can also call directly (`./stack.sh`, `tools/check.sh`, `tools/images.sh`).

## License

Apache License 2.0, like the rest of the Nebius Solutions Library (`LICENSE` at the repository root).

