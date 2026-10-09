# Images: one host, cached per region, shared between nodes

*For operators: how images reach the nodes (source registry, per-cluster cache, peer-to-peer layers, pre-pull) and how to add or mirror one.*

Every image a workload uses is named through one logical registry host,
`registry.serverless2.local` (fleet.yaml `images.host`). The reference is the
same in every region; what differs is where the bytes come from:

```
build host ── docker buildx --push ──▶ source registry (fleet.yaml images.source,
                                       Nebius Container Registry serverless2-models, hub project)
                                                 │
   nvcr.io  ghcr.io  docker.io  quay.io  registry.k8s.io   (public upstreams, NGC with a key)
        │       │        │         │          │           │
        ▼       ▼        ▼         ▼          ▼           ▼
   ┌─────────────────────────────────────────────────────────┐   one per cluster, on the system
   │ Zot pull-through cache  (namespace registry, PVC 1 TiB) │   pool; NodePort 30500
   └─────────────────────────────────────────────────────────┘
                     ▲ miss                     ▲ miss
            ┌────────┴────────┐        ┌────────┴────────┐
   node A   │ Spegel (peer)   │◀──────▶│ Spegel (peer)   │   node B   layers already on any node of
            │ containerd      │  P2P   │ containerd      │           the cluster come from that node
            └─────────────────┘        └─────────────────┘
   /etc/containerd/certs.d/registry.serverless2.local/hosts.toml (written by Spegel):
     1. http://<node>:30020 / :30021   Spegel on this node, which asks its peers
     2. http://127.0.0.1:30500          the cluster's Zot cache (NodePort on the loopback address)
```

Naming: `registry.serverless2.local/<alias>/<path>`, where the alias picks the
upstream (fleet.yaml `images.upstreams`, plus `nebius` = `images.source`):

| reference | comes from |
|---|---|
| `registry.serverless2.local/nebius/serverless2/api:0.7.1` | `<images.source>/serverless2/api:0.7.1` (our builds) |
| `registry.serverless2.local/nebius/my-model:1.0` | `<images.source>/my-model:1.0` (your own images) |
| `registry.serverless2.local/nvcr/<org>/<image>:<tag>` | `nvcr.io/<org>/<image>:<tag>` (NGC key in the cache, no pull Secret on pods) |
| `registry.serverless2.local/docker/library/busybox:1.36` | Docker Hub official image |
| `registry.serverless2.local/docker/kserve/huggingfaceserver:v0.20.0-gpu` | Docker Hub |
| `registry.serverless2.local/ghcr/<org>/<repo>:<tag>` | ghcr.io |

Digests are preserved end to end (`...@sha256:` references work, Zot syncs by
digest), so a catalog entry can pin a digest and the cache serves exactly it.

## Components (all existing tools; the only custom pieces are one DaemonSet and the Zot config template)

| piece | what | where in the repo |
|---|---|---|
| Zot 2.1.22 (CNCF) | pull-through cache: sync extension `onDemand` from every upstream, `docker2s2` compat, anonymous read only (pushes get 401), GC + retention (tags not pulled for `images.cache.keep_days` are dropped, untagged after 24 h), Prometheus metrics | app `clusters/common/apps/zot.yaml` (chart `zot` 0.1.128), values `clusters/common/values/zot.yaml` (+ `clusters/control/values/zot.yaml`: 100 Gi), config rendered by `charts/fleet/templates/image-cache.yaml` from fleet.yaml `images` into ConfigMap `registry/zot-config` |
| Spegel 0.7.4 (CNCF sandbox) | peer-to-peer distribution of layers between the nodes of a cluster, and the writer of containerd's `hosts.toml` for the logical host and the public registries (`additionalMirrorTargets` = the Zot NodePort) | app `clusters/common/apps/spegel.yaml` (OCI chart, repository Secret `clusters/control/apps/overlays/ops/argocd-helm-repos.yaml`), values `clusters/common/values/spegel.yaml` |
| node-config DaemonSet | containerd on the GPU node images has no registry `config_path`: a drop-in `/etc/containerd/conf.d/serverless2-registry.toml` (`config_path`, `discard_unpacked_layers = false`) and one containerd restart per node, through the host's systemd (`hostPID`, privileged init container; the CPU images already ship the setting and are left alone) | `clusters/common/manifests/node-config`, app `node-config` |
| Secret `registry/zot-sync-credentials` | `credentials.json`: `{"cr.eu-north1.nebius.cloud": {"username": "iam", "password": "<static key>"}, "nvcr.io": {"username": "$oauthtoken", "password": "<NGC key>"}}`; one per cluster, never in git | created at bootstrap (docs/BOOTSTRAP.md); the static key: `nebius iam static-key issue --parent-id <hub project> --account-service-account-id <ops SA> --service CONTAINER_REGISTRY --expires-at <date>` (up to 3 years) |
| Knative | `registries-skipping-tag-resolving: registry.serverless2.local` (the host resolves on nodes, not in the cluster network) | `clusters/common/manifests/knative/knative-serving.yaml` |

