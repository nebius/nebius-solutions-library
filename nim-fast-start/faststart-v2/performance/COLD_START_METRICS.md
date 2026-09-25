# BioNeMo NIM warm-instance cold-start metrics

## Measurement contract

The comparable production clock is:

1. the GPU node is already provisioned and Ready;
2. the exact container image is already present;
3. the model and artifact volumes are already attached to that node;
4. T0 is written immediately before `kubectl create` of the inert target Pod;
5. HTTP ready is the first successful application readiness response observed
   through the run-scoped Service by an independent probe;
6. call 1 is the first strict semantic request dispatched after readiness; and
7. call 2 is the immediate second strict semantic request, using a different
   valid input.

Under the current exact response-boundary contract, each inference case's
`elapsed_seconds` starts at request dispatch and ends after the complete HTTP
response body has been read. The validator records `response_received_at` at
that boundary, before response persistence, hashing, JSON decoding, or semantic
checks. A separately named validation-completion timestamp, currently
`validation_finished_at` or `validation_completed_at` depending on the lane
schema, retains the later validation boundary.

The primary end-to-end result is computed independently for every run as T0 to
call 2's absolute `response_received_at`, then aggregated. It must not be
reconstructed by adding independently aggregated readiness and call medians,
and validator completion must never be substituted for response receipt.

For the fresh n20 cohorts, a Ready attached-storage holder supplies a pre-T0
`CLOCK_BOOTTIME` anchor bracketed by controller UTC and monotonic reads. The
holder, target, worker, and semantic probe must retain the exact same node,
boot ID, time-namespace offsets, and clock resolution. Each response-body
boundary also records `CLOCK_BOOTTIME`. The conservative per-run upper bound is
`(event_boottime - anchor_boottime) + 2 * resolution`, rounded upward to
microseconds; it deliberately includes the proven at-most-1.25-second
anchor-to-T0 gap. Observed UTC T0 durations remain separate diagnostics. The
strict p95 SLO uses the conservative upper array, never the observed array or
the non-exact API-return proxy.

Kubernetes Pod `Ready` is retained as a separate diagnostic and must never be
reported as HTTP readiness. Worker receipt completion and HTTP readiness are
independent branches: a restored server can answer HTTP before the worker has
finished writing its receipt.

For native-snapshot rows, startup and model restoration completed before HTTP
readiness are already charged to T0-to-ready. Call 1 is the first inference
latency and, of the startup-related work, includes only work deferred until the
first request, such as per-shape model loading, JIT compilation, or kernel
initialization. Call 2 is the immediately warm path. Distinct semantic inputs
prevent call 2 from being an identical-response cache hit. The selected MSA
Search route is a conventional cached start, not a native-snapshot restore, but
uses the same T0, readiness, and request boundaries.

## Evidence classes and storage states

The timing table uses four evidence classes:

- **fresh fail-closed n=20** means one homogeneous immutable-contract cohort
  with every admitted attempt retained in the denominator, explicit cleanup,
  CLOCK_BOOTTIME conservative upper bounds, and nearest-rank percentiles;
- **exact response-boundary n=3** means all five published timing fields come
  from one coherent three-run cohort, and each run retains an absolute call-2
  response timestamp;
- **production-shaped n=3; exact total pending** means HTTP readiness and both
  call timers are already complete-body measurements, but the historical run
  did not retain call 2's absolute response timestamp; and
- **manual/provisional** begins at an in-Pod restore trigger rather than target
  creation and is not comparable with the production-shaped rows.

Storage state is mandatory metadata:

- **direct** means direct/O_DIRECT artifact reads bypass the host page cache;
- **buffered, fully prewarmed** means every artifact byte was read before T0
  and is expected to be page-resident;
- **cache volume, fully prewarmed** is the corresponding state for the selected
  conventional MSA route; and
- **retained page cache** is a manual legacy experiment, not the full target-Pod
  production clock.

“Storage attached” does not imply “artifact bytes page-resident.” Full prewarm
time is excluded from T0 and must remain visible. A complete prewarm receipt
retains the byte count, content/tree identity, and elapsed full-read time;
historical receipts that lack elapsed time are identified below rather than
having a duration inferred.

## Fresh fail-closed n=20 priority cohorts

