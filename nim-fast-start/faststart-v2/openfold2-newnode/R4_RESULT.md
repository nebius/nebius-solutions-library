# OpenFold2 new-node benchmark result (historical r4)

Run `of2-newnode-r4-0418` completed with both the frozen canary receipt and the
lifecycle receipt at `PASS`. The true demand edge was the exact node-group
`0 -> 1` request at `2026-08-18T04:21:09.698504003Z`.

Evidence classification: **historical lifecycle PASS; nonpoolable under the
current exact response-boundary contract**. The run proves the scale, node
admission, storage, pull, restore, two-call semantic, and cleanup sequence, but
it did not record either call's dispatch and complete-body arrival timestamps.
It therefore contributes zero samples to the current-contract cohort. The
structured audit is in [`CURRENT_STATUS.json`](CURRENT_STATUS.json).

| Phase from demand | Seconds |
| --- | ---: |
| Strict new-node admission | 119.055 |
| CRIU agent Ready | 119.765 |
| Target created/scheduled | 125.301 |
| Placeholder process start | 586.301 |
| Independent first successful HTTP readiness response | **604.270994** |
| Native restore receipt | 605.152 |
| Legacy validation complete after two strict semantic calls | **607.247235** |
| Benchmark PASS recorded | 621.948 |
| Full cleanup and holder restoration | 810.310 |

The HTTP-ready value is independently recomputed from
`scale-up-demand-at.txt` to `semantic-summary.json#/ready_wait/finished_at`.
The 607.247235-second terminal value ends at
`semantic-summary.json#/finished_at`: it is validation completion, not the
arrival of call 2's complete HTTP body. The historical per-case values
(1.916596 and 1.055248 seconds) start before request dispatch but stop only
after the response was persisted and semantically validated. Exact call-1,
call-2, and scale-to-call-2-body durations are consequently `NA`.

The exact 10,698,531,042-byte OpenFold2 image took 251.094 seconds to cold-pull
from the original NGC registry. That registry transfer is the dominant
avoidable portion of the current result. A byte-identical regional mirror is
qualified in [`REGIONAL_MIRROR_RESULT.md`](REGIONAL_MIRROR_RESULT.md) and was
subsequently cold-pulled in the historical r5 lifecycle.
The native restore itself took 4.083 seconds. Both semantic calls passed; their
legacy request-through-persistence-and-validation timers were 1.916596 and
1.055248 seconds and must not be relabeled as body-arrival latencies.

The fresh node initially lacked `profiles/block-iouring.json`. A run-owned
installer copied the exact existing ConfigMap payload (SHA-256
`ebbe5e221b6b331bb84efbdfea7adb88e9dddab62a2ea901598bad09fe7f76a0`), and
the target recovered on the same demand clock. The settled runner now performs
that preparation automatically with a digest-pinned BusyBox image and deletes
the installer by its server-returned UID after target start.

Cleanup restored node-group counts to `1/1/1/1`, removed all run-labelled
resources, recreated the holder Ready on its fixed node, and confirmed both RWO
volumes attached there.

Raw evidence:

`<private-evidence-root>/openfold2-newnode-production-20260818/runs/of2-newnode-r4-0418/`
