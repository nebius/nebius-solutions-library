# OpenFold2 new-node benchmark harness

Status: **TWO HISTORICAL LIFECYCLES PASS; ZERO CURRENT-CONTRACT SAMPLES.**

Runs `of2-newnode-r4-0418` and `of2-newnode-r5-regional` both completed a true
new-node lifecycle, two strict semantic calls, and full cleanup. They remain
useful evidence for node scaling, cold image pulls, volume detach/attach,
restore, semantic correctness, and restoration. They are not poolable under
the current exact metric contract because their validator did not timestamp
each call's dispatch and complete-body arrival. The values formerly described
as demand-to-two-response totals are actually demand-to-validation-complete
totals, and their per-call timers include response persistence and semantic
validation.

The corrected r4 result is in [`R4_RESULT.md`](R4_RESULT.md), the regional
result and comparison are in
[`R5_REGIONAL_RESULT.md`](R5_REGIONAL_RESULT.md); mirror identity qualification
is in [`REGIONAL_MIRROR_RESULT.md`](REGIONAL_MIRROR_RESULT.md), and the
machine-readable audit is in [`CURRENT_STATUS.json`](CURRENT_STATUS.json).

The v1 runner retains a fail-closed coordination gate
(`OPENFOLD2_NEWNODE_COORDINATED=YES`) in addition to its required `--execute`
argument. **Do not execute it as the current harness.** It has the blockers
listed below and may only become live work after a reviewed v2 implementation
and a new explicit handoff.

## Current audit blockers

- Eight of the nine archived v1 shared-pipeline SHA checks are stale. The
  current full-hash diagnostics are recorded in `CURRENT_STATUS.json`; only
  `dynamo/bind_target.py` still matches. These are audit diagnostics, not
  approved pins to copy into the runner.
- The current shared evidence CLI requires a target-submit timestamp, a target
  create-return proxy, and qualification receipts; v1 supplies none. Its warm
  qualifier also requires the exact image to be present before T0, which is
  incompatible with an intentionally cold new node.
- Current shared evidence assigns `demand_at` to target submission and
  `setup_demand_at` to scale dispatch. The v1 lifecycle validator instead
  requires `demand_at` to equal scale dispatch. Aliasing those boundaries would
  make the result ambiguous.
- V1 has no current clock receipt, image/storage phase receipts, complete
  attempt ledger, n>=20 cohort aggregator, or generalized per-NIM
  configuration.

## Fixed scope

- Cluster: `mk8scluster-example`
- Project/profile: `project-example` / `${NEBIUS_PROFILE}`
- Kube context: `${OPENFOLD2_KUBE_CONTEXT}`
- Node group: `mk8snodegroup-example`
- Namespace: `nim-fast-start`
- Original node-group desired count: exactly `1`
- Artifact/cache PVCs: `mlspec-archvteams-2407-ckpt-m3` and
  `openfold2-nim-cache`
- Holder: `of2-artifact-holder-t12` on
  `gpu-node-b.example.invalid`

The runner uses exact-ID cloud reads and updates and an exact context/server
check. It never enumerates clusters. It accepts either one exact Ready group
member or one exact non-deleting retiring member with cloud counts `1/1/1/0`,
`Ready=Unknown`, and both unreachable and cloud-shutdown taints. It refuses all
other starting states or any differing holder, PVC, attachment, secret metadata,
frozen pipeline hash, or native contract.

## Archived v1 execution shape

The following command records the archived invocation shape; it is not a
current execution instruction. Do not set the coordination variable until a
reviewed v2 replacement and separate live handoff exist:

```bash
OPENFOLD2_NEWNODE_COORDINATED=YES \
  ./run_newnode_benchmark.sh of2-newnode-01 --execute
```

The historical one-shot sequence was:

1. Revalidate the single allowed cluster/group, original desired count, old
   node, holder, the two RWO attachments, pull-secret references, and frozen
   pipeline.