OpenFold2 and Boltz2 were rerun serially on the same already provisioned pinned
H100 node with storage attached and exact image residency proven before T0. Both
cohorts admitted exactly 20 attempts; all 40 attempts passed strict semantic
and instrumentation validation and UID-bound cleanup. The primary SLO uses the
per-run conservative CLOCK_BOOTTIME upper bound and nearest-rank p95 (rank 19
for n=20), with failed attempts sorted after successful samples.

| NIM | Cohort outcome | Qualified / cleanup | Failed denominator | T0 to HTTP ready, observed p50 / p95 / max | T0 to HTTP ready, BOOTTIME upper p50 / p95 / max | Call 1 p50 / p95 / max | Call 2 p50 / p95 / max | T0 to call-2 body, observed p50 / p95 / max | T0 to call-2 body, BOOTTIME upper p50 / p95 / max |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| OpenFold2 | **PASS** | 20/20 / 20/20 | 0/20 | 14.242080 / 14.572160 / 14.991581 | 14.342258 / 14.671991 / 15.099141 | 1.938516 / 1.973362 / 1.975756 | 1.015083 / 1.032614 / 1.035316 | 17.202273 / 17.532731 / 17.955461 | **17.302540 / 17.629887 / 18.063099** |
| Boltz2 | **SLO FAIL** | 20/20 / 20/20 | 0/20 | 26.971552 / 28.328014 / 28.996664 | 27.070530 / 28.429408 / 29.095697 | 1.408064 / 1.489046 / 1.500368 | 0.282071 / 0.300494 / 0.361623 | 28.794544 / 30.208757 / 30.923531 | **28.892235 / 30.310246 / 31.022641** |

Boltz2's SLO failure is a latency result, not an execution failure: its model,
instrumentation, cleanup, and failed-attempt denominator all remained clean.
The complete 20-element arrays and p50/nearest-rank-p95/max values for all 14
retained clocks are in `openfold2/fresh-cohort-n20-results.tsv` and
`../boltz2-native/fresh-cohort-n20-results.tsv`. These include T0-to-first-call,
Kubernetes Ready, target-create API RTT, and the diagnostic client API-return
proxy; the proxy is explicitly not exact server acceptance and is not the
primary clock.

Two qualification limitations remain explicit. Target-container GPU checks
passed, but all 40 selected qualification receipts record privileged
host-driver Xid absence as unavailable/unproven because there was no
task-scoped privileged node-log collector. In addition, the 80 raw response
bodies referenced by the 40 two-call semantic summaries were not copied from
the probe containers or retained in controller-side evidence. The summaries do
retain every response SHA-256, byte count, complete-body timestamp, and strict
semantic invariant/receipt, and the pinned validator sources remain retained
through exact per-model instrumentation contracts. These gaps do not change
the reported timings, but the retained metadata is not a substitute for either
host-driver log evidence or the raw response bodies themselves.

## Retained n=3 median matrix

All values are seconds and use `median [minimum–maximum]`. The exact-total
column is intentionally blank where an absolute call-2 response timestamp was
not retained. Kubernetes condition timestamps are diagnostic only. In the
manual row, the value shown in the HTTP-ready column is measured from the
in-Pod restore trigger, not target-create T0.

