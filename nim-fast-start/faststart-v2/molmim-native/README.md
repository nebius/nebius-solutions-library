# MolMIM production-shaped startup qualification

Status: live performance qualification complete on 2026-08-18. The true
buffered native path is the selected fast-start route: its strict
T0-to-second-response median is 15.431630 seconds versus 24.147146 seconds for
an exact-image cached conventional start, an 8.715516-second (36.0934%) win.

## Frozen identity

- Image: `nvcr.io/nim/nvidia/molmim@sha256:7700c5556935a93055bee5367d36acb6d3e55d22fd1ba28503f5447656fa63fa`
  (retained size: 13,998,727,508 bytes).
- Qualification node: `gpu-node-b.example.invalid`, one NVIDIA H100.
- Retained cache source on hf93: `/snapshots/nim-caches/molmim`, 284,497,920
  bytes, including exactly one 281,589,760-byte MolMIM v1.3 checkpoint and
  the 2,908,160-byte H100 JIT archive. Because hf93 had no remaining volume
  attachment, the exact tree was streamed through private local evidence to a
  t12-attached PVC and verified at both boundaries.
- Production cache claim: `molmim-native-f7-cache`.
- Direct checkpoint: `molmim-native-f7-v1`, artifact version `1`.
- True legacy-buffered checkpoint: `molmim-native-f7-v2-buffered`, artifact
  version `1` with a distinct manifest selecting `imageIoMode: buffered`.
- Endpoint: `/generate`.
- Fixture: `fixtures/request-cmaes-qed.json`, 696 bytes, SHA-256
  `053e8a5befb020695e4d27200d21b296e7171f480075125cfa6f7b5a71dbc42d`.
- Validator SHA-256:
  `0d87fd53b554a629b8fb83c5abc79b074220f223ea97f7c1d8802d48e4833bd7`.

The retained tree has SHA-256
`5ff815495b2b90ec6f4d9e5df24216b11a60d49f711e68999347036b0f43056c`.
`cache-holder.yaml` independently rehashes and fully reads it before any T0;
its receipt records tree identity, 284,497,920 unique bytes, and full-read
elapsed time. The exact MolMIM container requires a writable cache and its NGC
bootstrap secret even when the payload is already present, so the prime,
donor, and conventional target use that runtime contract. The holder mounts
the resulting cache read-only and proves the pre-measurement state.

## Strict semantic contract

Every counted trial submits one separate CPU-only probe before waiting for the
GPU target to become Ready. The probe reaches only the run-scoped ClusterIP and
makes exactly two real POSTs:

1. caffeine seed, CMA-ES, one iteration, two particles, one molecule, QED;
2. aspirin seed, with the same optimization settings and a different input.

A PASS requires HTTP 200 JSON, the two exact canonical request hashes, one
unique generated molecule per response, RDKit-parsable SMILES, a finite QED in
`[0,1]` that agrees with independent RDKit calculation within 0.02, distinct
response hashes, and different generated molecules. Proxies are disabled and
redirects are rejected. The probe uses the exact MolMIM image as a CPU-only
oracle because that immutable image contains `/usr/bin/python3` and RDKit; a
local `--network none`, non-root, read-only-rootfs check passed during offline
preparation.

## Retained performance evidence and decision

The counted runs used one warm provisioned H100 node, the exact image already
present, and attached storage fully read before T0. T0 is the timestamp on the
line immediately before target Pod creation. Application HTTP readiness and
Kubernetes Ready are independent measurements. Call latency ends immediately
after the HTTP body is received; response validation finishes a few
milliseconds later and is retained separately. All prewarm work is outside T0.

| Path / metric | Median seconds | Range seconds |
|---|---:|---:|
| Conventional cached HTTP Ready | 18.912118 | 18.511612–18.928866 |
| Conventional cached Kubernetes Ready | 18.935902 | 18.831836–19.121639 |
| Conventional call 1 | 3.100931 | 3.057201–3.109182 |
| Conventional call 2 | 2.144578 | 2.139924–2.183543 |
| Conventional T0 to call-2 response | 24.147146 | 23.764785–24.214850 |
| Conventional T0 to validation complete | 24.149750 | 23.767309–24.217246 |
| Buffered native HTTP Ready | 10.520799 | 10.446875–10.522802 |
| Buffered native Kubernetes Ready | 11.735781 | 11.706764–11.862442 |
| Buffered native call 1 | 2.839590 | 2.812727–2.854831 |
| Buffered native call 2 | 2.099549 | 2.082203–2.109474 |
| Buffered native T0 to call-2 response | 15.431630 | 15.414674–15.464302 |
| Buffered native T0 to validation complete | 15.434133 | 15.416983–15.466200 |
| Buffered native worker restore | 3.229000 | 3.228000–3.329000 |