No per-region image rewriting exists any more: `services/api` renders the
catalog's references as they are, the dispatcher places every run at queue
time (`docs/SCHEDULING.md`), per-region registries and the `mirror-image` Job
are retired. The `eu-south1` Nebius registry `serverless2-models` of that
region is no longer used by anything.

## Pushing an image

Build once, push to the source registry, reference it through the host:

```sh
SRC=$(python3 -c 'import yaml; print(yaml.safe_load(open("fleet.yaml"))["fleet"]["images"]["source"])')
docker buildx build --push --platform linux/amd64 -t $SRC/serverless2/api:0.7.1 -f services/api/Dockerfile .
# reference: registry.serverless2.local/nebius/serverless2/api:0.7.1
```

Tags are immutable by convention: the caches keep what they have synced and do
not re-check an existing tag upstream. A new build is a new tag (every
`services/*` README bumps a version); the pre-pull list and the manifests name
the tag.

## Switching the source registry

1. Copy the images in use into the new registry (same paths), e.g. with a Job
   on the hub (`services/ops` image: `crane copy <old>/<path> <new>/<path>`; the
   ops SA mints the tokens), or `crane copy` with a static key from a laptop.
2. Change the one key: fleet.yaml `images.source`. Argo CD re-renders
   `registry/zot-config` on every cluster; Zot picks the new upstream up for
   every repository it does not already hold (rotate the `zot` StatefulSet to
   apply at once: `kubectl -n registry rollout restart statefulset/zot`).
3. Make sure the credential in `registry/zot-sync-credentials` can read the
   new registry (same host, same project: nothing to do).

Done on 2026-10-07: `k8s-inference-h100` (FS2-owned) -> `serverless2-models`
(`cr.<region>.nebius.cloud/<registry id>`, Terraform, hub project): the
eight references in use were copied in 42 s by an in-cluster Job, the key
flipped, the three caches restarted.

## Warm-up and sizing

- `clusters/<cluster>/values/fleet.yaml` `prepull.images` is the per-GPU-pool
  pre-pull DaemonSet (charts/fleet) AND the cache's warm-up set: the first node
  of a pool pulls the catalog's images through the cache (which fetches them
  from the upstream once), later nodes get them from the peers.
- Cache size: fleet.yaml `images.cache.size_gib` (1 TiB per GPU region,
  network-ssd, about USD 100/month per region at list price), overridable per
  cluster with `image_cache_size_gib` in the control block or a region block
  (control: 100 Gi). The claim `registry/zot-pvc-zot-0` is rendered by
  `charts/fleet` (image-cache.yaml) and referenced by the Zot StatefulSet
  (`pvc.create: false`), so a new cluster gets it before Zot starts and the
  StatefulSet carries no volumeClaimTemplate (which the fleet Argo CD could not
  bring to Synced under server-side apply). Raising the size in fleet.yaml
  expands the volume in place (CSI expansion); it is never shrunk. Retention
  keeps the 20 most recently pulled tags per repository and anything pulled or
  synced within `keep_days`.
- Public references (`docker.io/...`, `nvcr.io/...` with a pull Secret) still
  work on every node: Spegel shares their layers between nodes; the cache
  only answers for the logical host.

## Adding an upstream

Add an alias to fleet.yaml `images.upstreams` (`<alias>: https://<host>`), add
its credentials to `registry/zot-sync-credentials` when it needs any, and
reference `registry.serverless2.local/<alias>/<path>`.

## The setting at boot (Terraform, applied 2026-10-07)

Two files are written by cloud-init on every GPU node (`infra/cluster/main.tf`,
`gpu_cloud_init`): the containerd drop-in below, and the mirror file
`/etc/containerd/certs.d/registry.serverless2.local/hosts.toml` pointing at the
Zot NodePort on the node itself (`server = "http://127.0.0.1:30500"`). Spegel
rewrites that file with its peers once it runs (it keeps a copy under
`certs.d/_backup`). Without the second file the first pulls of a fresh node, which
happen before Spegel has started, fell back to DNS for the logical host and sat in
the kubelet's pull back-off for minutes (measured 2026-10-07, hub H100 pool).