| NIM | Evidence | Selected storage | n | T0 to HTTP ready | T0 to Kubernetes Ready | Call 1: dispatch to body | Call 2: dispatch to body | Exact T0 to call-2 body |
|---|---|---|---:|---:|---:|---:|---:|---:|
| OpenFold2 | exact response-boundary n=3 | direct | 3 | 11.365660 [11.238689–11.913086] | 12.311987 [11.542021–12.904340] | 1.851894 [1.844734–1.871146] | 0.992264 [0.984416–0.996515] | **14.236758 [14.087336–14.756378]** |
| Boltz2 | exact response-boundary n=3 | direct | 3 | 25.484587 [24.698911–25.711837] | 26.391013 [25.741550–27.021134] | 1.400760 [1.399536–1.406047] | 0.278996 [0.273126–0.288888] | **27.342018 [26.639785–27.664935]** |
| ProteinMPNN | exact response-boundary n=3 | buffered, fully prewarmed | 3 | 9.460347 [9.401879–9.494261] | 9.872222 [9.784322–9.996060] | 0.589204 [0.390123–0.597313] | 0.248845 [0.244145–0.255925] | **10.249097 [10.096532–10.342388]** |
| DiffDock | exact response-boundary n=3 | buffered, fully prewarmed | 3 | 12.127239 [12.057153–12.181481] | 12.702105 [12.674250–12.858719] | 1.456961 [1.456592–1.462333] | 0.588161 [0.578353–0.599702] | **14.190621 [14.103816–14.217744]** |
| OpenFold3 | exact response-boundary n=3 | buffered, fully prewarmed | 3 | 12.271182 [12.088885–12.369170] | 12.887492 [12.651241–12.966096] | 9.098247 [9.070079–9.180301] | 9.166892 [9.112610–9.174043] | **30.564921 [30.354807–30.614101]** |
| MSA Search PDB70 | exact response-boundary conventional n=3 | cache volume, fully prewarmed | 3 | 4.872400 [4.830585–4.962104] | 4.687717 [4.545373–4.982360] | 0.040644 [0.039441–0.041808] | 0.029920 [0.028986–0.030188] | **4.942788 [4.901161–5.035089]** |
| Evo2-40B | manual/provisional H200 restore trigger | direct, legacy artifact | 3 | 65.377 [63.052–65.696] | — | 1.181 [1.163–1.213] | 0.796 [0.795–0.819] | — |
| GenMol | exact response-boundary n=3 | buffered, fully prewarmed | 3 | 10.400351 [10.217778–10.478343] | 10.319216 [10.219599–11.051287] | 1.198462 [1.186065–1.205458] | 0.575554 [0.574723–0.585800] | **12.177434 [11.981694–12.272754]** |
| RFdiffusion | exact response-boundary n=3 | buffered, fully prewarmed | 3 | 17.662044 [17.456876–17.965447] | 19.609357 [19.532522–21.124378] | 7.892573 [7.792848–7.980680] | 5.584081 [5.552619–5.726694] | **31.379359 [30.843879–31.420852]** |
| MolMIM | exact response-boundary n=3 | buffered, fully prewarmed | 3 | 10.520799 [10.446875–10.522802] | 11.735781 [11.706764–11.862442] | 2.839590 [2.812727–2.854831] | 2.099549 [2.082203–2.109474] | **15.431630 [15.414674–15.464302]** |

All nine production-shaped rows now retain exact call-2 response totals. The
older DiffDock/OpenFold3 validation-completion cohorts and Evo2/RFdiffusion
manual histories are included only as explicitly non-selected comparators:

| NIM | Retained terminal evidence | Median [minimum–maximum], seconds |
|---|---|---:|
| DiffDock | legacy T0 to validation complete | 13.657086 [13.506684–13.707841] |
| OpenFold3 | legacy T0 to validation complete | 29.345285 [29.162791–29.461653] |
| Evo2-40B | manual restore trigger through two responses | 67.390 [65.080–67.780] |
| RFdiffusion | legacy manual restore trigger through two responses; not selected | 24.593 [24.458–24.851] |

## Pre-T0 storage preparation audit

