# Verification record

*For reviewers: what was measured, where and when, written without the names of the application-layer
models the reference fleet uses as its acceptance tests. Every number comes from a real cluster. The ids,
hostnames and IP addresses are those of the reference fleet and of temporary test fleets; a fleet you
deploy has its own. The full, model-by-model record of the reference fleet stays with that fleet's
maintainers; it is not part of this solution.*

Test workloads used throughout: `hello-run` (a CPU job), `container-run` (any container as a job; a
checkpoint loop and `sleep` commands), `distributed-run` (a two-node NCCL all-reduce), a molecular
dynamics solver as the GPU batch job (185k atoms, 5000 steps, about 3 minutes on one H100), a NIM
image as the large private-registry endpoint (16 GB), a streaming WebSocket endpoint, and a 0.5B
parameter chat model served by vLLM as the OpenAI endpoint.

## Scaling buffer (2026-10-09, late evening, the same test fleet)

`scaling.buffer = 1` on the hub's endpoint (`max 2`, otherwise scale to zero), three request loops for
three minutes, the dispatcher's cycle at 15 s: the floor rose from 0 to 2 (demand 1 + buffer 1) 15 s into
the load, the second replica started on the warm spare node and was serving 2 min later (the model's image
was not on that node yet), the floor stayed at 2 through the whole load, and 3 min after the last request
(the 120 s cooldown measured from the first quiet sample) it returned to 0; the model then scaled to zero.
Changing the buffer through the API rolls no revision (the dispatcher reads the model copy). The first
build dropped the floor for 30 s on a single quiet sample mid-load; the hysteresis (demand must stay low for
a whole cooldown) removed that. Dispatcher 0.2.6, API 0.10.3, 26 dispatcher unit tests.

## Redeployment from the merged copy: four spot regions, warm spare nodes (2026-10-09 evening, five clusters)

The test fleet was destroyed and deployed again from this directory as merged (the shared-components refactor
plus the large-model fixes and the review pass), with a control cluster and four GPU regions, every GPU pool on
spot: eu-north1 H100 (1-GPU pool with a price cap and one warm spare node, two whole-node pools, one of them on
InfiniBand), eu-west2 B300 with local NVMe, eu-west1 H200, us-central1 B200.

| Check | Result |
|---|---|
| Fresh deployment | cloud stage 90 resources (five clusters, database, registry, buckets); platform 120 resources per worker; models stages; acceptance probe passed (hello-run, then the example endpoint on a B200 spot node answered) |
| Spot in four regions | one 1-GPU `container-run` per region, submitted together, all SUCCEEDED on a spot node booted from zero: eu-west1 H200 about 1 min, us-central1 B200 3.5 min, eu-north1 H100 6 min, eu-west2 B300 8 min |
| Warm spare node (`warm_nodes = 1`) | the `cluster-overprovisioner` placeholder held one H100 node; a new endpoint's pod preempted it and started there at once, the placeholder went Pending and the autoscaler added the next spare in 4 min. First call 609 s (one-time pull of the 10 GB vLLM image); after scale to zero, the next cold start answered in **68 s** (a node boot alone is 3 to 6 min on these pools) |
| Idle endpoints stay idle | with `disable_model_info_refresh` the gateway log shows no more `GET /v1/models` from LiteLLM; a scaled-to-zero endpoint stayed down (before: woken every 5 min) |
| Console | models, endpoints with live replica counts, jobs with metrics and logs, the multi-node job form; UI tests 15, API tests 100 |