2. For the narrowly admitted retiring-node state only, first scale `1 -> 0`,
   prove cloud zero and removal of that exact Kubernetes Node, and retain the
   recovery evidence.
3. Delete the exact holder UID and wait for both VolumeAttachments to disappear.
4. Refresh the exact group and Kubernetes member after detach; refuse
   `NodeStatusUnknown`, deletion, or a controller-replacement overlap. Scale the
   exact node group `1 -> 0`, prove both cloud and Kubernetes zero,
   then stamp `demand_at` immediately before the exact `0 -> 1` update.
5. Admit only a new node name and UID with the exact donor-compatible H100,
   preemptible, driver, CUDA, runtime, kernel, OS, Ready, and capacity
   properties. For at most five minutes, the gate may poll only the reviewed
   `node.cilium.io/agent-not-ready:NoExecute` and
   `node.kubernetes.io/not-ready:NoExecute` startup taints; any other taint is
   terminal, and the final admission still requires no taints. Then wait for
   the node's CRIU agent.
6. Verify the exact existing seccomp ConfigMap, start a run-owned installer on
   the admitted node using digest-pinned BusyBox, prove the installed profile
   hash and live Pod UID, and retain it until the target starts. The installer
   has no image-pull-secret reference and is deleted by UID afterward.
7. Start the pinned regional NIM target with only
   `archvteams-2407-registry-pull`, observe the placeholder and both PVC
   attachments, bind its live Pod UID/spec hash, and create the CPU
   readiness-waiting probe before the one-shot native restore worker. The
   worker uses only `archvteams-2407-registry-pull`.
8. Require two unique strict semantic calls through the run-scoped ClusterIP and
   separate probe, then produce frozen validator evidence.
9. In the EXIT/INT/TERM trap, delete only run-labelled objects with server-side
   UID preconditions, prove both volumes detached, restore the original desired
   count, recreate the pinned holder if this run released it, and prove both
   volumes reattached to the holder node.

Raw evidence and the lifecycle receipt are retained under:

```text
<private-evidence-root>/openfold2-newnode-production-20260818/runs/<run-id>/
```

Successful completion requires both the frozen canary receipt and
`lifecycle-evidence.json` to be `PASS`. A benchmark failure with successful
restoration is recorded separately from a cleanup/restoration failure.

## Newnode-v2 measurement plan

1. Version the two clocks separately: `setup_demand_at` immediately before the
   exact `0 -> 1` scale request and `demand_at` immediately before target Pod
   creation. Never alias or sum independently aggregated phases.
2. Record the independent first successful application-readiness response,
   then call 1 and call 2 dispatch and complete-body arrival timestamps. Keep
   semantic-validation completion as a later, separately named boundary.
3. Add a cold-node qualification path that proves the target image was absent
   at scale T0 and binds the pulled exact digest afterward. Retain exact clock,
   image-pull, and volume detach/attach receipts.
4. Preserve every accepted and failed attempt in a fail-closed ledger. Run at
   least 20 accepted samples per scenario and report nearest-rank p50, p95,
   max, and the full failure denominator; do not pool either historical run.
5. Move node, image, storage, request, and semantic details into a generalized
   per-NIM configuration so the contract can cover all supported NIMs without
   changing metric definitions.
6. Pass offline tests and non-mutating preflight gates before requesting a
   separately authorized live cohort. No v2 performance result exists yet.

## Offline verification

```bash
cd nim-fast-start/faststart-v2/openfold2-newnode
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s ../dynamo/tests -v
PYTHONDONTWRITEBYTECODE=1 python3 -m py_compile \
  node_admission.py runtime_pipeline.py manifest_overlay.py lifecycle_evidence.py \
  starting_state.py seccomp_installer.py
bash -n run_newnode_benchmark.sh
shellcheck -x run_newnode_benchmark.sh
```

The scoped correction gate is all 22 harness tests (including the structured
audit-label assertions), JSON parsing, Python compilation, Bash syntax,
ShellCheck, and repository diff checks.