| NIM | Selected pre-T0 state and retained identity | Full-read time excluded from T0 | Receipt status |
|---|---|---:|---|
| OpenFold2 | direct/O_DIRECT M3 artifact identity-bound; payload not page-preloaded; exact target/worker/probe images proven resident | not applicable | n20 image-residency receipt SHA-256 `081a0183afe5d0be5906eab31c53d60acf953c8c06c79fa042a4024ba09a7a85`; no artifact page-cache claim |
| Boltz2 | direct/O_DIRECT 16,241,056,616-byte M3 artifact plus attached cache with 18 unique blobs/13,341,111,872 payload bytes, 19 symlinks/924 bytes, tree `0d433cbb0e93382707368a166e708b50bb40d4d995b8490999d6f3258337f1a1` | cache full read 422.854590; artifact not read | n20 cache receipt SHA-256 `e9c4a57dc2aed5795e9bef1b233154816862d84bb8f33c7dbd947629a9e46a25`; identity/full-read proof makes no artifact or cache page-residency claim |
| ProteinMPNN | 1,867,046,505 bytes, 57 files, aggregate content SHA-256 `b2ce82dfbef1cbeb9c3ac35b94f5a2f97fccc19a98419e213d8c0d42a5c2c0e0` | 3.586695 in-holder reader; 4.721513 outer `kubectl exec` wall interval | complete identity/elapsed receipt; outer command interval `2026-08-18T11:42:32.791854148Z`–`2026-08-18T11:42:37.513367125Z`; receipt SHA-256 `f611a9457b7991a63cbbac40849398ebcd826b86186d7ddfc3742199ac210ee5` |
| DiffDock | 7,516,058,314 bytes, 122 files, tree SHA-256 `2d9e339392d6b4c5207ddbd4ef8f26465e324b2e165bd4cd9b43530f006e1b1d` | 5.931160 | complete byte/tree/timestamp/elapsed receipt SHA-256 `aeb1af149e0d054af810d1f670fb339342aa4066b9fbd01d8bd2d0f2058be7e8` |
| OpenFold3 | 9,263,246,107 bytes, 148 files, tree SHA-256 `f488019348551f356a153ce17cd9568a9d59497ead375c81a84ddef3bc3972c2` | 7.386615 in-holder reader; 8.583788 outer `kubectl exec` wall interval | complete identity/timestamp/elapsed receipt SHA-256 `4e2ce483ed27d817f8e00fc26ef7f53fb9ad2b35f094b59ca44f97fb56abc7e9`; outer interval 12:35:15.138304811Z–12:35:23.722092736Z; holder UID retained |
| MSA Search PDB70 | 112,682,799 bytes across 13 unique inodes, content-stream SHA-256 `416efa6571423414a0fb46e8739bfa1202b5885122ce2e6cb280a00607bd4062` | 0.104987 | complete identity/timestamp/elapsed receipt SHA-256 `6aea481f44cd7d4ca05505c6bfd427a4353563ba2a3fb0c5c1fd09a92a98b98e` |
| Evo2-40B | legacy direct 99,959,572,798-byte checkpoint | not applicable | no current manifest-bound artifact; manual evidence only |
| GenMol | 4,781,347,930 bytes, 114 files, tree SHA-256 `8d847217744b84f2ddce4520bfaf83dec0285241fade9d9fb91b5b83d8c18198` | 6.328907 | complete identity and elapsed receipt; the exact cohort reused the continuously Ready holder rather than performing a fresh immediately-before-cohort read, so page residency is expected rather than freshly reproven |
| RFdiffusion | artifact: 22,087,352,229 bytes/90 files/manifest `5d47f0fac7bba60bdab3e29843f2fd99150491e917f7f3758a84176aef8c7f9d`/aggregate SHA-256 `8f3b3f66b2b8e886b2b04880d6e511ee138b409bf55471849dfd9657a6df44fb`; cache: 2,590,162,178 bytes/674 files/tree `8b79aa4f4ca6a3121ca6d3d7e8083addd949a28a84b375bd5754580415eb80fd` | artifact 16.332096; cache 32.633541; total 48.965637 | complete refreshed holder receipt SHA-256 `17afc7961933a10cd7b1ab6d0d391a54f459bf1f5db67bbb51be61cae5d0920d` |
| MolMIM | artifact: 5,220,755,473 bytes/81 files/tree `19c9d2eafb62887aa6dd1e71c0bcd4b4ea73522da5235ea19c4812d9a5c5ac20`; cache: 284,497,920 bytes/2 files/tree `5ff815495b2b90ec6f4d9e5df24216b11a60d49f711e68999347036b0f43056c` | artifact 4.194605; cache 17.524894 | both bytes/tree/elapsed receipts complete; aggregate retains pre-T0 capture times |

