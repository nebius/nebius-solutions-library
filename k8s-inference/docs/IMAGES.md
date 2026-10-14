# Images: one host, cached per region, shared between nodes

*For operators: how images reach the nodes (source registry, per-cluster cache, peer-to-peer layers, pre-pull) and how to add or mirror one.*

Every image a workload uses is named through one logical registry host,
`registry.serverless2.local` (`terraform.tfvars` `images.host`). The reference
is the same in every region; what differs is where the bytes come from:

```
build host ── docker buildx --push ──▶ source registry (images.source: by default the Container
                                       Registry the cloud stage creates in the hub project)
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
upstream (`images.upstreams`, plus `nebius` = `images.source`):

| reference | comes from |
|---|---|
| `registry.serverless2.local/nebius/serverless2/api:<tag>` | `<images.source>/serverless2/api:<tag>` (the platform images, tags from `images.versions`) |
| `registry.serverless2.local/nebius/my-model:1.0` | `<images.source>/my-model:1.0` (your own images) |
| `registry.serverless2.local/nvcr/<org>/<image>:<tag>` | `nvcr.io/<org>/<image>:<tag>` (NGC key in the cache, no pull Secret on pods) |
| `registry.serverless2.local/docker/library/busybox:1.36` | Docker Hub official image |
| `registry.serverless2.local/docker/kserve/huggingfaceserver:v0.20.0-gpu` | Docker Hub |
| `registry.serverless2.local/ghcr/<org>/<repo>:<tag>` | ghcr.io |

Digests are preserved end to end (`...@sha256:` references work, Zot syncs by
digest), so a model can pin a digest and the cache serves exactly it.

## Components (all existing tools; the only custom pieces are one DaemonSet and the Zot config template)

| piece | what | where in the repo |
|---|---|---|
| Zot 2.1.22 (CNCF) | pull-through cache: sync extension `onDemand` from every upstream, `docker2s2` compat, anonymous read only (pushes get 401), GC + retention (tags not pulled for `images.cache.keep_days` are dropped, untagged after 24 h), Prometheus metrics | app `clusters/common/apps/zot.yaml` (chart `zot` 0.1.128), values `clusters/common/values/zot.yaml`, config rendered by `charts/fleet/templates/image-cache.yaml` from the `images` block into ConfigMap `registry/zot-config` |
| Spegel 0.7.4 (CNCF sandbox) | peer-to-peer distribution of layers between the nodes of a cluster, and the writer of containerd's `hosts.toml` for the logical host and the public registries (`additionalMirrorTargets` = the Zot NodePort) | app `clusters/common/apps/spegel.yaml` (native OCI chart), values `clusters/common/values/spegel.yaml` |
| node-config DaemonSet | containerd on the GPU node images has no registry `config_path`: a drop-in `/etc/containerd/conf.d/serverless2-registry.toml` (`config_path`, `discard_unpacked_layers = false`) and one containerd restart per node, through the host's systemd (`hostPID`, privileged init container; the CPU images already ship the setting and are left alone) | `charts/platform/templates/node-config`, app `node-config` |
| Secret `registry/zot-sync-credentials` | `credentials.json`: `{"<source registry host>": {"username": "iam", "password": "<static key>"}, "nvcr.io": {"username": "$oauthtoken", "password": "<NGC key>"}}`; one per cluster, never in git | written by the platform stage (`stack/platform/secrets.tf`) from the environment variable `secrets.registry_key_env` names (default `REGISTRY_STATIC_KEY`); the operator issues the key once per fleet with `stack/scripts/registry-static-key.sh` (the Nebius CLI's `static-key issue` for the hub ops SA, `CONTAINER_REGISTRY`, 365 days; `--rotate` for a new one), a plan never issues one |
| Knative | `registries-skipping-tag-resolving: registry.serverless2.local` (the host resolves on nodes, not in the cluster network) | `charts/platform/templates/knative-serving/knative-serving.yaml` |

There is no per-region image rewriting: `services/api` renders references as
written (a plain reference such as `vllm/vllm-openai:v0.11.0` is normalised
onto the logical host, `services/api/models.py`), and the dispatcher places
every run at queue time (`docs/SCHEDULING.md`), so nothing in a Job depends
on the region it lands in.

## Pushing an image

The cloud stage creates the source registry (`<name>-images` in the hub project; `images.source` to use an
existing one). `tools/images.sh build <registry>` builds and pushes the four platform images (api,
dispatcher, jobs, ops) once, tagged by `images.versions`; `tools/images.sh copy <from> <to>` copies them
from another registry without a build. Any other image is the same three steps: build once, push to the
source registry, reference it through the host:

```sh
docker buildx build --push --platform linux/amd64 -t <images.source>/my-model:1.0 .
# reference: registry.serverless2.local/nebius/my-model:1.0
```

The ops image pins the Nebius CLI and crane by version and SHA-256
(`services/ops/Dockerfile`: build args `NEBIUS_CLI_VERSION` / `NEBIUS_CLI_SHA256`,
`CRANE_VERSION` / `CRANE_SHA256`, download host `NEBIUS_CLI_BASE_URL`); a bump
sets both the version and the sum, and the build fails on a mismatch.

Tags are immutable by convention: the caches keep what they have synced and do
not re-check an existing tag upstream. A new build is a new tag; `images.versions`
names the platform tags in use, and the pre-pull list and the model definitions
name theirs.

## Switching the source registry

1. Copy the images in use into the new registry (same paths), with
   `tools/images.sh copy <old> <new>` for the platform images, a Job in the
   cluster (`services/ops` image: `crane copy <old>/<path> <new>/<path>`; the
   ops SA mints the tokens) or `crane copy` with a static key from a laptop.
2. Change the one key: `images.source` in `terraform.tfvars`. Terraform updates
   `registry/zot-config` on every cluster; Zot picks the new upstream up for
   every repository it does not already hold (rotate the `zot` StatefulSet to
   apply at once: `kubectl -n registry rollout restart statefulset/zot`).
3. Make sure the credential in `registry/zot-sync-credentials` can read the
   new registry (same host, same project: nothing to do).

## Warm-up and sizing

- The pre-pull list (`charts/fleet` `prepull.images`, derived per cluster by the
  config stage from the bundled run classes' images) is the per-GPU-pool
  pre-pull DaemonSet AND the cache's warm-up set: the first node of a pool
  pulls those images through the cache (which fetches them from the upstream
  once), later nodes get them from the peers.
- Cache size: `images.cache.size_gib` (1 TiB per GPU region, network-ssd,
  about USD 100/month per region at list price), overridable per cluster with
  `image_cache_size_gib` in the `control_plane` block or a region block
  (control: 100 Gi). The claim `registry/zot-pvc-zot-0` is rendered by
  `charts/fleet` (image-cache.yaml) and referenced by the Zot StatefulSet
  (`pvc.create: false`), so a new cluster gets it before Zot starts and the
  StatefulSet mounts a separately owned cache claim, preserving it across chart upgrades.
- Orders of magnitude on a GPU node, through the logical host: a 500 MB image
  pulls in 20 to 30 s cold (the cache fetching it from the source registry)
  and in under 10 s once a peer or the cache has it; a 16 GB NIM image takes
  about ten minutes for the cache to sync from NGC (containerd's first request
  ends `NotFound` while the sync runs, the kubelet's retry pulls from the
  cache) and about three and a half minutes on the next node of the cluster.
  Pre-pull what a cold start must not wait for.

## Adding an upstream

Add an alias to `images.upstreams` (`<alias> = "https://<host>"`), add its
credentials to `registry/zot-sync-credentials` when it needs any
(`REGISTRY_CREDENTIALS` in the environment at apply time, `secrets` block), and
reference `registry.serverless2.local/<alias>/<path>`.

## The setting at boot

Two files are written by cloud-init on every GPU node (`stack/modules/cluster/main.tf`,
`gpu_cloud_init`): the containerd drop-in below, and the mirror file
`/etc/containerd/certs.d/registry.serverless2.local/hosts.toml` pointing at the
Zot NodePort on the node itself (`server = "http://127.0.0.1:30500"`). Spegel
rewrites that file with its peers once it runs (it keeps a copy under
`certs.d/_backup`). Without the second file the first pulls of a fresh node, which
happen before Spegel has started, fall back to DNS for the logical host and sit in
the kubelet's pull back-off for minutes.

The `node-config` DaemonSet is the catch-all for a node that lacks the drop-in: such
a node pulls its first workload image before the DaemonSet has restarted containerd
(one failed pull, retried by the kubelet about 10 s later; with several pods landing
at once the kubelet's pull back-off stretches that to minutes). It does nothing on a
node that already has the setting (same marker file). The cloud-init fragment
Terraform renders:

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

## Pitfalls

- Spegel's chart has post-delete hook objects (`spegel-cleanup`); rendering
  the chart with `helm template` without `--no-hooks` and applying the output
  creates a DaemonSet that deletes the mirror configuration on every node.
  Helm handles the hook correctly; never apply a plain `helm template` of it.
- containerd's `config_path` is read at start. On the GPU node images it is not
  set, and the node image's `config.toml` already defines the
  `cri.containerd` table: a drop-in under `conf.d` is the only safe way to add
  settings (defining a table twice makes containerd refuse to start, the
  node goes NotReady and the autoscaler replaces it).
- `systemctl` through `chroot` fails from a container ("Failed to connect to
  bus"); the DaemonSet uses `hostPID` and `nsenter -t 1`.
- The Spegel hostPort (30020) is not reachable on the loopback address through
  Cilium; containerd falls through to the NodePort (30021) within its 200 ms
  dial timeout, which costs nothing noticeable.

## The cache as an add-on (`images.cache.enabled`)

The cache is on by default. `images.cache = { enabled = false }` in the tfvars leaves it out of a fleet
whose nodes can reach their registries directly and that does not need the per-region warm layer: no Zot
release, no `registry` namespace, no cache claim, no registry static key
(`secrets.registry_key_env` is not required, Secret `registry/zot-sync-credentials` is not written),
no `node-config` DaemonSet and no containerd drop-in in the GPU pools' cloud-init (`stack/modules/cluster`:
a change of this flag rolls the GPU pools), and Knative's `registries-skipping-tag-resolving` lists `nvcr.io`
only. Spegel stays and still mirrors the public registries between the nodes (on the CPU node images, whose
containerd reads `certs.d`; the GPU node images lack that setting without the drop-in, so there Spegel's
`hosts.toml` files have no effect). What then names an image:

| reference | with the cache | without |
|---|---|---|
| the platform images (`images.versions`) | `registry.serverless2.local/nebius/serverless2/<component>:<tag>` | `<images.source>/serverless2/<component>:<tag>`, exactly the tags `tools/images.sh build` pushes (`<registry>/serverless2/<component>:<tag>`, `images.sh list` shows them); nothing to re-tag |
| the bundled classes (`catalog/models`) and the pre-pull list | as written in the catalog (`registry.serverless2.local/docker/library/busybox:1.36`) | rewritten once by `stack/config` to the upstream (`docker.io/library/busybox:1.36`) |
| a model defined through the API (`image: vllm/vllm-openai:v0.11.0`) | normalised to `registry.serverless2.local/docker/vllm/vllm-openai:v0.11.0` (`services/api/models.py`) | left as written (`IMAGES_HOST` is empty on the API) |
| helper images of the platform stage (curl, the postgres client) | through the cache | `docker.io/...`, `ghcr.io/...` |

What the operator then provides: the platform images in a registry the nodes can pull from. The Managed
Kubernetes node identity (`<cluster>-nodepull`, `stack/modules/cluster`) has `viewer` on its own project, so
the fleet's registry in the hub project is reachable from the hub region's nodes; a region in another
project needs read access on the hub project's registry for its node identity (or `images.source` pointing at a
registry of its own, filled with `tools/images.sh copy`), and private upstreams (NGC) need a pull Secret on
the pods, which the platform does not add. The setting is validated by rendering (`make check` renders both
charts with the cache off), not on a live fleet.
