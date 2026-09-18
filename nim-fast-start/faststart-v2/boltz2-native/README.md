# Boltz2 native Dynamo fast start

## Fresh fail-closed n=20 qualification

The fresh homogeneous cohort `b2-n20-v3-20260818t1532z` produced **20/20
semantically valid, instrumentation-valid runs and 20/20 cleanups**, with a
failed-attempt denominator of **0/20**. Its aggregate result is nevertheless
**SLO FAIL**, not PASS: the conservative CLOCK_BOOTTIME-upper nearest-rank p95
from pre-dispatch T0 through the second complete response body was
**30.310246 seconds**, which is not `<30 s`.

| Measurement | p50 | Nearest-rank p95 | Maximum |
|---|---:|---:|---:|
| T0 to successful semantic HTTP ready, observed UTC | 26.971552 s | 28.328014 s | 28.996664 s |
| T0 to HTTP ready, conservative BOOTTIME upper | 27.070530 s | 28.429408 s | 29.095697 s |
| T0 to Kubernetes Pod Ready, diagnostic | 28.173215 s | 29.263568 s | 29.762389 s |
| First inference, dispatch through complete HTTP body | 1.408064 s | 1.489046 s | 1.500368 s |
| Second inference, dispatch through complete HTTP body | 0.282071 s | 0.300494 s | 0.361623 s |
| T0 through first complete inference body, observed UTC | 28.396487 s | 29.735714 s | 30.413590 s |
| T0 through first complete inference body, conservative BOOTTIME upper | 28.495542 s | 29.837204 s | 30.512699 s |
| T0 through second complete inference body, observed UTC | **28.794544 s** | **30.208757 s** | **30.923531 s** |
| T0 through second complete inference body, conservative BOOTTIME upper | **28.892235 s** | **30.310246 s** | **31.022641 s** |

The complete 20-element arrays and p50/p95/max rows for all 14 retained clocks
are in `fresh-cohort-n20-results.tsv`. The explicitly non-exact client
API-return-proxy-to-call-2 p50/p95/max is
27.867918/29.329832/30.025449 seconds and is retained only as a diagnostic.

Two evidence limitations apply to this qualification. Target-container GPU
checks passed, but all 20 Boltz2 qualification receipts record privileged
host-driver Xid absence as unavailable/unproven because no task-scoped
privileged node-log collector was present. The semantic summaries also
reference 40 raw response bodies (two per attempt) that were not copied from
the probe containers or retained controller-side; across the OpenFold2 and
Boltz2 cohorts this is 80 unretained raw bodies. Response SHA-256 values, byte
counts, complete-body timestamps, strict semantic invariants/receipts, and the
pinned validator source are retained. Those retained checks do not substitute
for host-driver logs or controller-side raw response bodies.

This is a warm-instance cold start on the already Ready pinned H100 node with
storage attached. The exact target, restore-worker, and probe image digests were proven
resident outside T0. The direct M3 artifact was identity-bound but was not
page-cache preloaded. A separate UID0 read-only auditor performed a complete
422.854590-second read of the attached Boltz cache before T0 and proved 18
unique regular blobs/13,341,111,872 payload bytes, 19 symlinks/924 bytes, tree
SHA-256 `0d433cbb0e93382707368a166e708b50bb40d4d995b8490999d6f3258337f1a1`,
and content SHA-256
`b59a24007c2e5153259c9b0446a9071607a4d6ac6d0cf852b0aa39363009fe93`.
That setup receipt makes neither an artifact nor cache page-residency claim;
the read and all image setup remain outside T0.

The private evidence root is
`<private-evidence-root>/of2-boltz-n20-20260818T121158Z`.
The source ledger SHA-256 is
`1cb85fd9b844e00f26e684267d526eac88023ee8faf4d217411e46a5f05c68c7`;
the aggregate SHA-256 is
`e9512d5e1f61d64456c8ac1a05ebbe3365f4a82fb83244fa9faa5219210424f5`;
and the `QUALIFIED_HOMOGENEOUS_N20_SLO_FAIL` classification SHA-256 is
`29303c6709ef9bea9c14af226423456be9d673d40acb3c438d662dc533a6c954`.
Acquisition was frozen at Git commit
`e3b41aecc7b49d31914fa970aa58903feef4d5d9` (production-equivalent commit
`77e4c6d5357a89321fa4f09d16392cdc1186c0db`) with the exact 21-source
instrumentation-contract SHA-256
`a8d30d707ec273e1e9bd5fa35468cad7466e985e1a5515fcf1f593de67b18643`;
its receipt SHA-256 is
`7dc264feba7b3f234033900b9ebbcbd4d58c2a42a45814c3408ba615d20cbaec`.
The image-residency, target-preload, fresh cache full-read, prelaunch, and
pre-T0 GPU-zero receipt SHA-256 values are
`53bc14bbb2a5b3399f01a1a2b739747cba199f81f5a863315d78c5083de8fec8`,
`ee1bd7a0a8181f3df1c917272fda7c7a544c89c3312a6b2bd49c3d9371024d8b`,
`e9c4a57dc2aed5795e9bef1b233154816862d84bb8f33c7dbd947629a9e46a25`,
`3a18b9721e10d38605f42cd4b8e8176ebffa53a93e5fd204f1396546e1976692`,
and `580cd09dd98264c2e3d6012280700327af67bf2febe1c6568f34ab80ec821fcf`.
The post-cohort GPU-zero and final-state receipt SHA-256 values are
`a0b65845c45bed39efb42570140308f586a400a7e92b3b18a42d0e91c8323d1c`
and `2023272a9476535895101bc40702611c4b4b0de389190ddcdbb946351c6a9900`.
The final state retained the same node/boot, both exact holder UIDs, all seven
reviewed attached volumes, eight reviewed read-only claim users, zero run/setup
resources, zero GPU requests/limits/compute applications, and no capture agent.

