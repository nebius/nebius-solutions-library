# Kubernetes inference fleet on Nebius

*For architects and platform engineers who deploy an inference platform for a customer, and for the
people who run it afterwards.*

This Terraform solution brings up a complete, multi-region GPU inference platform in a Nebius tenant from
one file, `terraform.tfvars`. The tfvars describe the infrastructure only; models are added afterwards
through the console or the API. You get one API for models, three ways to run them (always-on endpoints,
queued endpoint calls, long jobs with checkpoints), queueing across regions by cost, fast cold starts
through a per-region image cache, tenants with API keys and budgets, and the usual operations stack.
Everything inside the clusters is an upstream Helm chart or a small manifest; the own code is the API,
the cost dispatcher and the job runner (about 3,000 lines of Python and shell).

| You get | How it is built |
|---|---|
| One API for models: invoke, poll, fetch, cancel, resume | `services/api` behind API keys (LiteLLM) and an Envoy gateway; `docs/API.md` |
| Any container as a model; the platform ships none | a container description per model, given through the console or `POST /v1/models`, stored in the fleet database and rendered with `charts/endpoint` (KServe) or as a job template |
| Endpoints that scale to zero, with a warm floor if you want one, and warm spare nodes per pool (`warm_nodes`) that any model can start on without an instance boot | KServe on Knative, one `InferenceService` per model and region; placeholder pods of negative priority hold the spares |
| Queued endpoint calls and long jobs that survive spot preemption | Kubernetes Jobs with a checkpoint volume; Kueue admits them, `docs/JOBS.md` |
| Multi-node jobs over InfiniBand | JobSet on a pool whose nodes form a Nebius GPU cluster; optional per pool |
| N regions, one queue | Kueue MultiKueue on a control cluster; a dispatcher sends each job to the cheapest free pool of the GPU class the model prefers, `docs/SCHEDULING.md` |
| Fast pod starts | Zot pull-through cache plus Spegel peer-to-peer in every cluster, one logical registry host, pre-pull per pool, a shared weights filesystem; `docs/IMAGES.md` |
| Tenants | a namespace per cluster, a bucket and identity per region, API keys with budgets, Pod Security baseline, optional GPU quota and image allow-list |
| Spot, on-demand and reserved capacity | per pool: spot with a price cap, on-demand, or capacity blocks used first |
| Operations | Grafana, Prometheus, Loki, Alertmanager, OpenCost with Nebius prices, a managed PostgreSQL with the service's backups, spot-node recovery; `docs/OBSERVABILITY.md`, `docs/OPERATIONS.md` |

## When to use what: Nebius Serverless AI or this solution

| Question | Nebius Serverless AI (the managed service) | This solution (your own fleet) |
|---|---|---|
| What is it? | A service in the Nebius console: you give it a container, Nebius runs it. Nothing to install or operate. | A platform Terraform brings up inside your own Nebius projects: Kubernetes clusters, GPU pools, queues, one API. You (or your platform team) run it. |
| How long until the first call? | Minutes. | About 45 minutes, then minutes per model. |
| How do I define a model? | A form: image, command, GPU, scaling, environment. | A container description with the same kind of fields (image, args, port, protocol, GPU class, scaling, regions). |
| What runs? | Always-on containers behind an HTTP endpoint that scale to zero. | The same endpoints, plus queued calls and long jobs with checkpoints that survive spot preemption, plus multi-node jobs over InfiniBand. |
| Which GPUs? | One platform and preset per endpoint. | A preferred GPU class with fallbacks per model; spot, on-demand and reserved pools, in several regions, the cheapest free one wins. |
| Who are the users? | You and your Nebius project members. | Your tenants: namespaces, API keys with budgets and model allow-lists, GPU quotas, rate limits. |
| Where do the data and the network live? | In the service. | In your projects, your subnets, your buckets, behind your source-IP allow-lists; private gateways are an option. |
| What do I pay for? | Usage, per the service's price list. | The nodes of your clusters (idle GPU pools scale to zero) plus a small control plane; cost reports per key and per run. |
| Operations? | None. | Grafana, Prometheus, Loki, OpenCost, backups and spot recovery are installed; upgrades and incidents are yours. |
| Pick it when... | You want one or a few models online quickly and do not want to run anything. | You run a platform for several teams or customers, mix online and batch work, need placement control, reserved capacity, private networking or your own observability, and are fine operating Kubernetes. |