A fresh GPU node pulls its first workload image before the `node-config`
DaemonSet has restarted containerd (one failed pull, retried by the kubelet
about 10 s later: measured 2 min 5 s from node creation to a running job
container; with several pods landing at once the kubelet's pull back-off
stretched that to minutes on 2026-10-07). Since 2026-10-07 the same drop-in is
written by cloud-init in the GPU node-group template (`infra/cluster/main.tf`,
`gpu_cloud_init`, next to the `weights` mount), so a fresh node's first pulls
already go through the cache. The DaemonSet stays as the catch-all and does
nothing on a node that already has the setting (same marker file). The
cloud-init fragment Terraform renders:

```yaml
#cloud-config
write_files:
  - path: /etc/containerd/conf.d/serverless2-registry.toml
    content: |
      # serverless2 image cache (docs/IMAGES.md): per-registry mirror configuration (Spegel peers, Zot cache)
      version = 2
      [plugins."io.containerd.grpc.v1.cri".registry]
        config_path = "/etc/containerd/certs.d"
      [plugins."io.containerd.grpc.v1.cri".containerd]
        discard_unpacked_layers = false
runcmd:
  - mkdir -p /etc/containerd/certs.d
  - grep -q '^imports' /etc/containerd/config.toml || sed -i '1a imports = ["/etc/containerd/conf.d/*.toml"]' /etc/containerd/config.toml
  - touch /etc/containerd/certs.d/.serverless2-containerd-restarted
  - systemctl restart containerd
```

## Measured (2026-10-07; details in docs/VERIFICATION.md)

| pull on a node, through the logical host | time |
|---|---|
| `docker/library/busybox:1.36` (2 MB), cold (Zot fetched it from Docker Hub) | 0.9 s |
| `nebius/serverless2/jobs:0.1.3` (44 MB; 0.1.4 since 2026-10-08), cold (from the source registry) | 2.4 s hub, 4.6 s eu-south1 |
| a 518 MB solver image, cold, hub | 20.9 s |
| same, eu-south1 GPU node, cache warm (Zot sync from the hub registry took 12 s before) | 29 s first node, 7.8 s re-pull |
| same, fresh eu-south1 spot node (a run landing there), cache warm | 12.3 s |
| a 16.4 GB NIM image, cold: Zot fetches from NGC | 9 min 38 s sync; containerd's first request ends `NotFound` at that moment, the kubelet's retry pulls from the cache |
| same, second GPU node of the cluster (cache + Spegel peer) | 3 min 27 s |

## Pitfalls found on the way

- Spegel's chart has post-delete hook objects (`spegel-cleanup`); rendering
  the chart with `helm template` without `--no-hooks` and applying the output
  creates a DaemonSet that deletes the mirror configuration on every node.
  Argo CD handles the hook correctly; never apply a plain `helm template` of it.
- containerd's `config_path` is read at start. On the GPU node images it is not
  set, and the node image's `config.toml` already defines the
  `cri.containerd` table: a drop-in under `conf.d` is the only safe way to add
  settings (defining a table twice makes containerd refuse to start and the
  node goes NotReady; one hub L40S node was lost to that during development and
  replaced by the autoscaler).
- `systemctl` through `chroot` fails from a container ("Failed to connect to
  bus"); the DaemonSet uses `hostPID` and `nsenter -t 1`.
- The Spegel hostPort (30020) is not reachable on the loopback address through
  Cilium; containerd falls through to the NodePort (30021) within its 200 ms
  dial timeout, which costs nothing noticeable.

## Terraform solution (2026-10-08)

The cloud stage creates the source registry (`<name>-images` in the hub project, `images.source` to use
an existing one); `tools/images.sh build <registry>` pushes the five platform images once (or
`tools/images.sh copy <from> <to>` copies them), tagged by `images.versions`. The platform stage renders
the cache configuration from the tfvars through `charts/fleet`, writes the cache credential Secret
(a static Container Registry key of the ops service account issued once per operator by
`stack/scripts/registry-static-key.sh`, cached under `stack/.secrets/`; NGC and other private upstreams
from the environment) and the containerd mirror files come from the GPU pools' cloud-init
(`stack/modules/cluster`). Every image reference stays `registry.serverless2.local/<alias>/<path>`.
