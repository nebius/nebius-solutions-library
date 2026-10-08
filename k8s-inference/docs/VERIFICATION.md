# Verification record

> The ids, hostnames and IP addresses in this record belong to the reference fleet the solution was developed and measured on; they are evidence, not inputs. A fleet you deploy has its own.

*For reviewers: what was measured, where and when. Every number comes from a real cluster.*

## Fleet placement acceptance (2026-10-07, lane F5, tenant `eval`)

Branch `worktree-agent-a7bc512134685cfb8` (API 0.6.0, dispatcher 0.1.2, runner 0.1.3) run from this
machine against the live clusters before the merge: the branch's API and dispatcher ran locally with the
admin kubeconfigs (`FLEET_MANAGER=true`, control cluster as the manager, the in-cluster dispatcher scaled
to 0 and six Applications on manual sync for the window, restored afterwards), the branch's fleet chart,
tenant chart and worker RBAC applied by hand. Everything the API answered came from the live clusters;
the merge replaces the hand-applied objects with the Argo CD-managed ones.

| Case | Result |
|---|---|
| `GET /v1/models` with the eval key | 6 entries; run classes list both regions, endpoints their deployments |
| `hello-run` through the fleet API, no region | profile `default` -> dispatcher ranked eu-south1 (cheapest, RTX PRO 6000 at $0.95/GPU-h, free) -> manager Job `op-hello-run-0933bcbf` (`managedBy: kueue.x-k8s.io/multikueue`), Workload nominated `eu-south1`, work volume created there by the dispatcher, SUCCEEDED in 67 s; `STATUS.json`, `result.json` and the attempt record in `s3://serverless2-eval-eu-north1/operations/op-hello-run-0933bcbf/`; `logs_url` -> eu-south1 Grafana |
| `container-run` checkpoint loop (busybox, 0 GPU, `prefer-h100`), pod deleted mid-run | `op-container-run-33fd9bf6` placed on the hub; after `kubectl delete pod` the replacement pod started on the same volume (`start at n=17`), SUCCEEDED (`finished`); attempts `PREEMPTED, SUCCEEDED` |
| `gromacs` 5k steps through the fleet API, no region | profile `prefer-rtx-pro-6000`, classes `[rtx-pro-6000, h100]` -> affinity `pool In [h100-spot-1x, rtx6000-spot-1x]`; dispatcher ranked eu-south1 (0.95 < hub 2.15) -> `op-gromacs-fe8172b2` pinned `eu-south1`, images rewritten to `cr.eu-south1.nebius.cloud/e07hj7qkcbdk1n8hd9/...` (GROMACS image mirrored there with the new ops Job template in 11 s); a second RTX node was added (the warm STT replica held the first), 5001 steps at 274 ns/day, SUCCEEDED; run.xtc/edr/log/cpt/gro, topol.tpr in the hub-region bucket; billed 29 GPU-s = $0.012889 at the eu-south1 price to the eval key (`/key/update`, spend 0.0327) |
| `container-run` cancel while RUNNING, then `:resume` | `op-container-run-f9142cff` (hub, fleet-placed): `:cancel` at checkpoint n=20 patched the worker's copy (deadline 1 s) and annotated the manager's Job -> CANCELLED, attempt `CANCELLED`, `resumable: true`, the work volume kept on the hub (no owner); `:resume` -> `op-container-run-f9142cff-r1` on the manager pinned `eu-north1`, same claim, `start at n=20`, SUCCEEDED in 205 s, `out/done.txt` under `operations/op-container-run-f9142cff-r1/`, volume released by the uploader. An earlier attempt (`op-container-run-d8e2c213`) exposed the cancel bug (manager Job deleted because its mirrored `startTime` lagged); its orphaned volume is the sweeper's test case: the dispatcher's sweeper deleted `op-container-run-d8e2c213-work` on the hub at 10:22:34 UTC, 15 min after creation, "manager jobs are gone" |
| `async` Qwen call through the fleet API (`region: eu-north1`) | forwarded to the hub API; `op-qwen2-5-0-5b-7c82b1f6` SUCCEEDED in 165 s, response inline from the bucket ("Hello."); a first attempt `op-qwen2-5-0-5b-b3d8936f` hit the 600 s default deadline while the hub added an L40S node (and while the pre-0.6 API named a ServiceAccount the v2 tenant chart had removed; the alias is back): async calls now have no default deadline |
| STT in eu-south1 via the regional host (`wss://nemotron-speech-en-0-6b-predictor.models.213.239.185.7.sslip.io/v1/audio/stream`, key as WebSocket subprotocol) | connect 1.08 s, ready 1.15 s, 5.7 s clip completed in 1.67 s, RTF 0.091, transcript correct |
| billing pass (`python -m billing`, control cluster settings) | 3 fleet runs billed once from the attempt records (gromacs $0.012889, the CPU runs $0), idempotent |
| operation list from the fleet API | manager Jobs plus the hub's regional Jobs, each once (the worker copies are never listed) |
| fleet Argo CD after the window (10:25 UTC) | 74 Applications, 73 Synced/Healthy with automated sync restored; `dcgm-exporter` (hub) Progressing: its DaemonSet pod on the L40S on-demand node that the async call added at 10:05 (the pool's first node ever) is in CrashLoopBackOff (8 restarts; the node's kubelet log endpoint was unreachable from this host); open item for the L40S pool, outside this lane; the node scales away with the pool's `min_nodes: 0`; the in-cluster dispatcher 0.1.1 and the ApplicationSet controller are back, Argo CD reverted the hand-applied objects to `main` (the branch's state returns with the merge) |
| `infra/fleet/apply.sh plan all` (read-only, main checkout's state) | control, hub, eu-south1: "No changes. Your infrastructure matches the configuration." |
| hub `model-diffdock` / `model-nemotron-speech-en-0-6b` Progressing | cause: Argo CD's InferenceService health reads `modelStatus.transitionStatus`, InProgress for a scaled-to-zero endpoint; health override from the Ready condition applied to the fleet Argo CD (helm upgrade): both Healthy |

Findings during the acceptance (all fixed in the branch):

- MultiKueue deletes the worker's copy of a Job (and its pods) the moment the manager's Job finishes:
  attempts of finished fleet runs come from the uploader's records, billing bills the manager's Job, the
  work volume has no owner on the worker (a sweeper in the dispatcher deletes it once its manager Jobs
  are gone), and a running fleet run is cancelled through the worker's copy while the manager's status
  still lags (the first cancel deleted the manager Job because its mirrored `startTime` was not there yet).
- The uploader gave the kubelet 12 s to report `main`'s exit code and recorded "interrupted" for two runs
  that had finished cleanly (the pod status lagged); runner 0.1.3 waits up to 2 min.
- A Job's pod template is immutable (Kubernetes allows only node-scheduling fields on a suspended Job), so
  the dispatcher cannot rewrite images after nomination; runs with per-region images are placed before
  rendering through the dispatcher's `/v1/rank`. Cross-project image pulls are impossible (403 measured,
  IAM refuses cross-project permits), so this applies to every run today (the runner image).
- The control cluster lacked the WorkloadPriorityClasses the manager Jobs name
  (`WorkloadPriorityClass "customer-batch" not found`): the `scheduling` component now renders there too.

## eu-south1 shared weights filesystem (2026-10-07, lane G1)