## Retained response-boundary n=3 result

The earlier production-shaped direct-AIO path passed three consecutive
response-boundary trials on one provisioned H100. Median time was **18.465
seconds** for the UID/PodSpec-bound native restore and **27.342018 seconds**
from target submit through receipt of the second complete inference response.

## Qualified path

- Image: `nvcr.io/nim/mit/boltz2@sha256:0788c95c8b5b6c1a73a62c656b298ecc353a8187dc22b794f496ae40672c4c98`
- Node/GPU used for qualification: `gpu-node-b.example.invalid`, one
  H100
- Checkpoint: `boltz2-native-f7-v1`, version `1`
- M3 artifact: 16,241,056,616 bytes
- Manifest SHA-256:
  `6539b9f50a71c9f5fb6a3fbacd44f5d5ea41003539b6563682a38600d1492456`
- Runtime cache: `boltz2-nim-cache-native-f7-r3`, 13,341,112,796 bytes at
  capture
- Snapshot: direct image I/O, two CUDA PIDs, 1,908,910,080-byte rootfs diff;
  `/tmp` remains inside the captured overlay
- Restore interface image:
  `registry.example.invalid/faststart/snapshot-agent@sha256:31e1dacd18b99aec1ab7e8ec8c933f260c9dcec687938b40c44c61274f930d86`
- Strict validator SHA-256:
  `284db204afbbad91a8a40fff4a7aea41400f032b54f70ca579ae6563a7b4ad08`
- Corrected response-boundary validator for the rerun:
  `fad2b524739d699f7417fb083048431b3a87c4c2686010cc253ad8eb6057b958`

Each trial created an inert exact-digest GPU target, bound the live Pod UID,
container ID, image ID, cgroup and canonical PodSpec hash, submitted a separate
CPU semantic probe before the one-shot worker, and reached the target through a
run-scoped ClusterIP. The probe sent exactly two different 20-residue requests
with inline A3M alignments containing a real LF byte. It required HTTP 200,
distinct response bytes, exactly one mmCIF structure, exact chain and sequence,
all N/CA/C/O backbone atoms, finite coordinates/B factors, and finite confidence
and pTM scores in `[0,1]`.

| Measurement | Trial 1 | Trial 2 | Trial 3 | Median |
|---|---:|---:|---:|---:|
| Native restore | 18.868 s | 18.465 s | 17.880 s | **18.465 s** |
| T0 to successful semantic HTTP ready | 25.711837 s | 25.484587 s | 24.698911 s | **25.484587 s** |
| T0 to Kubernetes Pod Ready | 27.021134 s | 26.391013 s | 25.741550 s | **26.391013 s** |
| First inference, dispatch through complete HTTP body | 1.399536 s | 1.406047 s | 1.400760 s | **1.400760 s** |
| Second inference, dispatch through complete HTTP body | 0.273126 s | 0.288888 s | 0.278996 s | **0.278996 s** |
| T0 through second complete inference response | 27.664935 s | 27.342018 s | 26.639785 s | **27.342018 s** |

The current per-run values and evidence locations are in
`response-boundary-results.tsv`. Each counted run has a `trial-summary.json`
whose call timers end at `response_received_at`, after the complete HTTP body
but before persistence and semantic validation. Historical response-plus-
validation measurements remain unchanged in `results.tsv` and their immutable
`corrected-submit-edge-timings.json` sidecars.

## Capture baseline and experiments

The warmed donor passed two strict loopback predictions in 0.572948 seconds
total before capture. Its two response hashes were distinct and both structures
contained 20 residues, 167 atoms and 501 finite coordinates.

An isolated `writeback` manifest variant used hard links for every immutable
data file, a distinct manifest inode, and a deliberate full 16.241 GB buffered
read into the node page cache. Prewarming took 16.292456 seconds. The variant
remained functionally correct but regressed restore to 25.764 seconds and the
legacy T0-to-validation interval to 34.620570 seconds, so it is rejected. Direct-AIO v1 remains the
leader.

`b2p1-0333` is not part of the sample. Its restore succeeded in 17.617 seconds,
but the CPU probe was assigned to a preemptible node immediately before that
node became `Unknown`; the probe container never started. Subsequent probes
retained the separate-Pod/ClusterIP boundary but used required hostname
affinity to the approved, Ready t12 node.

## Scope

These are process-cold, warm-instance measurements. `T0` is recorded before
target creation with the H100 provisioned and storage attached. Successful
semantic HTTP readiness comes from the probe's strict 200/ready response;
Kubernetes Pod Ready is retained separately. Worker receipt and semantic probe
events are concurrent timelines and are not ordered against each other. The
measurements include target creation, binding, worker scheduling/restore, and
two external semantic calls. Both call clocks stop after receipt of the complete
HTTP body. They do not include H100 provisioning, the initial 33.536-second
exact-image preload, model-cache construction, or artifact creation. The writeback
prewarm cost is reported but excluded from its demand clock because it was an
explicit provisioned-state experiment.

## Offline verification

Run from this directory:

```bash
python3 -m unittest -v tests.test_boltz2_native
```

The suite covers the actual-LF A3M regression, archived request nesting,
malformed semantic results, exact image digest and canonical PodSpec binding,
post-binding drift rejection, and target/restore/probe rendering.