The two are not exclusive: a team can start on Serverless AI and move to this fleet when it needs
queues, tenants or multi-node jobs; a model is a container plus a few knobs in both. Check the
Serverless AI documentation for its limits of the day; this table compares the shapes of the two offerings.

A note on names: `serverless2` in image paths, label keys (`serverless2.nebius/...`), gateway and Service
names is this platform's working name, the second generation of the serverless inference platform it grew
out of. It is not Nebius Serverless AI, the managed service the table compares with.

## How it is put together

```
terraform.tfvars ──▶ ./stack.sh ──▶ cloud      one state: clusters, pools, filesystems, static IPs, identities, registry, buckets, the managed database
                                ──▶ platform   one state per cluster: Helm releases and manifests (workers first, then control)
                                ──▶ models     one state per cluster: tenants, API keys, the acceptance probe (it creates the example model through the API)
```

- **Control cluster** (CPU only, the default): the customer API, LiteLLM, the console, the Kueue MultiKueue
  manager with the cost dispatcher and price feed, Grafana. Next to it, in the same project and region, the
  fleet database: one Nebius Managed Service for PostgreSQL cluster (`<fleet>-db`, private access only) with
  the databases `platform` (model definitions and their history, owned by the API) and `litellm` (keys,
  budgets, spend). `control_plane.dedicated = false` is the single-cluster mode for a proof of concept: those
  services run on the one region's system pool.
- **Region clusters** (one per Nebius region, one project each): GPU pools, Knative/KServe endpoints, an
  Envoy gateway with key checks, a regional API, the image cache, DCGM, OpenCost, Loki, the weights filesystem.

`docs/ARCHITECTURE.md` has the component diagram and the request paths; `docs/REQUIREMENTS.md` maps every
requirement to the mechanism that implements it.

## Prerequisites

