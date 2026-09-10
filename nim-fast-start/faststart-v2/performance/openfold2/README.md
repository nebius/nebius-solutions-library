# OpenFold2 production-native provisioned-node result

## Fresh fail-closed n=20 qualification

The fresh homogeneous cohort `of2-n20-v3-20260818t1421z` is the selected
qualification. All 20 scheduled attempts were admitted, completed strict
semantic validation, and passed UID-bound cleanup: **20/20 qualified, 20/20
cleanup, failed-attempt denominator 0/20**. The conservative CLOCK_BOOTTIME
upper-bound nearest-rank p95 was **17.629887 seconds**, so OpenFold2 passes the
strict `<30 s` T0-to-second-complete-body target.

| Measurement | p50 | Nearest-rank p95 | Maximum |
|---|---:|---:|---:|
| T0 to successful semantic HTTP ready, observed UTC | 14.242080 s | 14.572160 s | 14.991581 s |
| T0 to HTTP ready, conservative BOOTTIME upper | 14.342258 s | 14.671991 s | 15.099141 s |
| T0 to Kubernetes Pod Ready, diagnostic | 15.387387 s | 16.109487 s | 16.124244 s |
| First inference, dispatch through complete HTTP body | 1.938516 s | 1.973362 s | 1.975756 s |
| Second inference, dispatch through complete HTTP body | 1.015083 s | 1.032614 s | 1.035316 s |
| T0 through first complete inference body, observed UTC | 16.172968 s | 16.489561 s | 16.933596 s |
| T0 through first complete inference body, conservative BOOTTIME upper | 16.273235 s | 16.586717 s | 17.041233 s |
| T0 through second complete inference body, observed UTC | **17.202273 s** | **17.532731 s** | **17.955461 s** |
| T0 through second complete inference body, conservative BOOTTIME upper | **17.302540 s** | **17.629887 s** | **18.063099 s** |

The complete 20-element arrays and p50/p95/max rows for all 14 retained clocks,
including the explicitly non-exact client API-return proxy, are in
`fresh-cohort-n20-results.tsv`. The API-return-proxy-to-call-2 p50/p95/max is
16.331869/16.670896/17.027670 seconds; it is diagnostic and is not substituted
for the primary pre-dispatch T0 clock.

Two evidence limitations apply to this qualification. Target-container GPU
checks passed, but all 20 OpenFold2 qualification receipts record privileged
host-driver Xid absence as unavailable/unproven because no task-scoped
privileged node-log collector was present. The semantic summaries also
reference 40 raw response bodies (two per attempt) that were not copied from
the probe containers or retained controller-side; across the OpenFold2 and
Boltz2 cohorts this is 80 unretained raw bodies. Response SHA-256 values, byte
counts, complete-body timestamps, strict semantic invariants/receipts, and the
pinned validator source are retained. Those retained checks do not substitute
for host-driver logs or controller-side raw response bodies.

This is a warm-instance cold start: the pinned H100 node was already Ready; the
exact target, restore-worker, and probe images were proven cached; and the M3
artifact and cache PVCs were already attached. T0 remained immediately before
client dispatch of target creation. The direct M3 artifact was not page-cache
preloaded and no artifact page-cache claim is made. Image setup and the GPU-zero
audit occurred before T0 and are excluded from every sample.

The private evidence root is
`<private-evidence-root>/of2-boltz-n20-20260818T121158Z`.
The source ledger SHA-256 is
`cefad84839f1e1e1794715abcdffdd2b10cc2bb25867b4466e7d30ea5cabcd6a`;
the aggregate SHA-256 is
`803a4c139b99d3016e6b4a9ab922dfaf18a11acb51de0ed921a112d1f7a44587`;
and the homogeneous-cohort classification receipt SHA-256 is
`c5831b7413d6540b7c09e1f697c1b9ad45bf2ab05cad2309f857d591ce355d6f`.
Acquisition was frozen at Git commit
`e3b41aecc7b49d31914fa970aa58903feef4d5d9` (production-equivalent commit
`77e4c6d5357a89321fa4f09d16392cdc1186c0db`) with the exact 18-source
instrumentation-contract SHA-256
`0661b8875e553da04581086178089be450327df949ce24b4b5019edec7357c4b`;
its receipt SHA-256 is
`ae2e330db2c67a94aa20d7a23f95e231f6a964e64997dabefb14e14c684d2c54`.
The prelaunch, image-residency, and pre-T0 GPU-zero receipt SHA-256 values are
`6b85e6cd73e6ac306c92af407c3ebc6e13af5caba0b8c3212a9fde59e5a67f20`,
`081a0183afe5d0be5906eab31c53d60acf953c8c06c79fa042a4024ba09a7a85`,
and `96534e434852c2101c06c8339421a7ba3741bacc47578967c23ce11b8a8d9a34`.
The post-cohort final-state receipt SHA-256 is
`d0436512a91c7ec6630678ae19c788c61147cf06ec92f30681c2447c6e216400`.
Its UID-cleaned GPU-zero audit receipt SHA-256 is
`705db22f54d08a02e06ff8c9663ab714b5ea61f9e7351c63653d52850f258e59`.
Its immutable `source.tree_sha256` label was incorrect—the value was the
40-hex Git SHA-1 tree object ID. The hash-bound correction sidecar SHA-256
`296c15052074d9c8b8310f24e87d3af19e2841ed9ffcc763b1fe614131ba52f2`
correctly names it `git_tree_oid` with algorithm SHA-1 and records no timing or
source-contract effect.