The pre-T0 cache holder fully read 284,497,920 unique bytes in 17.524894
seconds. The selected buffered holder fully read 5,220,755,473 bytes in
4.194605 seconds, with tree SHA-256
`19c9d2eafb62887aa6dd1e71c0bcd4b4ea73522da5235ea19c4812d9a5c5ac20`
and manifest SHA-256
`3305ed17be7b332dd46b084155aadcc59e281e06240e6a62477d165b6ec644a0`.

The required direct-mode canary also passed: HTTP Ready 52.675188 seconds,
Kubernetes Ready 53.899638 seconds, call 1 2.893734 seconds, call 2 2.094551
seconds, and T0 to the second response 57.667891 seconds. Direct mode is not the
selected route.

The retained raw evidence root is
`<private-evidence-root>/molmim-native-f7-20260818T073602Z`.
The conventional and buffered summaries have SHA-256
`dfdc7eb43b83766825972690805808f1bcf18519ca0024812b042381512cfa86` and
`97dd239b440b2f0c2d22ac20e925192d653fd9f38808cb85cade9870393e9cdd`.
Every counted n=3 trial has a separate cleanup receipt. The comparison result
is `PROMOTE_BUFFERED_NATIVE_FOR_FURTHER_QUALIFICATION`. The final retained
cluster-state receipt has SHA-256
`9324a5706e000b983f19d1de410dcde15099ca3827ef6f957ab8880c166fc804`.

## Prepared paths

`conventional/` provides the control:

- `image-cache-holder.yaml` pins and primes the exact 14 GB image without a
  GPU request and proves the CPU RDKit oracle exists;
- `render.py` creates a scheduler-bound real NIM target, private ClusterIP,
  network policies, immutable validator ConfigMap, and early CPU probe;
- `run_cached_trial.sh` requires the exact image and fully prewarmed cache
  holders, records target Events proving the image was already present, and
  produces one strict demand-to-two-call receipt;
- `run_cached_n3.sh` and `aggregate.py` require three independent PASS trials
  and six semantic responses.

The native path provides UID-bound capture, a scheduler-created inert target,
canonical PodSpec/cgroup/container/image binding, direct and true buffered
artifacts, separate early CPU probing through ClusterIP, n=3 aggregation, and
run-scoped cleanup. The native runner also refuses to start unless the exact
image holder and fully prewarmed cache holder are Ready on the H100.
Native evidence keeps Kubernetes Pod Ready separate from the validator's
direct HTTP `ready_at`; n=3 reports direct demand-to-ready, each real request,
and the independently timestamped demand-to-second-response total.

`conventional/compare.py` compares the conventional n=3 median with the exact
buffered native n=3 median. It exits `3` and emits
`REJECT_NATIVE_RESTORE_KEEP_CONVENTIONAL_CACHED` when buffered native is equal
or slower. This is intentional fail-fast behavior: do not spend more GPU time
on MolMIM restore after that result.

## Worker contract gate

`dynamo/restore-interface.live.json` pins the current GLIBC-compatible generic
performance worker and its exact eight-patch source provenance:

```text
registry.example.invalid/faststart/snapshot-agent@sha256:d5ce1eaad55378a93a9bf53b35effcbc378ed15ab7e5b7f6b41df6689cefdf28
```

- restore-worker SHA-256:
  `941157dd1815acf6f3e26cbe9dea65ee1c9a398c719881d474e5d7c5c7e28651`;
- 34-file tool-manifest SHA-256:
  `c0d638100c03fa35973e82859d15b9c8dd1bcbf0fe9cb185b58cc21fae7ead1e`;
- materialized source-tree SHA-256:
  `76838bc28fa641ba3d3165c1deb1f019c4f63ed9fce9571b38194ff65ef7b816`,
  including `ns-bind-mount-glibc35` patch SHA-256
  `4847d7d42aae570fc7f91351a8fbf3018f10dc6247d93c2c9696754861731366`;
- maximum required glibc: 2.35;
- direct, writeback, and true legacy-buffered I/O supported.

The build is classified `performance-validation-only`. The exact Jammy CUDA
base still lacks the required baseline SBOM for a full `agent` compliance
release, so `release_ready` is deliberately `false`. Native render/run entry
points stop by default; the live measurements used the explicit
`--allow-performance-validation-worker` acknowledgement. The timing result is
therefore a performance qualification, not a full compliance-release claim.

## Verification

Run from this directory:

```console
python3 -m unittest discover -s tests -p 'test_*.py' -v
python3 -m unittest discover -s dynamo/tests -p 'test_*.py' -v
python3 -m unittest discover -s conventional/tests -p 'test_*.py' -v
bash dynamo/tests/test_run_provisioned_trial.sh
bash dynamo/tests/test_run_n3.sh
python3 -m py_compile *.py dynamo/*.py conventional/*.py
bash -n dynamo/*.sh dynamo/tests/*.sh conventional/*.sh
```

The executed live order and reproducible commands are in `EXECUTION_PLAN.md`.