- A Nebius tenant with one project per region (the control cluster can share the first region's project),
  a subnet in each, and quotas for the pools you plan: GPUs per platform, network-ssd for filesystems and
  caches, one public IP per cluster, one managed PostgreSQL cluster (`msp.postgres.count`) per fleet.
- The control plane in a region that offers Managed Service for PostgreSQL: eu-north1, eu-west1, eu-west2,
  me-west1, us-central1 or uk-south1 (`stack/config/variables.tf` checks it; eu-south1 cannot host the
  control plane, it can still be a GPU region).
- The Nebius CLI logged in with `editor` on those projects (`nebius profile activate <name>` picks the
  profile; `./stack.sh` takes an access token from it with `nebius iam get-access-token` and hands it to
  Terraform as `NEBIUS_IAM_TOKEN`, the same way as the other solutions in this library). Terraform 1.12 or newer with
  the Nebius provider `nebius/nebius` from the public registry (floor `>= 0.6.23`, the newest floor in this library;
  validated with 0.6.67), `helm`, `kubectl`, and `docker buildx` or `crane` for the platform images. The AWS CLI (`aws`) only for
  `./stack.sh destroy` with `protect_data = false`: it empties the buckets Terraform is about to delete.
- Optional: an NVIDIA NGC key for NIM images, a Hugging Face token for gated weights. They are read from
  environment variables, never written into the tfvars.

## Quick start

About 45 minutes from an empty project to the first model call. Every step is one command.

```sh
cp terraform.tfvars.example terraform.tfvars   # 1. name, projects, subnets, pools, tenants
stack/bootstrap/state-bucket.sh                # 2. once: the Terraform state bucket and its credentials
./stack.sh preflight                           # 3. every pool against the Nebius compatibility matrix and quotas
./stack.sh apply cloud                         # 4. clusters, pools, filesystems, IPs, identities, registry, the database (about 15 min)
tools/images.sh build                          # 5. build and push the five platform images to the fleet's registry
./stack.sh apply                               # 6. platform on every cluster, then tenants and the acceptance probe (it creates the example model)
./stack.sh output models control               # 7. URLs; tenant keys: ./stack.sh output models control -json tenant_keys
```

The platform ships no models, only three generic job classes (`hello-run`, `container-run`,
`distributed-run`). A model is a container you describe: any image from Docker Hub, NGC, GHCR, Quay or the
fleet's own registry, pulled through the per-region cache. Nothing about a model is in the tfvars: you add
models through the console ("New model") or `POST /v1/models` with an admin key, and the platform keeps
them in its database. The acceptance probe creates the first one for you, `llm-example` (a stock
`vllm/vllm-openai:v0.11.0` container serving a small public chat model, `HuggingFaceTB/SmolLM2-360M-Instruct`, on one GPU of the fleet's first
GPU class, scaled to zero when idle), calls it, and leaves it as the first model of the console. Then call
a model yourself:

```sh
API=$(./stack.sh output models control -raw api_url); KEY=<a tenant key from step 7>
curl -H "Authorization: Bearer $KEY" $API/v1/models
curl -X POST -H "Authorization: Bearer $KEY" -H 'content-type: application/json' \
  -d '{"mode":"run","input":{"message":"hello"}}' $API/v1/models/hello-run:invoke
curl -X POST -H "Authorization: Bearer $KEY" -H 'content-type: application/json' \
  -d '{"mode":"sync","input":{"messages":[{"role":"user","content":"Say hello."}],"max_tokens":16}}' $API/v1/models/llm-example:invoke
```

`./stack.sh apply` runs the stages in dependency order. Each stage can also run alone (`./stack.sh plan
platform hub`), every state lives in the bucket, and `./stack.sh tf <stage> [<id>] -- <args>` runs any
Terraform command against a stage. `make check` renders everything and runs the tests without touching
the cloud (run `make venv` once first: it creates the Python virtualenv the tests use); CI runs the same gate.

## What is the platform and what is not

The platform is the fleet: clusters, pools, queues, the image cache, the API, tenants, keys, endpoints and
jobs from a container description, observability and cost. It knows nothing about any particular model.

Everything that is specific to a domain sits on top, as an application layer that brings its own models: a
scientific-AI platform with its docking, speech and molecular-dynamics containers, a chat product with its
LLMs, a team's fine-tuning jobs. Such a layer is not part of this solution; it talks to the platform
through the API only.

## The one file

`terraform.tfvars.example` explains every key in place; the schema with its validations is
`stack/config/variables.tf`.

| Section | What it defines |
|---|---|
| `name`, `labels`, `protect_data` | prefix of every cloud resource, labels on it, the destroy guard for data |
| `control_plane` | the dedicated CPU cluster (project, subnet, system pool, API allow-list), or single-cluster mode; `database`: the managed PostgreSQL next to it (preset, disk, hosts, backup retention) |
| `regions.<region>` | project, subnet, weights filesystem, `pools`: platform, preset, GPU class, capacity (spot with price cap, on-demand, reserved), min and max nodes, local NVMe, InfiniBand |
| `images` | the logical registry host, the source registry, cache size and retention, upstream registries, platform image tags |
| `prices` | USD per GPU-hour per platform: the scheduling order and the cost reports |
| `edge` | public or internal gateways, optional domain, allowed source CIDRs, ACME email, API rate limit |
| `observability` | retention, alert webhook, cost export |
| `secrets` | the names of the environment variables that carry NGC, Hugging Face and registry credentials |
| `tenants.<name>` | regions, output retention, keys with budgets, optional GPU quota, fair share, dedicated pools, image allow-list |
| `acceptance` | the probe that runs after the models stage: one run of a built-in class and, by default, the example endpoint `llm-example` created through the API and called once |

There is no `models` section: the tfvars describe infrastructure only. Models are runtime input of the
platform ("Day 2" below).

### Vocabulary

- **fleet**: everything one `terraform.tfvars` describes. **region**: one Nebius region, one project, one
  cluster; its short **cluster id** names things (`hub`, `eu-west2`, `control`).
- **pool**: a GPU node group with one platform and preset. **GPU class**: the name a model asks for
  (`h100`, `b300`); a class can be served by several pools and regions. **profile**: the queue a model is
  admitted through, `prefer-<class>` or `default`.
- **tenant**: a customer or team with its own namespace, bucket and keys. **model**: a container you
  describe, either an **endpoint** (always on, scales to zero) or a **run class** (a job template). **run**: one job.

## Day 2

- **Add a model**: no Terraform. Open the console (`app.<control host>`) and fill in "New model", or
  `POST /v1/models` on the fleet's API (the control cluster's `api.` host) with an admin key: a container
  plus a few knobs (image, command and args, env, port, protocol `openai | http | websocket | grpc`, GPU
  count and classes, scaling, regions, weights; the full list is the docstring of `services/api/models.py`).
  The API stores the definition in the fleet database with its history, renders the endpoint on every
  cluster of the model's regions with `charts/endpoint`, and registers an OpenAI endpoint as a LiteLLM
  model group. `PUT /v1/models/<id>` changes it, `DELETE` removes it, `GET /v1/models/<id>/history` lists
  the versions. Only the fleet API writes; a regional API answers a write with 409 and the fleet API's URL.
  `docs/API.md` "Models".

  ```sh
  curl -X POST -H "Authorization: Bearer $ADMIN_KEY" -H 'content-type: application/json' $API/v1/models -d '{
    "id": "my-llm", "image": "vllm/vllm-openai:v0.11.0",
    "args": ["--model", "<org>/<model>", "--port", "8000"],
    "port": 8000, "protocol": "openai", "served_model": "<org>/<model>",
    "gpu": {"count": 1, "classes": ["h100"]}, "scaling": {"min": 0, "max": 2}}'
  ```
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
IPs) costs about USD 2.4 per hour, plus the managed PostgreSQL cluster (one `cpu-e2` 2 vCPU / 8 GiB host
with 64 GiB network-ssd by default: tens of USD per month, see the Nebius price list). GPU pools scale to zero unless `min_nodes` keeps spares; endpoints scale
to zero unless they have a floor. The whole fresh-deploy test (an H100 endpoint, a GPU batch run, B300 dispatch
tests, 3.5 hours) cost about USD 10 (`docs/VERIFICATION.md`).