Found and fixed on this deployment: `verify-full` against the managed database failed (`certificate verify
failed`) until the Nebius MSP CA was fetched and mounted; a region applied before the hub rendered empty
storage credentials and its uploader failed (stage order: the hub's models stage first); a 1-GPU run on the
hub booted an 8-GPU node because the whole-node pool's per-GPU spot price was lower than the 1-GPU pool's cap
(queue flavors and the dispatcher order pools by the cost of the run now); the acceptance probe gave up before
a first image pull in a fresh region finished (20 min now); the KServe CRD uninstall hung on endpoints defined
through the API (drained first now); bucket emptying ran after the key had lost its group (ordered now); a
region whose tenant-wide `compute.disk.count` quota is exhausted cannot host the platform (me-west1 that day;
the preflight reports the headroom now). Cost of the evening: about 2 GPU-hours of spot across the four
regions plus the fleet's idle cost, under 10 USD.

## Fresh test fleet with the managed database, spot pools and large models (2026-10-09, three clusters, two GPU regions)

A second fleet was deployed from the pull request's copy of this directory, with the managed database and
spot pools only, and then extended day-2 three times through `terraform.tfvars` (`./stack.sh apply cloud`,
then `./stack.sh apply`), without touching anything by hand:

| Step | Result |
|---|---|
| Fresh deployment: control cluster + hub region (eu-north1), Managed PostgreSQL cluster (`platform` and `litellm` databases), one H100 spot pool (1 GPU per node, price cap, 0 to 2 nodes) | every stage in one pass; the acceptance probe created its example endpoint through the API, called it and left it as the console's first model |
| Day-2: a B300 spot region (eu-west2, the 8-GPU preset with six local NVMe disks, following the spot price, 0 to 1 node) | cloud stage 15 resources in 14 min, platform and models stages 35 min; MultiKueue worker connected, queue `prefer-b300` present |
| `container-run` with `scratch: local-nvme`, 8 B300 GPUs | node from zero in 6 min, pod affinity `local-nvme=true`, `emptyDir` on the node's 21 TiB NVMe (6 x 3.84 TB), 6.7 GB/s write; the spot node drew on `compute.instance.preemptible.count` only |
| Preemption: the B300 VM stopped from the cloud side at checkpoint 12 of a checkpoint loop | the ops job restarted the VM in 1 min, the run resumed from the last checkpoint, both attempts on the operation |
| Day-2: two whole-node H100 spot pools (`h100-spot-8x`, 0 to 1; `h100-spot-8x-ib`, 0 to 2 in one InfiniBand fabric), weights filesystem 256 GiB to 1 TiB | cloud stage 7 min 30 s (the InfiniBand node boots inside the apply); Kueue flavors and quotas present before the platform stage ran |
| 8 GPUs: a 72B chat model (145 GB of weights, vLLM tensor-parallel 8) as an endpoint through `POST /v1/models` | cold start 6 min 3 s with nothing cached (image pull 3 min, weights 56 s, engine 75 s), 2 min 19 s with image and weights cached, 133 s end to end through `:invoke` from zero pods; answers in 0.7 to 1.4 s; 8 parallel requests x 256 tokens: 567 tokens/s; scaled to zero 3 min after the last request; removed with `DELETE /v1/models/{id}` |
| 16 GPUs: a 235B mixture-of-experts model (470 GB) as a `distributed-run` over InfiniBand (Ray + vLLM, tensor-parallel 8 x pipeline-parallel 2) | admitted in 7 s, both pods on InfiniBand nodes with `ib-8` claims; NCCL `Using network IB`, GPU Direct RDMA on all 8 HCAs per node, pipeline channels `via NET/IB` between the pods; weights loaded in 10 min from the shared filesystem into 16 workers; correct answers in 0.1 to 0.7 s; 16 parallel requests x 256 tokens: 740 tokens/s; `:cancel` ended the run and both nodes scaled down |

What the test fleet found and this copy now carries: the tenant chart renders the InfiniBand claim templates
and the shared scratch claim from the fleet (not from a tenant field); the control cluster carries the claim
templates and Kueue's `deviceClassMappings` too (a multi-node run stayed queued without them); growing the
weights filesystem day-2 no longer fails the platform stage; an endpoint with N GPUs is pinned to a pool
whose preset has at least N (an 8-GPU replica on a 1-GPU preset was unschedulable for ever); Knative collects
never-ready revisions after an hour; the LiteLLM route waits for a cold start like the API route; the
dispatcher's orphan sweeper touches only a run's own work volume (it deleted a tenant's shared claim once);
cancelling a running multi-node run deactivates its Kueue Workload (the suspend alone was undone); the
uploader skips the run's checkpoint directory (32 GB of a model's weights reached the bucket once); the
dispatcher's price feed asks for a VM following the spot price (the live spot-price map was empty); the Kueue
flavor of an NVMe pool declares the `local-nvme` label; a preempted attempt is on the operation from its
first second.

Limits measured on the same fleet: LiteLLM's model-info refresh (`GET /v1/models` on every endpoint every
5 min) woke scaled-to-zero endpoints, so an idle 8-GPU model ran most of the time (switched off since:
`disable_model_info_refresh`); the cluster
autoscaler never adds InfiniBand nodes (its simulated node has no DRA attributes), so an InfiniBand pool's
`min_nodes` is set to the node count of the next multi-node run and back to 0 afterwards; a run class has no
`weights` mount, so a multi-node run downloads or copies its weights into its own checkpoint path; a run's
image is used as given (callers name the cache's form of the reference themselves); there is no endpoint
form for a model over several nodes yet.

Cost of the day: about 42 GPU-hours of H100 spot (0.79 USD per GPU-hour) and 7 GPU-hours of B300 spot
(0.99), roughly 42 USD.

## Fresh deployment from `terraform.tfvars` (2026-10-08, three clusters, two GPU regions)

A second fleet was brought up from nothing with the Terraform solution in the owner's projects, never
touching the reference fleet, and destroyed again: a dedicated control cluster in eu-north1, a region
in eu-north1 with one `gpu-h100-sxm` 1-GPU spot pool (price cap 2.15), a region in eu-west2 with one
`gpu-b300-sxm` 1-GPU spot pool, 256 GiB weights filesystems and caches, one tenant with one key, the
generic job classes plus a chat endpoint and the batch job, the acceptance probe on.

| Step | Result |
|---|---|
| state bucket, preflight | bucket created; preflight passes (platform/preset/driver matrix, state bucket, catalog classes, local-NVMe rule) |
| `apply cloud` | control and hub clusters, pools, identities, registry, buckets in about 12 min; a third region (eu-south1) could not get a public IP (project quota 3/3) and was replaced by eu-west2 |
| local NVMe | `local_nvme = true` is rejected by the API for 1-GPU presets (`spec.local_disks.passthrough_group.requested is invalid`); only the 8-GPU B300 preset ships local NVMe (table in `docs/FLEET.md`); the mount path is plan-validated, not exercised |
| `apply platform` on the second region and on control | one pass each, 106 and 103 resources, about 9 min each; 78 and 62 pods Running, Let's Encrypt certificates READY, static gateway IPs, `healthz` 200, Grafana 200, MultiKueue workers CONNECTED. The first region needed three ordering fixes now in the code (Prometheus CRDs before ServiceMonitor-bearing charts, wait for envoy-gateway and the cache, the cache after the fleet chart) |
| `apply models` | tenant namespaces with Pod Security `baseline`, a bucket and identity per region, Secrets, the chat endpoint READY after about 10 min (H100 spot node from zero in 2 min, image pull, vLLM init), LiteLLM key for the tenant, acceptance probe Job succeeded (hello-run QUEUED to SUCCEEDED in under 3 min, a chat completion through the edge) |
| runs through the customer API | hello-run dispatched by cost to the cheaper B300 region (node scaled from zero), SUCCEEDED; `container-run` with `scratch: local-nvme` rendered the NVMe affinity and stayed Pending as it should without such a pool; the batch job dispatched to the H100 region, 5000 steps, SUCCEEDED, artifacts in the tenant bucket, billed at the region's price |
| destroy | models, platform and cloud states in reverse order; two stops found and fixed (a worker's models state read the hub's already-deleted state; Kueue's uninstall waited on aggregated ClusterRoles) and two Nebius rules documented (a registry with artifacts and a bucket with objects refuse deletion). Afterwards no cluster, node group, filesystem, IP, GPU cluster, registry, service account or bucket of the test prefix remained |
| InfiniBand pools, plan only | `interconnect = "infiniband"` + `infiniband_fabric` on an 8-GPU pool plans one `nebius_compute_v1_gpu_cluster`, the node group's `template.gpu_cluster` and the node and Kueue flavor label `serverless2.nebius/interconnect = infiniband` |
| cost | about USD 10 for the 3.5-hour test (upper bound: spot H100 counted at its cap); idle, the three-cluster fleet costs about USD 2.4 per hour, GPU minutes only while a run or an endpoint holds a node |

## Fresh deployment from the library copy, as a new user (2026-10-08, one GPU region)

The exact directory that goes into the Nebius Solutions Library was copied to a scratch directory and the
README quick start followed: a dedicated control cluster and one region (eu-north1, one 1-GPU H100 spot
pool), 256 GiB filesystem and caches, one tenant, the acceptance probe.

| Step | Time | Result |
|---|---|---|
| state bucket, preflight | 20 s | ok |
| `apply cloud` | 14 min | 29 resources |
| `tools/images.sh build` | 4 min 40 s | the five platform images pushed to the new registry |
| `apply` (platform on both clusters, models on both) | 19 min 32 s | platform 106 and 111 resources in one pass each |
| acceptance probe | 2 min | hello-run SUCCEEDED in 71 s; the chat endpoint answered through the edge after a 502 while it scaled from zero |
| runs through the public API | 5 min for all three | hello-run 49 s; `container-run` (`sleep 30`) 92 s; the batch job 191 s on the H100 spot node, all SUCCEEDED |
| chat call without `model` in the body | 31 s | HTTP 200, the served model name injected by the API |
| console `/config.json`, Grafana | | API URL and Grafana URLs served at runtime; Grafana 200 on both clusters |
| `make check` inside the copy | | every stage and chart renders, all unit tests pass |
| destroy | about 35 min | clean after the fixes below |
| cost | | about USD 5 |

Defects this run found, all fixed: the example ACME e-mail address used a domain Let's Encrypt refuses, so
no certificate was ever issued on a fresh fleet (the schema now rejects placeholder domains); destroy
stopped on non-empty buckets and a non-empty registry (destroy-time cleanup with the bucket's own key);
Kueue's uninstall never finished because the aggregation controller re-creates its roles (installed and
uninstalled without Helm's wait, with an explicit readiness wait and a cleanup step); the state-bucket
bootstrap failed on a second run (a wrong key in the membership check).

## Multi-node runs over InfiniBand (2026-10-08, reference fleet, temporary pool of 2 x 8 H100)

| Check | Result |
|---|---|
| control path | `distributed-run` with `nodes: 2` and no GPUs: one JobSet of two indexed pods, one Kueue Workload with a pod set of 2 admitted on the preferred class, rank environment (`MASTER_ADDR`, `NODE_RANK`, `NNODES`, `WORLD_SIZE`) correct in both pods, rank 0 uploads outputs, both pods write attempt records, SUCCEEDED in 75 s |
| fabric NICs in pods | the node group needs `gpu_settings.dra = true`: Nebius's DraNet driver then publishes the 8 NDR ports per node and a pod claims them through a `ResourceClaimTemplate`; without it the NICs exist on the node but no claim can be satisfied |
| scale from zero | the autoscaler cannot satisfy DRA claims for an empty pool; InfiniBand pools require `min_nodes >= 1` (validated in the schema) |
| NCCL over the fabric | `NET/IB` virtual devices on every rank, GPU Direct RDMA enabled; a 1 GiB all-reduce over 16 GPUs on 2 nodes at **436 GB/s** bus bandwidth (Nebius calls above 300 stable) |
| preemption | restart-all exercised twice: a lost rank restarts the whole set from the last checkpoint |
| cost | about USD 13 to 34 for about 80 minutes of 2 x 8 H100 spot; pool, GPU cluster and pricing policy destroyed afterwards, Terraform plan clean |

## Privileges of InfiniBand jobs (2026-10-08, same hardware)

| Step | Result |
|---|---|
| a pod on a node as the image ships it | `Max locked memory 8 MiB`; with every capability dropped, RDMA memory registration fails (`ibv_reg_mr`: cannot allocate memory) and the run restarts until it fails |
| fix | `LimitMEMLOCK=infinity` on the container runtime of GPU nodes, written by the GPU pools' cloud-init at boot (and by the node-config DaemonSet for older nodes) |
| the same run on the fixed nodes | every capability dropped (`CapEff 0`), namespace at Pod Security `baseline`, no added capability: `Completed`, 0 restarts, **432 GB/s** |
| what this means | the platform never renders a privileged pod, hostNetwork, hostPID, hostPath or an added capability for InfiniBand runs; runner sidecars run as a non-root user; the model container keeps its image's user |
| a pitfall | restarting containerd on nodes that already run InfiniBand pods took the runtime down until the pool rolled; roll pools through Terraform instead |

The limits of multi-node runs are in `docs/JOBS.md` "Limits of multi-node runs" (one run = one pool = one
GPU cluster = one fabric; 8-GPU presets only; `min_nodes >= 1`; exact-count NIC claims; restart-all on
preemption; the fabric table per platform and region).

## Per-GPU images for runs (2026-10-09, reference fleet)

A run class carrying one image per GPU class (`job.images`, same digest under two tags: the mechanism is
what is measured):

| Check | Result |
|---|---|
| ranking | `GET /v1/rank` on the dispatcher returns the best (region, pool, class) first: the cheaper RTX PRO 6000 spot pool, then H100 |
| unpinned run | rendered with the RTX class's image, queued on that class's profile queue, admitted in the RTX region, the worker pod carries that image and the class label, SUCCEEDED |
| the same run pinned to the H100 region | the ranking honours the pin, the H100 image is rendered, admitted on the hub, SUCCEEDED; the operation reports `gpu_class: h100` |
| a single-image class | unchanged: no class restriction, free class switching while queued |

## Scheduling, placement and resume (2026-10-07, reference fleet)

| Case | Result |
|---|---|
| cost-aware cross-region placement | an unpinned job goes to the cheapest free pool of its class order (measured: the RTX PRO 6000 spot region at $0.95/GPU-h over the H100 region at $2.15); a pinned region is honoured |
| queue-time re-dispatch | with the cheapest region's queue on hold, the dispatcher added the next region after 300 s and MultiKueue ran the job there |
| spot loss and resume | a running job's pod deleted mid-run: the replacement pod started on the same regional volume, skipped the input fetch, and continued from its checkpoint (`start at n=9`); cancel, then `:resume`, continued on the same volume as a new attempt in the same region |
| preemption by endpoints | an endpoint scaling up preempted a batch pod; the attempt record in the bucket is the history, billing sums all attempts |
| MultiKueue admission | the external dispatcher must write `nominatedClusterNames` with server-side apply as field manager `kueue-admission`; a merge patch leaves a field Kueue cannot clear and the manager Workload never becomes Admitted (fixed in the dispatcher, measured before and after) |
| async endpoint calls | a queued call is a Job that waits for the endpoint's cold start and stores the response in the bucket; calls go through the gateway with the caller's key (a direct call from a tenant pod to an endpoint is blocked by the network policy) |
| API-defined endpoints through LiteLLM | an OpenAI endpoint defined through the model API is a LiteLLM model group that calls the gateway as the platform-internal key; a chat completion with a tenant key through LiteLLM and through the API's own sync path both answered (measured 2026-10-09 after the first version, which registered the group with a dummy key, failed with 401 at the edge) |

## Images, cache and cold starts (2026-10-07, reference fleet)

| Case | Result |
|---|---|
| mirror configuration on nodes | GPU node images ship containerd without `config_path`; the GPU pools' cloud-init writes the drop-in and the mirror file for the logical registry host at boot, the node-config DaemonSet repairs older nodes (a probe pull through the logical host decides) |
| cold pulls through the cache | a 2 MB image from Docker Hub 0.9 s; the 44 MB runner image 2.4 s (hub) and 4.6 s (second region); a 518 MB solver image 20.9 s cold, 12.3 s on a fresh spot node with the cache warm, 7.8 s re-pull via a peer |
| a 16 GB NIM image | 9 min 38 s for the cache to fetch it from NGC the first time; a second GPU node of the same cluster got it in 3 min 27 s from the cache and its peer |
| weights on the shared filesystem | the NIM's cold start fell from 250 s to 28 s with its cache on the filesystem; the chat model's weights load in under a second from the mount, the rest of its cold start is engine initialisation |
| fresh node to running job container | about 2 min 5 s |

## Platform health checks (2026-10-07 and 2026-10-08, reference fleet)

- Alertmanager delivers to the in-cluster sink (no external receiver by decision); the Watchdog heartbeat proves the path.
- The DCGM exporter cannot watch its profiling fields on Nebius L40S VMs; a second release without those fields serves L40S nodes, Prometheus scrapes it.
- Tenant isolation: a pod in a tenant namespace cannot reach an endpoint's in-cluster service (timeout) and gets 401 from the gateway without a key; every run pod has seccomp `RuntimeDefault`, all capabilities dropped and no privilege escalation; tenant namespaces enforce Pod Security `baseline`.
- Terraform plan is clean on every cluster after each change; state lives in a versioned Object Storage bucket.

## Earlier findings (2026-10-05)

Claims checked against Nebius docs and the CLI and against live behaviour while choosing the components:
Karpenter on Managed Kubernetes unusable in practice; KServe scales to zero only with Knative for generic
containers; the Nebius load balancer rejects `externalTrafficPolicy: Local`; nodes pull only from
registries of their own project and cross-project IAM permits are refused; the pod template of a
suspended Job is immutable except for node-scheduling fields; Envoy Gateway's built-in API-key auth needs
keys in Secrets, hence the ext-auth service against LiteLLM.
