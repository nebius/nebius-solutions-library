# OpenFold2 regional new-node benchmark result (historical r5)

Run `of2-newnode-r5-regional` completed with the frozen semantic receipt,
lifecycle receipt, and cleanup receipt all at `PASS`. The true demand edge was
the exact node-group `0 -> 1` request at
`2026-08-18T05:31:25.603010500Z`.

Evidence classification: **historical lifecycle PASS; nonpoolable under the
current exact response-boundary contract**. The run preserves valid scale,
node admission, storage, pull, restore, two-call semantic, and cleanup facts,
but it did not record either call's dispatch and complete-body arrival
timestamps. It therefore contributes zero samples to the current-contract
cohort. The structured audit is in
[`CURRENT_STATUS.json`](CURRENT_STATUS.json).

| Phase from demand | Seconds |
| --- | ---: |
| Strict new-node admission | 106.373 |
| CRIU agent Ready | 107.099 |
| Target created/scheduled | 119.397 |
| Placeholder process start | 522.397 |
| Independent first successful HTTP readiness response | **572.607133** |
| Native restore receipt | 573.481 |
| Legacy validation complete after two strict semantic calls | **575.458978** |
| Benchmark PASS recorded | 590.520 |
| Full cleanup and holder restoration | 774.171 |

The HTTP-ready value is independently recomputed from
`scale-up-demand-at.txt` to `semantic-summary.json#/ready_wait/finished_at`.
The 575.458978-second terminal value ends at
`semantic-summary.json#/finished_at`: it is validation completion, not the
arrival of call 2's complete HTTP body. The historical per-case values
(1.888578 and 0.958277 seconds) start before request dispatch but stop only
after the response was persisted and semantically validated. Exact call-1,
call-2, and scale-to-call-2-body durations are consequently `NA`.

The exact 10,698,531,042-byte regional image took **260.653 seconds** to
cold-pull. That is 9.559 seconds, or 3.81%, slower than the 251.094-second NGC
pull in r4. The byte-identical regional mirror is therefore not a cold-load
optimization on the current node/registry path.
This response-boundary correction does not change either pull measurement or
the OCI index/child-manifest identities retained in
[`REGIONAL_MIRROR_RESULT.md`](REGIONAL_MIRROR_RESULT.md).

The legacy demand-to-validation-complete total nevertheless improved from
607.247235 seconds to 575.458978 seconds: **31.788257 seconds (5.23%) lower**.
This is a historical diagnostic comparison, not a call-2-body result. The
independently recomputed HTTP-ready boundary improved from 604.270994 seconds
to 572.607133 seconds: **31.663861 seconds (5.24%) lower**. The new node admitted
12.681 seconds sooner, the settled harness avoided r4's in-clock manual seccomp
recovery, and storage-control-plane timings differed. Native restore was
effectively unchanged (4.131 seconds versus 4.083 seconds), so none of these
deltas should be attributed to the regional registry.

Both strict 20-residue folds passed through the run-scoped ClusterIP and
separate CPU probe. Their legacy request-through-persistence-and-validation
timers were 1.888578 and 0.958277 seconds and must not be relabeled as
body-arrival latencies. Cleanup restored the node group to `1/1/1/1`, removed
all run-scoped resources, recreated the holder Ready on its fixed holder node,
and confirmed both RWO volumes attached there.

Raw evidence:

`<private-evidence-root>/openfold2-newnode-production-20260818/runs/of2-newnode-r5-regional/`