## Destroying

`./stack.sh destroy` runs models, platform and cloud in reverse. With `protect_data = true` (the default)
the weights filesystems and tenant buckets refuse to be destroyed: set it to `false` and apply first, or
keep them and remove the rest. Nebius refuses to delete a bucket that still holds objects, so the destroy
empties the disposable buckets first (`stack/scripts/empty-bucket.sh`, needs the AWS CLI) and removes every
image from the fleet's registry (`stack/scripts/empty-registry.sh`), which Nebius refuses to delete otherwise.
The state bucket is outside Terraform (`stack/bootstrap/state-bucket.sh`).

## Security model

Customers hold API keys with a budget, a model allow-list and a rate limit at the gateway. Endpoints are
reachable only with a key. Runs are Jobs in a tenant namespace with Pod Security `baseline`, no
capabilities, no privilege escalation, the default seccomp profile and a tenant-local network policy.
`edge.source_cidrs` limits who reaches the public listeners, `allowed_cidrs` who reaches the Kubernetes
APIs. Generated credentials live only in the state bucket (sensitive outputs) and in the clusters'
Secrets. The fleet database has no public access and is reached only from the control cluster's VPC; its
password is Terraform-generated and lives in Kubernetes Secrets; the control API is its only writer of
model definitions.

## Security notes and limitations

This is a template for a platform team, not a hardened multi-tenant service. What it does not do, so
that nobody assumes it does:

- **Shared GPU nodes.** Tenant containers (runs and endpoints) share the GPU nodes of a region. Each pod
  runs under Pod Security `baseline` with every capability dropped, no privilege escalation and the
  runtime's default seccomp profile, in its own namespace behind a tenant-local network policy; the model
  container runs as the user its image sets, root included, unless the job class sets `runAsNonRoot`.
  That is container isolation, not VM isolation: a kernel or driver escape would expose the node to every
  tenant on it. Run images you trust, or give a tenant its own pools.