The selected OpenFold3 response-boundary aggregate is
`<private-evidence-root>/openfold3-response-boundary/aggregate.json`,
SHA-256
`a8c8469759452aaf709aeeb5200e5b773337bae85788a94b7384e8f862d244f3`.
Its full-read receipt is in the same directory; the final-state receipt SHA-256
is `914ef402442db45e10419a5958ceb57eaa49645d4fe96d1764d1f8ea5037fffe`.
The authoritative MSA receipt is the unique-inode version at
`<private-evidence-root>/msa-search-response-requal-20260818T111418Z/cache-holder-receipt.json`;
the older root-level cache receipt double-counted a symlink and its target and
must not be used. Its selected response-boundary aggregate is
`<private-evidence-root>/msa-search-response-requal-20260818T111418Z/aggregate.json`,
SHA-256
`8b2e6a126d49ce49ed333d6e8b446d873856f66e9b9c3bf89e3b15eb94bbdb75`.
ProteinMPNN's selected response-boundary aggregate is
`<private-evidence-root>/proteinmpnn-native-f7-response-20260818T114151Z/aggregate.json`,
SHA-256
`a19a7b8c618b771623c2f6df45267d125961d28a05b0e87eb8a40023ea5f88df`.
DiffDock's selected aggregate is
`<private-evidence-root>/diffdock-native-f7-response-20260818T1209Z/aggregate.json`,
SHA-256
`1e582f6c571e5d9af36e362b2f75df43fef035b7a7265780a5052e2531e88f24`.
RFdiffusion's selected aggregate is
`<private-evidence-root>/rfdiffusion-native-f7-20260818T080831Z/aggregates/rfd-f7-warm-buffered-n3.json`,
SHA-256
`5e27493276dfd1eda3eb640c1bfe4655e378060ceba8a77619abb3271f27f0b6`;
its authoritative refreshed holder receipt is
`<private-evidence-root>/rfdiffusion-native-f7-20260818T080831Z/setup/buffered-holder-r7-refresh-receipt.json`.
The earlier `rfd-f7-buf-{1,2,3}` diagnostic cohort is explicitly excluded
because direct-canary activity followed its stale holder read; it never
contributes to the selected median. The retained n=3 OpenFold2 and Boltz2
exact-image preloads took 264.996 and 33.536 seconds respectively. The fresh
n20 cohorts instead bind the exact image-residency receipts named above and in
the model READMEs. Every preload occurred before T0 and is image-residency
setup rather than artifact full-read time.

MolMIM's selected call timers are monotonic dispatch-to-complete-body
measurements and its exact total is independently derived from call 2's
absolute `response_received_at`. Its retained per-case schema omits an absolute
`request_started_at`; that is a receipt-schema gap, not a timing-arithmetic
substitution. The later validation-completion timestamp remains separate.

## Storage sensitivity already demonstrated

- OpenFold3 direct I/O took 87.284431 seconds to HTTP readiness in its one
  production-shaped canary, versus the selected 12.271182-second fully
  prewarmed buffered median. The selected exact T0-to-second-response median
  is 30.564921 seconds.
- ProteinMPNN direct n=3 took 23.763 seconds to HTTP readiness, versus the
  selected 9.460347-second buffered median; the selected path excludes the
  measured 3.586695-second artifact read. Its exact selected total through the
  second complete response is 10.249097 seconds.
- DiffDock direct took 72.594545 seconds to HTTP readiness in its canary,
  versus the selected exact-cohort 12.127239-second buffered median; the exact
  selected T0-to-second-response median is 14.190621 seconds.
- GenMol direct n=3 took 48.738868 seconds to HTTP readiness, versus the
  selected 10.400351-second buffered median. The selected exact
  response-boundary total is 12.177434 seconds; an exact direct total is not
  available from the retained direct cohort.
- RFdiffusion's exact direct canary took 199.036267 seconds to HTTP readiness,
  8.323738 and 5.639307 seconds for the two calls, and 213.009981 seconds
  through the second complete response. The selected refreshed, fully
  prewarmed buffered n=3 median is 17.662044 seconds to readiness and
  31.379359 seconds through the second response. Its 48.965637-second artifact
  plus cache full read occurred before T0.

These deltas are why storage state is part of the result rather than an
implementation footnote.

## Remaining measurement work

Nine of the ten NIMs have production-shaped n=3 HTTP-ready, two-call, and exact
T0-to-call-2 response-boundary evidence: OpenFold2, Boltz2, ProteinMPNN,
DiffDock, OpenFold3, MSA Search, GenMol, RFdiffusion, and MolMIM.

OpenFold2 and Boltz2 additionally have the fresh fail-closed n=20 evidence
reported above. OpenFold2 meets the strict `<30 s` conservative-upper p95
target; Boltz2 does not, despite 20/20 valid runs and cleanups.

Evo2-40B is the only remaining non-production-shaped row. It is blocked on an
explicit owner decision to release the only allowed H200 from the healthy
owner-managed Deployment; the current Pod topology is not a task-scoped
capture donor and was not modified. Its retained manual direct result is not
promoted into the production-shaped comparison.

Primary evidence lives in the model lanes and the private run directories
named by their checked-in compact receipts. Failed setup attempts are excluded
and retained separately; they never contribute to a median.