Two earlier admitted cohorts remain separate and contribute zero selected
samples. `of2-n20-20260818t1250z` is preserved as 3/3
`FAILED_INSTRUMENTATION` (ledger/classification SHA-256
`c88ce3bd528a34774b96b6cf85fae65781bb7588d273b7b6d88ec1085f83c409` /
`47875ceee14f7af20b7ed2ab5c093e04d5959f557689e539714e37af82702197`).
`of2-n20-v2-20260818t1313z` is preserved as 3/3
`FAILED_CLOCK_INSTRUMENTATION` (ledger/classification SHA-256
`fe125713bbd6a3a49839afeedb96556cd46e04330970f7d54d1917e1b97dbecb` /
`56cd6183f6db4b009896ff29f6f6b6f56dca10f020ac362702d42b3a6945e285`).
Neither cohort is retried, relabeled, or pooled with v3.

## Retained response-boundary n=3 result

The earlier direct-AIO production path completed two distinct strict OpenFold2
folds in a **14.236758-second median** from target submit through receipt of the
second complete HTTP body. Three consecutive clean restores passed on an
already provisioned H100 with storage attached and the exact image cached:

| Measurement | Median | Range |
|---|---:|---:|
| T0 to successful semantic HTTP ready | **11.365660 s** | 11.238689–11.913086 s |
| First inference, dispatch through complete HTTP body | **1.851894 s** | 1.844734–1.871146 s |
| Second inference, dispatch through complete HTTP body | **0.992264 s** | 0.984416–0.996515 s |
| T0 through second complete inference response | **14.236758 s** | 14.087336–14.756378 s |
| T0 to Kubernetes Pod Ready | **12.311987 s** | 11.542021–12.904340 s |
| Worker restore | **3.954 s** | 3.811–4.025 s |

The separate CPU probe Job is submitted as soon as the scheduler-created
target Pod has a bound UID and canonical PodSpec hash. It waits on the exact
OpenFold2 readiness endpoint, then sends exactly two fixed, distinct inference
requests through the run-scoped ClusterIP Service. This overlaps client
scheduling with restoration. The earlier sequential flow is retained as
historical evidence but used an older clock and is not mixed into the corrected
submit-edge comparison.

The retained n=3 response-boundary runs are in
`provisioned-response-boundary-results.tsv`. The historical early-probe runs
remain in `provisioned-early-probe-results.tsv`, and the earlier sequential
comparison is retained in `provisioned-results.tsv`.
That older probe did not retain a successful HTTP-readiness timestamp, so its
HTTP-ready cells are `NA`; its 10–11 second values are preserved in
the separately named Kubernetes-Ready column. Its Kubernetes and total fields
have also been losslessly rebased to `target-submit-at.txt`; immutable sidecars
record HTTP readiness as unavailable rather than substituting Pod Ready.

The exact model image was
`registry.example.invalid/faststart/openfold2@sha256:fc64916731fee39e124225829dca78e80ec24fe1891be47057d0d69209b93ab4`.
The native artifact was `openfold2-native-f7-v1`, version `1`, with manifest
SHA-256
`78368af3e6f143d7dc681632c4150b29f6354717103638b56e776244d9631b04`.
The optimized runs use semantic-validator SHA-256
`8da1693931ce62604917a74b1518ac29ee28bdcb89fbe389bee13912351ac9ce`.
The response-boundary rerun is pinned to corrected validator SHA-256
`4f3e70ef29ea9cd3113c09e6f63bd15b4d9826bf64d7d16972c6c3d0eef3090e`.

The source evidence root is intentionally retained outside the repository at:

```text
<private-evidence-root>/openfold2-native-f7-20260818T0221Z
```

The current evidence directories are `runs/of2rb1b-0913`,
`runs/of2rb2-0916`, and `runs/of2rb3-0918`. They contain the target binding,
worker receipt, exact readiness wait, two response-body timestamps, Kubernetes
object captures, and derived `canary-evidence.json`. The historical optimized
evidence directories are `runs/p6-earlyprobe`, `runs/p7-earlyprobe`, and
`runs/p8-earlyprobe`. They contain the target
binding, worker receipt, readiness wait, semantic summary, Kubernetes object
captures, derived `canary-evidence.json`, and immutable
`corrected-submit-edge-timings.json`. The raw `canary-evidence.json` field named
`demand_to_http_ready` actually used Kubernetes Ready in these retained runs;
the corrected sidecar supersedes that stale label without overwriting it.
Setup-only `p5-earlyprobe` is
explicitly excluded because its first readiness matcher did not recognize the
image's JSON-object health response and sent no inference requests.

Two response-boundary setup attempts are also excluded. The first image preload
omitted the existing private-registry pull secret and received HTTP 403 before
any target was created. `runs/of2rb1-0909` submitted a target, but its restore
worker could not schedule with a 1-CPU request beside the preserved CPU-only
holders; it completed no semantic call. The counted path requests 500m for
scheduling while retaining the 4-CPU worker limit. The exact target image's
264.996-second preload occurred before T0 and is excluded from every trial.

This is the warm-instance cold-start result: `T0` is recorded immediately
before target creation on an already provisioned H100 with the exact images,
model cache, native checkpoint, and storage attached. HTTP readiness is the
first successful application readiness response. Both inference timers end when
the complete HTTP response body is received, before persistence and semantic
validation; the second request is distinct and immediately follows the first.
Newly-created-node latency is measured and reported separately.