Owner raised `compute.filesystem.size.network-ssd` for `project-e07jcsatpr00q4cje3yz2q` to 5 TiB;
`fleet.yaml` eu-south1 `weights_filesystem: {enabled: true, size_gib: 2048}`.

| Step | Evidence |
|---|---|
| `infra/fleet/apply.sh plan eu-south1` | 1 to add (`nebius_compute_v1_filesystem.weights[0]`, 2048 GiB NETWORK_SSD), 1 to change (node group `rtx6000-spot-1x`: `template.filesystems` mount tag `weights` + cloud-init), 0 to destroy |
| `apply eu-south1` | `Apply complete! Resources: 1 added, 1 changed`; node group modified in 4 min 47 s (no timeout); the one RTX node was cordoned and replaced (surge 1); `plan` afterwards: `No changes` |
| Mount on a new node | busybox pod on `computeinstance-e07r53vx4fwnfjq7q2`: `weights on /weights type virtiofs (rw,relatime)`, 2.0T, both RTX nodes of the rolled pool have it |
| Seed Job (`ops/seed-weights-job.yaml`, `SEED_POOL=rtx6000-spot-1x`, HF `Qwen/Qwen2.5-0.5B-Instruct`) | `seed-weights-8nqcj` Complete in 56 s, 954 MB under `/mnt/weights/qwen2-5-0-5b/hub` |
| PV + claim `models/weights-shared` (`clusters/eu-south1/apps/overlays/scheduling/weights-pv.yaml`) | `Bound`, 2Ti RWX (pre-applied with the Argo CD field manager; the `scheduling-eu-south1` Application adopts it after the merge) |
| `qwen2-5-0-5b` eu-south1 deployment (`catalog`, pool `rtx6000-spot-1x`, HF cache on the claim) | InferenceService Ready 8.5 min after creation on a fresh node (image pull + vLLM init); vLLM log: `Loading weights took 0.46 seconds` from the virtiofs mount, no HF download |
| Sync call through the regional API `api.213.239.185.7.sslip.io` (tenant `eval`) | `"Hello from EU-South1!"`; warm 0.5-0.6 s per call; the first call after Ready took 50 s (vLLM warm-up on first request) |
| Cold start after scale-to-zero (node present, weights seeded) | 257 s end to end: `Loading weights took 0.22 seconds`, model loaded at +45 s, uvicorn up at +3 min 30 s (torch.compile + CUDA-graph capture on RTX PRO 6000 dominate, same shape as the hub's 137.7 s on H100) |
| Fleet Argo CD after the roll | eu-south1 DaemonSet apps (alloy, dcgm-exporter, nvidia-device-plugin, kube-prometheus-stack) Progressing while the new nodes joined, Healthy afterwards |

Follow-up (not done here): vLLM's compile cache (`VLLM_CACHE_ROOT`) on the shared filesystem would cut
the remaining cold-start time on both regions; the weights themselves are no longer part of it.

## Image cache (2026-10-07, lane G4)

Zot pull-through cache per cluster + Spegel peer-to-peer + one logical registry host
(`docs/IMAGES.md`); every run placed at queue time. Tenant `eval`, the branch's API 0.7.0 and
dispatcher 0.1.3 run locally against the control cluster inside a test window (in-cluster dispatcher
and ApplicationSet controller paused, as lane F5 did), everything else live.

| Case | Result |
|---|---|
| containerd on the node images | CPU images: `config_path = /etc/containerd/certs.d`, `discard_unpacked_layers = false` shipped. GPU images (L40S hub node, RTX eu-south1 nodes): `version = 2`, `imports = conf.d/*.toml`, no registry section, effective `config_path = ''`; the `node-config` DaemonSet writes `conf.d/serverless2-registry.toml` and restarts containerd once per node (`wrote ... containerd restarted`, eu-south1 RTX node; CPU nodes: `node image ships the registry configuration`) |
| hosts.toml written by Spegel on every node (hub, eu-south1, control) | `registry.serverless2.local`, `docker.io`, `ghcr.io`, `quay.io`, `nvcr.io`, `registry.k8s.io`: hosts `http://<node>:30020`, `http://<node>:30021` (Spegel), `http://127.0.0.1:30500` (Zot NodePort on the loopback address; reachable from the host namespace through Cilium, measured 200 on a system node and on an L40S node) |
| Zot access control | anonymous `GET /v2/.../tags/list` 200; anonymous `POST .../blobs/uploads/` and `PUT .../manifests/latest` 401 |
| cold pulls on a hub system node through the logical host | `docker/library/busybox:1.36` (Docker Hub) 0.94 s; `nebius/serverless2/jobs:0.1.3` (source registry) 2.4 s; `nebius/gromacs:2026.4-cuda12.8-sm90-120` (518 MB) 20.9 s; Zot log: `successfully synced image` for each |
| cold pulls on an eu-south1 system node (cross-region source) | busybox 0.8 s, runner 4.6 s, GROMACS: Zot synced the 518 MB from the hub registry in 12 s, pull 29 s on the first node |
| eu-south1 RTX node after the containerd restart | busybox 0.8 s, runner 4.6 s, GROMACS 29.2 s cold / 7.8 s re-pull (Spegel peer + cache) |
| DiffDock NIM 15 GiB (`nvcr/nim/mit/diffdock:2.2.0`) on the eu-south1 RTX node, cold | Zot synced it from NGC in 9 min 38 s (12:33:50 -> 12:43:28); containerd's first request ended with `NotFound` at the same second, the kubelet's retry found it cached (`skipping image because it's already synced`). On a second RTX node (fresh spot node of the same cluster): 16.4 GB in 3 min 27 s from the cache and the first node's Spegel peer (about 80 MB/s), no NGC traffic |
| fresh eu-south1 spot node (12:41:04) for the GROMACS run | node-config + Spegel pods Running at 12:42:39; the fetch init container's first pull of `registry.serverless2.local/...` at 12:4x failed (`lookup registry.serverless2.local`), the retry at 12:42:46 succeeded; GROMACS image from the cache in 12.3 s; `main` running at 12:43:09 (2 min 5 s after node creation). The cloud-init snippet in `docs/IMAGES.md` removes that one retry (Terraform lane) |
| `hello-run` through the fleet API 0.7.0, no region | `op-hello-run-4a418fd5`: created unpinned (`region: null`, no `serverless2.nebius/region` label); dispatcher nominated eu-south1; SUCCEEDED in 48 s |
| `gromacs` 5k steps through the fleet API 0.7.0, no region | `op-gromacs-0bf3da40`: unpinned, image `registry.serverless2.local/nebius/gromacs:2026.4-cuda12.8-sm90-120` as written in the catalog (no rewrite), profile `prefer-rtx-pro-6000`; dispatcher nominated eu-south1, a second RTX spot node was added, 5001 steps, SUCCEEDED (worker Job complete 12:43:47), billed 16 GPU-s = $0.007111 at the eu-south1 price |
| admission is read on the workers (cause found and fixed later the same day: the dispatcher's merge patch of `nominatedClusterNames`, see "MultiKueue admission" below) | manager Workloads never got `status.clusterName` nor a Ready MultiKueue admission check under the external dispatcher (all finished runs: `cluster= None`, check `Pending`, while the worker's Workload is `Admitted`); the dispatcher 0.1.3 therefore reads the remote Workload (same name) in the nominated workers, records `serverless2.nebius/admitted-cluster` on the manager Workload and creates the work volume there; the API reports that cluster as the region (never a nomination). `op-hello-run-fe2b46f6` (window): the branch's dispatcher logged `admitted in eu-south1` and created the 1 Gi volume there 2 s later; SUCCEEDED; the API reports `region: eu-south1` with the attempt's node after the run finished (manager Workload: `clusterName` empty, annotation `admitted-cluster: eu-south1`); no volume left in either worker afterwards (the uploader released it) |
| re-nomination while a run waits | the GROMACS Workload was re-nominated `[eu-south1, hub]` at 12:43:27 by the branch's dispatcher (300 s without a visible admission on the manager); harmless here (the run was already executing in eu-south1), and the worker-side admission read above stops the re-nomination of running jobs |
| tests | `services/api/tests` 19 passed, `services/dispatcher/tests` 13 passed |
| registry switch | eight references copied from `k8s-inference-h100` into `serverless2-models` (`e00kttahv8d1dzfxwv`) by an in-cluster Job with the ops SA in 42 s (push permitted: no extra IAM permit needed); fleet.yaml `images.source` flipped; `registry/zot-config` re-rendered and the three Zot pods restarted; the caches fetch from the new registry (annotation `serverless2.nebius/source` on the ConfigMap) |
| mistakes made and undone | (1) `helm template` of the Spegel chart without `--no-hooks` applied its post-delete cleanup DaemonSet, which removed the hosts.toml files; deleted, Spegel restarted. (2) The first containerd fix appended a table to `config.toml` that the GPU image already defines; the hub L40S node's containerd refused to start (NotReady, "container runtime is down"); the node was idle and the autoscaler replaced it. The DaemonSet now writes a drop-in only and parses the config before restarting |

## Alertmanager without an external receiver (2026-10-07, lane G1)

Owner: no Slack webhook for now. `slack_configs` and the `alertmanager-slack` Secret mount removed from
`clusters/common/values/kube-prometheus-stack.yaml`; `helm template kube-prometheus-stack 91.9.0` with
the values renders with zero `slack` occurrences; alert `AlertmanagerSlackNotifyFailing` replaced by
`AlertmanagerNotifyFailing` (any integration). After the merge the live Secret
`monitoring/alertmanager-slack` can be deleted on control, hub and eu-south1.

## Earlier records

Claims checked against primary sources (Nebius docs and CLI, SkyPilot docs,
dstack docs, KServe docs, LiteLLM docs and licence, Argo docs, beta9 licence
file) on 2026-10-05, plus live findings during the build.

| Claim | Verdict |
|---|---|
| Managed Kubernetes supports Karpenter (`--control-plane-karpenter`) | true on paper; unusable in practice (image `mk8s/karpenter:v0.0.1` missing, no 1.35 worker image families), spike S1 |
| Nebius ships Managed SkyPilot that discovers Managed Kubernetes clusters | true; not used by this substrate |
| SkyServe fit for customer endpoints | false: beta, QPS-only, no request buffering |
| SkyPilot Endpoints | early access, LLM-centric |
| KServe scales to zero without Knative for generic containers | false; only LLMInferenceService via KEDA idle replicas |
| KEDA HTTP add-on | pre-1.0, HTTPScaledObject deprecated |
| dstack multi-region Nebius from one server | true; SSO is enterprise-only |
| LiteLLM MIT core, pass-through cost per request | true; SSO/SAML/audit are enterprise; per-key pass-through allow-list is `metadata.allowed_passthrough_routes` (top-level field is enterprise) |
| Argo per-namespace artifact repos, SSO RBAC delegation, HTTP templates | true (Argo Workflows retired 2026-10-07) |
| beta9 licence | AGPL-3.0 |
| Inferize joined Token Factory | true (press release 2026-10-01) |
| us-north1 | private region, B300 only; us-central1 is the public US region |
| Spot pricing policy | per preemptible resource: cap via `nebius billing pricing-policy` or follow spot; dynamic pricing from 2026-10-08 |
| Object Storage cross-region Transfer service | true |
| KServe 0.20 in Knative mode with Gateway API | creates only the predictor HTTPRoute (`<isvc>-predictor.<ns>.<domain>`); `status.url` host has no route; clients use the predictor host |
| `kserve/huggingfaceserver:v0.20.0` | CPU-only torch; the `-gpu` tag works; vLLM in Knative pods needs an emptyDir `/dev/shm` |
| Nebius LB | rejects `externalTrafficPolicy: Local`; Knative revision timeout max raised 600 -> 3600 s |
| Scale-from-zero node provisioning | works through the node-group autoscaler without Karpenter; a STOPPED spot VM is not restarted by the platform (ops CronJob, S13) |
| Nodes pulling images | only from registries in their own project; per-region registries and mirroring (S10); 2026-10-07: a cross-project IAM permit is refused ("resource not found in project"), a hub node gets 403 from the eu-south1 registry |
| Envoy Gateway v1.6 API-key auth | keys must live in Secrets; LiteLLM keys are dynamic, hence the ext-auth service (S15) |
| Kubernetes Job pod template on a suspended Job | immutable except node-scheduling fields ("spec.template: field is immutable", 2026-10-07): image rewrites must happen at creation |

## DCGM exporter on L40S nodes (2026-10-07, after lane G1/G3)

| Check | Result |
|---|---|
| Crash cause on the hub L40S on-demand node `computeinstance-e00f2sxznndefrhfs6` (driver 580.173.02) | exporter 4.8.4 log: DCGM and NVML initialise, then `Failed to watch DCGM fields ... field_ids=[... 1005 1009 1010] ... error="error watching fields: The third-party Profiling module returned an unrecoverable error"` and the collector exits; H100 and RTX PRO 6000 nodes run the same configuration without error |
| Test pod on that node: same DaemonSet pod spec, counters file with the `DCGM_FI_PROF_*` lines removed | Running, 0 restarts, `/metrics` serves `DCGM_FI_DEV_GPU_UTIL`, `DCGM_FI_DEV_GPU_TEMP` (27 C), `DCGM_FI_DEV_FB_USED` for the L40S |
| Fix | second Application `dcgm-exporter-l40s` (same chart 4.8.4, `clusters/common/values/dcgm-exporter-l40s.yaml`: node affinity `nebius.com/gpu-name in (L40S)`, `customMetrics` = chart default with the profiling fields commented out); `dcgm-exporter` excludes `nebius.com/gpu-name in (L40S)`; both rendered with `helm template` (1 DaemonSet each, 0 active `DCGM_FI_PROF` lines in the L40S counters). First deployment in the shared `monitoring` namespace left both apps OutOfSync: the chart hard-codes `exporter-metrics-config-map` and Role `dcgm-exporter-read-cm`, so the releases overwrote each other; the L40S release moved to namespace `monitoring-l40s` |

## Post-merge fixes after lane G4 (2026-10-07 13:00-13:30 UTC)

| Check | Result |
|---|---|
| Hub H100 nodes created during the merge window pulled nothing through the cache (`ImagePullBackOff`, `lookup registry.serverless2.local ... server misbehaving`) | cause: an earlier hand-applied node-config version had appended the registry tables to `config.toml` without a restart, the merged version then saw `config_path` in the file and exited; the running containerd had no mirror configuration (`crictl pull` through the logical host failed, `ctr --hosts-dir` succeeded). Fix: the DaemonSet decides with a probe pull of busybox through the logical host (containerd 2.x does not show the images plugin's registry setting in `crictl info`), repairs a legacy appended block, and verifies after the restart; the GPU pools were rolled with the cloud-init drop-in (Terraform), the first rolled node mounted the weights filesystem and pulled through the mirrors |
| Fresh node, first pulls before Spegel runs | journal of `computeinstance-e00kgtj872mcr7k31p`: containerd restarted by cloud-init at 13:18:44 (15 s after boot), pulls from 13:19:08 fell back to DNS for the logical host because its `hosts.toml` did not exist yet; kubelet back-off then held the pods for minutes. Fix: cloud-init also writes `certs.d/registry.serverless2.local/hosts.toml` (Zot NodePort on the node); pools rolled again |
| Zot Applications OutOfSync with an empty diff on all three clusters | not the `kubectl rollout restart` annotation, not the `g4` field manager, not null values (each removed and re-checked); a StatefulSet with a `volumeClaimTemplate` under server-side apply. The claim `zot-pvc-zot-0` is now rendered by `charts/fleet` and mounted directly; StatefulSets replaced with `--cascade=orphan` (pods kept, caches intact: hub catalog unchanged after the switch); all three `Synced/Healthy` |
| Zot on-demand sync from the new hub registry and NGC | hub catalog after the roll: `docker/kserve/huggingfaceserver`, `docker/library/busybox`, `nebius/fs2-models/nemotron-speech/en`, `nebius/gromacs`, `nebius/serverless2/{api,jobs,ops}`, `nvcr/nim/mit/diffdock`; DiffDock 2.2.0 synced in 2 min 58 s (13:15:42 to 13:18:40) |

## Final acceptance after the pool rolls (2026-10-07 13:45-13:52 UTC, main 2954959, API 0.7.0)

| Check | Result |
|---|---|
| First node of the second roll (hub, `computeinstance-e00dpj2mewra71vxyv`, boot 13:31:59) | mirror file present from boot (Spegel kept the cloud-init copy under `certs.d/_backup`), 0 DNS-fallback errors for the logical host in containerd's journal since boot, node-config: "probe pull ok", pre-pull streaming the catalog images through the cache, DCGM exporter Running |
| Newest eu-south1 node (`computeinstance-e07e8fm5chn902pkw4`, boot 13:43:49) | 0 DNS-fallback errors, 0 failed pulls of the logical host since boot, weights filesystem mounted, node-config: "probe pull ok" |
| `hello-run` through the control API, no region (`op-hello-run-fb7b1047`) | dispatched at queue time to eu-south1 (cheapest), SUCCEEDED in 96 s on a node 3 min old |
| `gromacs` 5000 steps through the control API, no region (`op-gromacs-b866c8b5`, inputs `s3://serverless2-eval-eu-north1/inputs/mas-20e`) | dispatched to eu-south1, SUCCEEDED in 88 s on the same fresh node, artifacts `run.{xtc,edr,gro,log,cpt}`, `topol.tpr`, `mdout.mdp`, attempt record in the bucket |
| Terraform | `plan all`: no changes on control, hub, eu-south1 (state in the bucket); both GPU regions rolled twice today (drop-in, then the mirror file) |
| Endpoints | hub diffdock, nemotron-speech-en-0-6b, qwen2-5-0-5b Ready; eu-south1 nemotron-speech-en-0-6b, qwen2-5-0-5b Ready; no image-pull or crash-looping pods on any cluster |

## MultiKueue admission (2026-10-07 14:00-14:20 UTC)

| Check | Result |
|---|---|
| Why manager Workloads never got `Admitted` / `clusterName` under the external dispatcher | Kueue leader log (`kueue-controller-manager-...-nvgn6`, 49 occurrences in 40 min): `Failed to patch workload ... status: Invalid value: clusterName and nominatedClusterNames are mutually exclusive` from `multikueue/workload.go:1324` (`syncAdmittingRemoteState`). The dispatcher (0.1.2/0.1.3) wrote `nominatedClusterNames` with a merge patch as field manager `kueue-admission` (managedFields: `kueue-admission Update`); Kueue's server-side apply as the same manager cannot remove a field owned by an Update entry, so its patch (set `clusterName`, clear nominations) was rejected on every reconcile. Not a Kueue bug: the constraint is documented under "Workload Dispatching" |
| dispatcher 0.1.4 (server-side apply call) | no effect: the image's kubernetes client 31.0.0 rejected the `_content_type` argument (`ApiTypeError` in the dispatcher log); nominations kept being merge patches |
| Re-applying a running Workload's nomination with server-side apply | no effect either: the Update ownership entry stays; only Workloads nominated by apply from the start admit |
| dispatcher 0.1.5 (client 36.0.3), fresh probe `op-container-run-75d5c8f7` | 15 s after submission: `clusterName: hub`, `nominatedClusterNames: null`, check `Ready` ("The workload was admitted on \"hub\""), `Admitted: True`, managedFields owner of the field: `kueue-admission Apply` only; dispatcher log clean |
| Why the fix matters (pre-fix run `op-container-run-0d15dcc9`, nominated by dispatcher 0.1.4 with the old merge patch) | its first attempt on the hub was cut by the pool roll (attempt record `PREEMPTED` on `computeinstance-e00chxhr2c144vfdre`); because the manager never recorded the admission, the dispatcher's 300 s re-nomination added eu-south1, MultiKueue created a second remote there, it admitted first and the run finished in eu-south1 with a fresh work volume (the hub volume was swept). With `clusterName` set at admission (immutable in Kueue) and the dispatcher skipping admitted Workloads, a run can only be re-admitted in the cluster that admitted it, which is the same-region resume the requirements ask for |
| Argo CD Progressing on three hub DaemonSet apps after the smoke runs | transient: DaemonSet health while the last H100 node scaled away; all Synced/Healthy minutes later, every hub DaemonSet desired = ready |

## Security fixes F1/F2/F3 on the live fleet (2026-10-08 14:20-15:05 UTC, main eb0d341 + served-model fix)

| Check | Result |
|---|---|
| Tenant policy after merge (all three clusters) | `tenant-isolation` egress: DNS, kube-apiserver, world, `envoy-gateway-system` only; `models`/`knative-serving` gone; tenant namespaces `pod-security.kubernetes.io/enforce=baseline` (onboarding `--skip-cloud` re-rendered demo and eval) |
| Direct call from a pod in `tenant-eval` (hub) to `qwen2-5-0-5b-predictor.models.svc.cluster.local` | blocked (timeout, HTTP 000); the same pod reaching the gateway hostname without a key: 401 |
| Async Qwen through the control API (`op-qwen2-5-0-5b-1a186d2d`) | SUCCEEDED in 14 s; job pod env `URL=https://qwen2-5-0-5b-predictor.models.89.169.125.46.sslip.io/openai/v1/chat/completions`, `CONNECT_TO=...:443:knative-external.envoy-gateway-system.svc:443`, runner `jobs:0.1.4`, capabilities dropped |
| hello-run (`op-hello-run-9a819a3d`, 68 s) and GROMACS 5k (`op-gromacs-9e434613`, 372 s incl. a fresh spot node) with the hardened spec | both SUCCEEDED in eu-south1; rendered Job: pod seccomp RuntimeDefault, every container `drop: [ALL]`, `allowPrivilegeEscalation: false`, runner containers non-root, model container keeps the image user |
| Served model name | the first async attempt failed with "Model with name None does not exist": OpenAI-protocol endpoints need `model` (= `--model_name`, here `qwen`) in the body and the caller did not send it (the earlier acceptance call had). Fix: the API injects the served name for sync and async when omitted (`served_model` from the catalog's `servedModel` or `--model_name=`; API 0.7.2) |
| LiteLLM spend | unchanged for Qwen: the model has no LiteLLM route, so accounting stays with the billing CronJob's attempt records (unchanged behaviour, noted by lane H4) |

## Fresh deployment from `terraform.tfvars` (2026-10-08 14:10-17:30 UTC, lane H2, branch lane-h2 on main a90b2fe)

A second fleet, `s2lib`, was brought up from nothing with the Terraform solution (README.md) in the owner's
projects, never touching the live fleet (`serverless2-*` clusters, registry, buckets), and destroyed again.
Every number below comes from the applies' output, `kubectl` on the three test clusters and the customer
API through the public edge. The tfvars used is the one in `terraform.tfvars.example` reduced to: control
plane dedicated in project-e00rene (eu-north1), region eu-north1 (`id = "hub"`, pool `h100-spot-1x`
1gpu-16vcpu-200gb spot cap 2.15, max 1 then 2), region eu-west2 (project rene-eu-west2, pool
`b300-spot-1x` gpu-b300-sxm 1gpu-24vcpu-346gb spot, max 1), 256 GiB weights filesystems and caches,
tenant `eval` with one key (budget 25 USD), models gromacs + qwen2-5-0-5b from the bundled catalog and
hello-run + container-run re-homed to `{hub, eu-west2}`, `acceptance = { model = "hello-run", endpoint =
"qwen2-5-0-5b" }`, `protect_data = false`. Platform images: the five tags copied/built into the fleet's own
registry (`tools/images.sh`), api 0.8.1 and dispatcher 0.1.6 built from this branch.

### Stages and timings

| Step | Result |
|---|---|
| `stack/bootstrap/state-bucket.sh` + `./stack.sh preflight` | bucket `s2lib-tfstate`; preflight passes (platform/preset/driver matrix, state bucket; the final code also checks catalog classes and the exact local-NVMe rule) |
| `apply cloud` (first region set eu-north1 + eu-south1) | control + hub clusters, pools, identities, registry, buckets in ~12 min; eu-south1 cluster came up but its gateway IP failed: `vpc.ipv4-address.public.count` quota 3/3 in that project. Region replaced by eu-west2 in the tfvars: `apply cloud` planned "15 to add, 14 to destroy", eu-south1 torn down and eu-west2 created (cluster 10m55s, system pool 2m11s, static IP vpcallocation-e04cpcp6w59vhkrgje) |
| local NVMe probe | with `local_nvme = true` on gpu-b300-sxm/1gpu-24vcpu-346gb the node group was rejected: `Instance template spec is invalid: spec.local_disks.passthrough_group.requested is invalid` (same as gpu-h100-sxm 1x/8x in eu-north1 earlier that day). The documentation lists exactly one NVMe preset, gpu-b300-sxm 8gpu-192vcpu-2768gb in uk-south1/eu-west2/us-north1; the owner's eu-west2 project has a B300 quota of 5 GPUs, so it could not be created. Table and consequences: docs/FLEET.md "Local NVMe". The rendering path was verified instead (below) |
| `apply platform hub` | first pass stopped three times for ordering fixes now in the code (Prometheus-operator CRDs before ServiceMonitor-bearing charts, envoy-gateway/zot not waited for, zot in wave 2 after the fleet chart's claim, `registry` and `knative-serving` namespaces pre-created with Helm ownership metadata); final state 105 resources; 78 pods Running, wildcard certificate from Let's Encrypt production READY, gateway 89.169.102.213, `https://api.<ip>.sslip.io/healthz` 200, Grafana 200 |
| `apply platform eu-west2` | ONE pass: 106 added, 0 errors, ~9 min (after the allow-list fix below); 78 pods Running, certificate READY, gateway 213.239.174.32, healthz 200 (`"region":"eu-west2"`), Grafana 200, flavor `b300-spot-1x` |
| `apply platform control` | ONE pass: 103 added, 0 errors, ~9 min; 62 pods Running, certificate READY, gateway 89.169.103.172, healthz 200 (`"fleet_manager":true,"regions":["control","eu-west2","eu-north1"]`); `multikueuecluster` hub and eu-west2 CONNECTED=True, admission checks `multikueue-{default,prefer-b300,prefer-h100}`, flavors `hub-h100-spot-1x` and `eu-west2-b300-spot-1x`; the workers' kubeconfigs arrived through `terraform_remote_state`, no manual step. Two control-only items were missing from the stage and are now rendered by it: ClusterRole `serverless2-api-tenant` (the fleet API got 503 "tenant storage not configured (Forbidden)") and the cost dispatcher (no nomination ever happened) |
| `apply models hub`, `apply models eu-west2` | tenant namespace with the PSA labels, bucket + identity per region, Secrets, Qwen `InferenceService` on the hub (READY after ~10 min: H100 spot node from zero in 2 min, huggingfaceserver image through the fresh cache ~6 min); keyless call at the edge: 401 |
| `apply models control` | LiteLLM key for tenant eval, acceptance probe Job: `probe = "acceptance-probe Job in namespace api succeeded (model hello-run, endpoint qwen2-5-0-5b)"`: hello-run QUEUED 16:30:50, RUNNING 16:33:33, SUCCEEDED 16:33:43 in eu-west2; Qwen chat 16:35:06 HTTP 503 (scale from zero), 16:40:45 HTTP 200 `"Hello! How can I assist you today?"` |

### Runs through the customer API (control cluster edge, tenant key from the outputs)

| Run | Result |
|---|---|
| `hello-run`, unpinned (`op-hello-run-be1cec28`) | dispatcher: `profile={'classes': ['b300','h100'], 'strategy': 'cheapest'} -> ['eu-west2'] best=[('eu-west2','b300-spot-1x',0.99,True)]`; admitted in eu-west2 (admission check "The workload was admitted on eu-west2"), B300 spot node from zero (instance RUNNING at +1.5 min, node Ready at +2.5 min), attempt on computeinstance-e04gx0gr9erj3jcwm1 exit 0, SUCCEEDED, artifacts under `s3://s2lib-eval-eu-north1/operations/op-hello-run-be1cec28/` (hub-region tenant bucket), presigned URLs from `/result`, `logs_url` into the eu-west2 Grafana |
| `container-run` with `scratch: local-nvme`, pinned eu-west2 (`op-container-run-331d3d5d`) | worker pod rendered with nodeAffinity `{pool In [b300-spot-1x,h100-spot-1x], serverless2.nebius/local-nvme In [true]}` and `/work` as `emptyDir{sizeLimit: 20Gi}` (no PVC); stays Pending ("didn't match Pod's node affinity") and the autoscaler does not scale a non-NVMe pool for it; cancelled. This is the whole NVMe path that could be exercised without the 8-GPU B300 preset |
| `gromacs` 5000 steps, unpinned (`op-gromacs-976612fe`, inputs `s3://s2lib-eval-eu-north1/inputs/mas-20e`, the 185k-atom baseline) | dispatcher: `profile={'classes': ['h100','b300'], 'strategy': 'preferred'} classes=['h100'] -> ['hub']`, admitted in hub, 50 Gi work PVC created by the dispatcher, ran on the H100 spot node the Qwen endpoint had just released (no second node needed), grompp + mdrun attempt 16:44:52-16:45:04 exit 0, operation SUCCEEDED (eu-north1, 70 s from admission); artifacts `run.{cpt,edr,gro,log,xtc}`, `topol.tpr`, `mdout.mdp`, `run.mdp`, `STATUS.json`, attempt record (total 27 MB) in the tenant's hub bucket. Two earlier submissions exposed the catalog-class and dispatcher-apply defects fixed on the branch (below) |

### What the test changed in the solution (all on the branch)

- Catalog entries keep only GPU classes the fleet declares (first GROMACS submission waited forever on `prefer-rtx-pro-6000`, the bundled entry's first class); `preflight` reports entries left without a class.
- Dispatcher 0.1.6: the nomination apply restates `status.admission` and `status.admissionChecks` (second GROMACS submission: the apply released Kueue's quota reservation, the manager re-admitted and copied the workload to a non-nominated cluster, the retry wedged every later apply with "admissionChecks[0].state: Required value").
- `stack.sh` keeps one Terraform data directory per stage and cluster: two overlapping stage runs in one checkout re-keyed each other's backend once during this test (the hub run wrote its state to the eu-west2 key; both states were re-synced by solo applies, no cluster change resulted).
- mk8s API allow-lists are enforced per cluster: the runner's egress for eu-west2 differed from eu-north1 (Tailscale subnet routes), the eu-west2 endpoint dropped it until the real egress was listed (`allowed_cidrs`) and routed directly. README "Troubleshooting" carries the rule.

### Region and platform coverage

| Region | Project | Platform / preset | Capacity | Exercised |
|---|---|---|---|---|
| eu-north1 | project-e00rene | cpu-d3 8vcpu-32gb (system pools, control + hub) | on-demand | all platform components, Zot/Spegel cache, LE certificates, static IPs |
| eu-north1 | project-e00rene | gpu-h100-sxm 1gpu-16vcpu-200gb | spot (cap 2.15) | Qwen2.5-0.5B endpoint (KServe/Knative), GROMACS run, scale 0->1->0 twice |
| eu-west2 | project-e04t56y1pr003cgwt0d3ws | cpu-d3 8vcpu-32gb (system pool) | on-demand | platform components, cache, LE certificate, static IP (one of the 3 public IPs of the project) |
| eu-west2 | project-e04t56y1pr003cgwt0d3ws | gpu-b300-sxm 1gpu-24vcpu-346gb | spot | hello-run x2 via MultiKueue (cheapest region), node labels pool/capacity, scale 0->1; `local_nvme` rejected on this preset |
| eu-south1 | project-e07jcsatpr00q4cje3yz2q | cpu-d3 (system pool) | on-demand | cluster creation only; replaced by eu-west2 (no free public IP in the project) |

### Destroy

`./stack.sh destroy all` (16:52-17:27 UTC): models control 5, models hub 10, models eu-west2 9 resources;
platform control 112, hub 106, eu-west2 106; cloud 44. Two things stopped it and are now handled or
documented: the models stage of a worker read the hub's models state (already gone) for the `s3-fleet`
Secret, so the wrapper now destroys the hub's models state last and the lookups tolerate its absence; the
Kueue chart's uninstall waited on its two aggregated ClusterRoles (about ten minutes per cluster until they
were deleted by hand, README "Troubleshooting"). The cloud stage refuses a registry with artifacts and a
bucket with objects (Nebius API: "Please remove all artifacts from registry!", "BucketNotEmpty"): the tenant
buckets had been emptied with their own keys beforehand, the registry's 28 artifacts were deleted with
`nebius registry image delete`, the backups bucket (refilled by the Postgres WAL archive between emptying
and destroy) was deleted with `nebius storage bucket delete --ttl 1h` (scheduled purge) and dropped from
the state. Afterwards: no `s2lib` cluster, instance, node group, filesystem, public IP allocation, GPU
cluster, registry, service account or bucket in the three projects except the state bucket and the
state-key service account, which `stack/bootstrap/state-bucket.sh` created outside Terraform and which
were removed by hand last. The live fleet's resources were never touched.

### InfiniBand pools (owner requirement, plan-only)

Pool input `interconnect = "infiniband"` + `infiniband_fabric` (schema validation: full-node preset,
fabric given; `preflight` checks the platform's `allow_gpu_clustering` for the preset). `terraform plan`
of the cloud stage with an extra pool `h100-ib-8x` (gpu-h100-sxm 8gpu-128vcpu-1600gb, fabric-2) on the
emptied test state: 46 to add, including `module.cluster["hub"].nebius_compute_v1_gpu_cluster.ib["h100-ib-8x"]`
with `infiniband_fabric = "fabric-2"`, the node group's `template.gpu_cluster = { id = (known after apply) }`
and the node label `serverless2.nebius/interconnect = "infiniband"`; `helm template charts/fleet` renders
the pool's worker ResourceFlavor with `nodeLabels.serverless2.nebius/interconnect: infiniband` and the
manager flavor with the same label. No 8-GPU node was created; the JobSet-based multi-node job class is a
follow-on lane.

### Cost of the test

Estimated from instance lifetimes (`nebius compute instance list` creation times) and list prices, upper bound
(spot H100 counted at its 2.15 cap; the running spot price was 0.79):

| Item | Rate (USD/h) | Hours | USD |
|---|---|---|---|
| control system pool, 2 x cpu-d3 8vcpu-32gb | 0.74 | 3.1 | 2.3 |
| hub system pool, 2 x cpu-d3 | 0.74 | 3.1 | 2.3 |
| eu-south1 system pool, 2 x cpu-d3 (replaced region) | 0.74 | 1.1 | 0.8 |
| eu-west2 system pool, 2 x cpu-d3 | 0.74 | 1.8 | 1.3 |
| H100 spot 1x, two scale-ups (Qwen start; probe Qwen + GROMACS) | 2.15 | 0.9 | 1.9 |
| B300 spot 1x, one scale-up (hello-runs) | 0.99 | 0.5 | 0.5 |
| 3 static public IPs, 3 x 256 GiB network-ssd filesystems, registry and buckets | - | 3.3 | 0.5 |
| **Total for the 3.5-hour test** | | | **about 10** |

Idle, the three-cluster fleet (6 CPU nodes, filesystems, IPs, no GPU node) costs about USD 2.4 per hour;
every GPU minute is paid only while a run or an endpoint holds a node (pools and endpoints scale to zero).

## Multi-node runs over InfiniBand (lane H5, 2026-10-08)

Control path, hub, tenant `eval`, the lane's API run locally against the cluster (regional mode):

| Check | Result |
|---|---|
| `POST /v1/models/distributed-run:invoke` (`nodes: 2`, `gpus_per_node: 0`, busybox, `interconnect: none`) | 202, `op-distributed-run-6bf1896c`; a JobSet with one indexed Job of 2 pods (`parallelism/completions 2`, `backoffLimit 0`), `failurePolicy maxRestarts 3 / Recreate`, headless Service `op-distributed-run-6bf1896c` |
| Kueue | Workload `jobset-op-distributed-run-6bf1896c-0a22d`, owner `JobSet`, one pod set of 2, admitted in `prefer-h100` on flavor `h100-spot-1x` (nodeSelector `serverless2.nebius/pool=h100-spot-1x` and the GPU toleration injected); the autoscaler added one H100 spot node (`TriggeredScaleUp 0->1`), both pods ran on it |
| Rank environment | `main` logs: `rank=0 nnodes=2 world=2 master=op-distributed-run-6bf1896c-workers-0-0.op-distributed-run-6bf1896c:29500 host=op-distributed-run-6bf1896c-workers-0-0` and `rank=1 ...` (pod hostnames and the JobSet subdomain resolve inside the pods) |
| Uploads | rank 0: `UPLOAD ... status=succeeded`, `STATUS.json` + its attempt record; rank 1: `RECORD rank=1` (attempt record only, `UPLOAD_SCOPE=rank0`); `GET .../result`: `STATUS.json`, two `attempts/*.json` |
| Operation | SUCCEEDED, `nodes: 2`, two attempts on `computeinstance-e00j6tyg1jg1d7980m`, 75 s, JobSet `Completed/AllJobsCompleted`, 0 restarts |
| Found and fixed | the busybox command could not write `/work/out/rank-N.txt` ("Permission denied"): `fetch` (uid 10001) creates `out/` with mode 755 and `main` runs with every capability dropped since lane H4 (no `CAP_DAC_OVERRIDE` even as root), so a root image cannot write into it. The same applies to every single-node `container-run` since H4 (GROMACS writes to `/work` itself and was not affected). Fix: `fetch` creates `out/` and `checkpoint/` with mode 2777 (runner `jobs:0.1.6`) |
| Unit tests | `services/api/tests` 25 (JobSet rendering, status/cancel/resume, billing), `services/dispatcher` 16 (JobSet owners) |

Terraform (the live hub, state in the bucket): pool `h100-spot-8x-ib` (`gpu-h100-sxm` `8gpu-128vcpu-1600gb`, spot cap 2.15, `interconnect: infiniband`, `infiniband_fabric: fabric-3`) planned 3 to add and nothing else changed, applied in one pass: GPU cluster `computegpucluster-e00at3meg7bft6g0w4` (`serverless2-hub-h100-spot-8x-ib-ib`, fabric-3), pricing policy, node group with `template.gpu_cluster`, `plan` clean afterwards; removed after the test (see below).

Capacity findings (what could be tested): project `project-e00rene` (eu-north1) quotas `compute.instance.gpu.h100` usage 10, `.h200` usage 2, `compute.gpucluster.count` usage 1 (the POC's fabric-2 cluster), no limit values exposed to this identity; `nebius capacity resource-advice` is PermissionDenied at tenant scope for the Terraform identity (`sandbox2`); eu-south1 (`project-e07jcsatpr00q4cje3yz2q`) has RTX PRO 6000 only (no fabric); no capacity block groups visible in project-e00rene (the owner's 16 x H200 reservation lives elsewhere). The only way to know whether 2 x 8 H100 spot nodes exist is to ask for them: the InfiniBand data-path test below did.

InfiniBand data path, hub, 2026-10-08 17:35-19:00 UTC (temporary pool `h100-spot-8x-ib`: 2 x `gpu-h100-sxm`
`8gpu-128vcpu-1600gb` spot, GPU cluster on `fabric-3`, `gpu_settings.dra = true`, `min_nodes 2`; removed after
the test, `plan` clean):

| Check | Result |
|---|---|
| JobSet rendered by the lane's job engine (`distributed-run`, `nodes: 2`, `gpus_per_node: 8`, `interconnect: required`, image `nvcr/nvidia/pytorch:25.09-py3` through the cache) applied in an isolated namespace `h5-ib` (PSA privileged, outside Kueue: the live fleet chart has no flavor for a temporary pool) | pods claim ResourceClaimTemplate `ib-8` (DeviceClass `ib.networking.nebius.ai`, ExactCount 8); both claims allocated 8 devices each (`pci-0000-8c-00-0` ... `pci-0000-b6-00-0`) on their nodes |
| What a node without `gpu_settings.dra` does | the 8 x 400 Gb/s NDR ports (`mlx5_0..7`, `ib0..7`, `/dev/infiniband`) are there, but no DraNet label, no ResourceSlice: `cannot allocate all claims`, the pods never schedule. With `dra = true` (Terraform, pool rolled) the nodes carry `nebius.com/dranet-rdma-capable=true` and DraNet publishes the NICs |
| Scale from zero | the autoscaler never templates DRA devices for an empty pool (`pod didn't trigger scale-up: cannot allocate all claims`); `min_nodes >= 1` is now required for InfiniBand pools |
| Same-namespace traffic | the ad-hoc namespace had no `tenant-local` policy: the ranks could not reach the master (tenant-isolation allows ingress from host/remote-node/prometheus only), torchrun's rendezvous timed out twice (15 min each) and the JobSet's `RestartJobSet` policy recreated both pods each time (restarts 2: the restart-all path works). Tenant namespaces get the policy from charts/tenant; nothing to change |
| NCCL over InfiniBand | `NCCL INFO NET/IB : Made virtual device [0..7] name=mlx5_0..7 speed=400000` on every rank, 256 `via NET/IB` channel setups per pod, NCCL RDMA plugin v10 loaded; **1 GiB all-reduce over 16 GPUs on 2 nodes: 4.6 ms, algbw 232.8 GB/s, busbw 436.5 GB/s** (Nebius' NCCL tutorial calls > 300 GB/s a stable link) |
| Outputs | rank 0: `allreduce.py`, `out/allreduce.txt`, `STATUS.json`, its attempt record uploaded to `s3://serverless2-eval-eu-north1/operations/h5-ib-nccl/`; rank 1: attempt record only (`UPLOAD_SCOPE=rank0`); JobSet `Completed/AllJobsCompleted` |
| Cost | 2 x 8 H100 spot for about 1 h 20 min (cap 2.15/GPU-h, running price 0.79): about USD 13-34; the pool, its GPU cluster and pricing policy destroyed afterwards |

## Privileges of InfiniBand jobs (lane H6, 2026-10-08 19:05-20:25 UTC, hub, temporary pool `h100-spot-8x-ib`)

Owner's question: do multi-node InfiniBand jobs need root or privileged pods on shared infrastructure, and what are the
limits. Measured on the same 2 x 8 H100 spot pool lane H5 used (fabric-3 GPU cluster, `gpu_settings.dra = true`,
`min_nodes 2`), with the JobSet the API renders for `distributed-run` (`nodes: 2`, `interconnect: required`,
`nvcr/nvidia/pytorch:25.09-py3`, the 1 GiB all-reduce of lane H5), applied in a tenant namespace rendered by
charts/tenant with PSA `baseline` enforced, outside Kueue (no flavor for a temporary pool), and the one difference under
test: `capabilities: {drop: [ALL]}` on `main` instead of `{drop: [ALL], add: [IPC_LOCK]}`.

| Step | Result |
|---|---|
| Baseline pod on a node as the image ships it (`ulimit -l`, `/proc/1/limits`) | `Max locked memory 8388608` (8 MiB), soft and hard; the host's root shell and containerd itself have the same 8 MiB; no `LimitMEMLOCK` anywhere in the containerd unit, no `/etc/security/limits.d` entry |
| NCCL run with every capability dropped on such a node (`CapEff 0000000000000000`, uid 0 from the image) | the NICs are claimed and visible (NCCL `NET/IB : Made virtual device` x 64 per rank, GPU Direct RDMA enabled), then every rank fails in memory registration: `ibvwrap.c NCCL WARN Call to ibv_create_cq failed with error Cannot allocate memory`, `Call to ibv_reg_mr_iova2 failed`, `ncclSystemError`; the JobSet restarted all ranks 3 times (restart-all works) and went `Failed`. So the requirement is RLIMIT_MEMLOCK, not a device permission: RDMA pins memory and `CAP_IPC_LOCK` only matters because it bypasses that limit |
| Fix: unlimited memlock on the container runtime (`[Service] LimitMEMLOCK=infinity` drop-in on the containerd unit; written by the GPU pools' cloud-init at boot, infra/cluster and stack/modules/cluster, and by the node-config DaemonSet on older nodes) | pool rolled through Terraform (`plan` 3 node groups to change, apply, `plan` clean): a baseline pod on a fresh node reports `Max locked memory unlimited unlimited`, with `runAsNonRoot`, `drop: [ALL]`, seccomp `RuntimeDefault` |
| NCCL run on the fresh nodes with every capability dropped (`CapEff 0000000000000000`), namespace PSA `baseline`, no `IPC_LOCK` | `Completed / AllJobsCompleted`, 0 restarts, both ranks exit 0; 64 `NET/IB` virtual devices and 256 `via NET/IB` channel setups per rank, 0 NCCL warnings, 0 registration failures; **1 GiB all-reduce over 16 GPUs: 4.7 ms, algbw 230.6 GB/s, busbw 432.3 GB/s** (lane H5 with IPC_LOCK: 436.5 GB/s); rank 0 uploaded `out/allreduce.txt` and `STATUS.json`, every pod its attempt record |
| Hot restart of containerd through a DaemonSet on nodes already running IB pods (the first attempt to apply the drop-in) | both nodes went `NotReady` ("container runtime is down", `/run/containerd/containerd.sock` unreachable) and never recovered until the pool rolled; the cause could not be read without node access. The shipped mechanism is the boot-time cloud-init; the node-config catch-all restarts containerd only on nodes that still have a bounded limit, which after this change are nodes created before it |
| What this means for the security posture | the API never renders a privileged pod, hostNetwork, hostPID, hostPath or an added capability for InfiniBand runs; runner sidecars run as uid 10001; the model container keeps its image's user (root in the NVIDIA images, with zero effective capabilities); every tenant namespace stays at PSA `baseline` (the `infiniband: true` / `privileged` escalation of lane H5 is removed). InfiniBand traffic between GPU clusters is isolated by Nebius with partition keys; the NICs reach a pod only through its DRA claim |
| Cost | 2 x 8 H100 spot (plus 2 replacement nodes during the roll) for about 1 h 20 min: about USD 25-45 at the running spot price (cap 2.15/GPU-h); pool, GPU cluster and pricing policy destroyed afterwards, `plan hub` clean |

Limits of multi-node runs are written down in docs/JOBS.md "Limits of multi-node runs" (one pool = one GPU cluster = one
fabric per run; 8-GPU presets; `min_nodes >= 1`; ExactCount NIC claims; restart-all on preemption; the fabric table).
`interconnect: required` is now the default for `nodes > 1`; TCP only on an explicit `none`; 400 when no pool of the run's
classes has a fabric (unit tests).

## Fresh deployment from the library copy (2026-10-08 20:20-21:50 UTC, lane J1)

The exact directory that goes into the Nebius Solutions Library (`tools/library-sync.sh` output) was copied to a
scratch directory and the README quick start followed as a new user would: `terraform.tfvars` from the example
with name `s2pr`, a dedicated control cluster and one region (eu-north1, one `h100-spot-1x` pool, max 1), 256 GiB
filesystem and caches, tenant `eval`, the four bundled models, the acceptance probe. Nothing from the source
repository or the reference fleet was used except the eval tenant's GROMACS inputs (24 MB), copied into the new
tenant's own bucket with its own key.

| Step | Time | Result |
|---|---|---|
| `state-bucket.sh`, `preflight` | 18 s, 2 s | bucket `s2pr-tfstate`; preflight passes |
| `apply cloud` | 14 min 6 s | 29 resources: 2 clusters, pools, 256 GiB filesystem, 2 static IPs, identities, registry, buckets |
| `tools/images.sh build` | 4 min 40 s | api 0.8.1, dispatcher 0.1.6, jobs 0.1.4, ops 0.1.9, ui 0.2.2 pushed to the new registry |
| `apply` (platform hub, platform control, models hub, models control) | 19 min 32 s | platform hub 106 resources and platform control 111 resources in ONE pass each; models hub 10; models control stopped on the acceptance probe (see defect 1) |
| probe after the fix | 2 min 7 s | hello-run SUCCEEDED in 71 s; Qwen answered through the edge after a 502 while the endpoint scaled from zero |
| runs through the public API (tenant key from the outputs) | 5 min 16 s for all three | hello-run SUCCEEDED 49 s; container-run (busybox, `sleep 30`) SUCCEEDED 92 s; GROMACS 5000 steps SUCCEEDED 191 s on the H100 spot node, 202 ns/day, artifacts `run.{xtc,edr,gro,log,cpt}`, `topol.tpr` in the tenant bucket |
| Qwen sync call without `model` in the body | 31 s | HTTP 200, "Hello! How can I help you today?", served model `qwen` injected by the API |
| console `/config.json`, Grafana | | API URL and both Grafana URLs served at runtime; Grafana login page 200 on both clusters |
| `make check` inside the copy | | 41 tests, every stage and chart renders |
| `destroy` | 4 attempts, about 35 min of waiting in total | see defects 2 and 3; cloud destroy clean afterwards |
| cost | | about USD 5 (two CPU clusters for 90 min, one H100 spot node for 70 min, storage) |

Defects found by this run, fixed on the branch that carries this record:

1. **No certificate on any fresh fleet.** The example's `acme.email = "ops@example.com"` is refused by Let's
   Encrypt (`invalidContact: contact email has forbidden domain "example.com"`), cert-manager never issues the
   wildcard, the https listener stays unprogrammed and the probe times out (curl exit 28). The schema now
   validates the mailbox and refuses example.com/.test/.invalid; the example carries `CHANGE-ME@your-company.com`.
   Lane H2's test had used a real address, which hid this.
2. **`destroy` stopped on `BucketNotEmpty`** at the tenant bucket (run inputs and outputs inside), and would
   have stopped again at the backups bucket (cost reports). Disposable buckets are now emptied by a destroy-time
   provisioner with the bucket's own key (`stack/scripts/empty-bucket.sh`; the AWS CLI is a prerequisite for
   destroying with `protect_data = false`).
3. **`destroy platform` waited 15 minutes on the Kueue release and failed** on both clusters: Helm's
   uninstall `--wait` watches `kueue-batch-admin-role`/`kueue-batch-user-role`, and the kube-controller-manager's
   `clusterrole-aggregation-controller` re-creates both by server-side apply while Helm deletes the roles that
   feed them (managedFields manager on the stale roles: `clusterrole-aggregation-controller`, Apply). Deleting
   the re-created roles by hand let the hub's uninstall finish within a minute. Kueue is now installed and
   uninstalled without Helm's wait; `terraform_data.kueue_ready` waits for the controller rollout before wave 2,
   and `terraform_data.kueue_uninstall_cleanup` removes the stale roles after the uninstall.
4. The bundled `gromacs` class references an image the fleet's registry does not have until the user builds and
   pushes it (`models/gromacs/image`); the pre-pull reported `ImagePullBackOff` for it. README quick start says so
   now; the run itself used the image copied from the reference fleet's registry.

The reference fleet (`serverless2-*`) was not touched: its Terraform plan is unchanged (checked after the destroy).