- **Operator UIs.** Grafana uses a password login behind the source-IP
  allow-list `edge.source_cidrs`; there is no single sign-on, no second factor and no audit trail beyond the
  tools' own logs. Keep the allow-list tight.
- **Traffic inside the clusters is not encrypted.** TLS ends at the public listeners; calls from the
  gateway to LiteLLM, the API and the model pods, and between the control cluster and the workers over
  the Kubernetes APIs, cross the VPC in the clear except where the endpoint itself is TLS (Kubernetes API
  servers, the managed database). No service mesh is installed.
- **No scanning of user images.** CI scans the five platform images with `trivy`; the images tenants
  register through the API are pulled and run as they are. Scan them before you register them.
- **Not reviewed for customer-facing use.** A formal security review is due before anyone outside your
  organisation gets a key.

## Troubleshooting

- `terraform` or `kubectl` times out on a cluster right after `apply cloud`: the IP you run from is not in
  that cluster's `allowed_cidrs`. Enforcement is per region; list your egress IP everywhere.
- `apply cloud` fails with `spec.local_disks.passthrough_group.requested is invalid`: the preset ships no
  local disks (see Limits).
- A `vpc.ipv4-address.public.count` quota error: the region's project has no free public IP for the gateway.
- A run stays `QUEUED` with no queue on its Workload: the catalog entry prefers a GPU class no pool declares;
  `preflight` reports such entries.
- `destroy platform <id>` seems stuck on `kueue-batch-admin-role`/`kueue-batch-user-role`: the destroy
  removes those two aggregated roles itself after the Kueue release (`stack/scripts/kueue-uninstall-cleanup.sh`);
  on a fleet created before 2026-10-09 delete them by hand once.
- `cilium-operator` with one replica Pending: Nebius runs two; keep `system_pool.node_count = 2`.
- The first pull of a multi-GB model image in a region takes minutes (the cache syncs it once); the
  pre-pull warms the images of the built-in classes per pool, a model's image is cached at its first pull.
- `POST /v1/models` answers 409 "this API has no model database": you called a regional API. Models are
  written on the fleet's API (the control cluster's `api.` host), which the message names.

## Repository layout

| Path | Content |
|---|---|
| `terraform.tfvars.example`, `stack.sh`, `Makefile` | the one input, the stage driver, short names for the everyday commands |
| `stack/config` | the schema (`variables.tf` with validations) and the derived view shared by every stage |
| `stack/cloud`, `stack/platform`, `stack/models` | the three stages; `stack/modules/{cluster,tenant-region}` the reusable modules |
| `stack/bootstrap`, `stack/scripts` | state bucket bootstrap, preflight, small helpers |
| `clusters/common/apps`, `clusters/common/values`, `clusters/common/manifests` | the chart list with pinned versions, Helm values, shared manifests |
| `charts/fleet`, `charts/endpoint`, `charts/tenant` | queues and cache from the fleet definition; one endpoint; one tenant namespace |
| `catalog/models` | the generic job classes (`hello-run`, `container-run`, `distributed-run`); models are defined through the API and stored in the fleet database |
| `services/api`, `services/dispatcher`, `services/jobs`, `services/ops`, `ui` | the own code: API, dispatcher and price feed, job runner, ops image, console |
| `tools/` | build or copy the platform images, the quality gate, the library sync |
| `docs/` | architecture, API, jobs, scheduling, images, edge, observability, operations, requirements, verification, security pre-review |

## Everyday commands

`make help` lists them: `make venv` (once, the test virtualenv), `make check` (render everything and run the
tests, no cloud access), `make preflight`, `make plan`, `make apply`, `make destroy`, `make images`. Every
target is a script you can also call directly (`./stack.sh`, `tools/check.sh`, `tools/images.sh`).

## License

Apache License 2.0, like the rest of the Nebius Solutions Library (`LICENSE` at the repository root).

