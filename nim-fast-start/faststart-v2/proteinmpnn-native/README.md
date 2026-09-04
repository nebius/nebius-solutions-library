# ProteinMPNN native Dynamo fast start

This lane restores the production ProteinMPNN NIM from a native f7 checkpoint,
then proves application readiness with exactly two real 1UBQ inference calls.
The measured implementation is restricted to
`mk8scluster-example` and H100 node
`gpu-node-a.example.invalid`.

## Winning candidate

- NIM image: `nvcr.io/nim/ipd/proteinmpnn@sha256:b55a0aa6733e267e6e6fe06434e98aea61eff14bc5545127555607fef6f38aa5`
- checkpoint: `proteinmpnn-native-f7-v3-buffered`, version `1`
- artifact manifest: `6a298ceefc93b259e5ec7e6c1e74ae3ab43cdd9a757bee1934923dbfcdc06c07`
- artifact inventory: 57 regular files, 1,867,046,505 bytes
- restore worker: `registry.example.invalid/faststart/snapshot-agent@sha256:063286a3a1354d1c5969fa80f445bb5fbd2a96bc0999c7b6897495f0b4c2fd4d`
- injected tool manifest: `fc22c423deca17b4175ab42c23a66310c8e2c4d8c4b63a24c33894300020943b`
- restore interface: `dynamo/restore-interface.live.json`, SHA-256 `f93406d1f54169eb54f733498526b0b1eb4e3c79b225542ac6db84768e2814b4`

The artifact differs from retained direct-I/O v1 only in its checkpoint ID and
`criu.imageIoMode: buffered`; all 56 payload files are hard links, including
the required 389,120-byte rootfs delta. `prewarm_buffered_artifact.py` reads
and inventories every regular file before measurement.

## Results

The selected fully prewarmed buffered route passed a fresh coherent exact
response-boundary n=3. Values are `median [minimum–maximum]` seconds.

| metric | selected n=3 |
| --- | ---: |
| T0 to independent HTTP ready | **9.460347 [9.401879–9.494261]** |
| T0 to Kubernetes Ready (diagnostic) | 9.872222 [9.784322–9.996060] |
| call 1, dispatch to complete body | **0.589204 [0.390123–0.597313]** |
| call 2, dispatch to complete body | **0.248845 [0.244145–0.255925]** |
| T0 to call 2 `response_received_at` | **10.249097 [10.096532–10.342388]** |
| worker restore | 1.344 [1.310–1.346] |

HTTP ready is the validator's first successful semantic readiness response,
not the Kubernetes condition. `T0` is captured before target creation on the
already provisioned H100 with storage attached. Kubernetes Ready is diagnostic;
call 1 is the first inference and therefore includes any model work deferred
until first use, and call 2 is the immediate warm inference. The exact total is
computed per trial from the absolute call-2 response timestamp, never by adding
independently aggregated medians or using validation completion.

The selected route's 9.460347-second readiness median is 2.512x faster than
the retained 23.763006-second direct median. Its 1.344-second restore median is
11.702x faster than the retained 15.727-second direct median. Those historical
direct and buffered cohorts remain non-selected comparators because they lack
absolute call-2 response timestamps; `results.tsv` labels their terminal total
as validation completion.

The strict calls used seeds 2370 and 2371. Their response hashes are:

- `7fdaec16e144acc4e9547348a4ebf898c3d9c2838086c8f67256b6f0319a392e`
- `e418b42ce8e13c34e65b97a83b7196895f7b2ac84274b6ef4b53966f54bfb4f2`

Immediately before the selected cohort, the unchanged holder read all 57 files
and 1,867,046,505 bytes in 3.586695 seconds. The read began at
`2026-08-18T11:42:32.791854148Z` and completed at
`2026-08-18T11:42:37.513367125Z`; its tree SHA-256 is
`b2ce82dfbef1cbeb9c3ac35b94f5a2f97fccc19a98419e213d8c0d42a5c2c0e0`.
No artifact refresh occurred between counted trials. The exact target, worker,
and probe images were proven resident outside T0, and no counted target emitted
an image-pull event.

Raw evidence is retained outside Git under
`<private-evidence-root>/proteinmpnn-native-f7-response-20260818T114151Z`.
The selected `aggregate.json` SHA-256 is
`a19a7b8c618b771623c2f6df45267d125961d28a05b0e87eb8a40023ea5f88df`;
the full-read and image-residency receipt SHA-256 values are respectively
`f611a9457b7991a63cbbac40849398ebcd826b86186d7ddfc3742199ac210ee5`
and `885ca3ed7f042575d32b7eac06dffc1f956dd74a1cbd68ce8e37ab1497a95b4c`.
Each trial used run-ID-scoped exact-manifest cleanup with retained
absence/GPU-zero receipt. The final state has no counted or excluded run
resources, zero active GPU requests, the same Ready holder, both PVCs Bound,
and all unrelated holders preserved; its receipt SHA-256 is
`7823e7c864eb8b62e14abd871dd3ed91dd3087a01ab0fd8e1308b99480cb6c0a`.

One setup attempt, `pmp-rb-1131-r1`, is excluded because the original 1000m
worker request exceeded the exact scheduling envelope by 30m. The selected
worker requests 500m with the same 4-CPU limit, leaving 470m scheduled
headroom at the audited 4,830m baseline; the linter and tests pin both values.
The first online r1 derivation also encountered Kubernetes' whole-second probe
finish timestamp 0.254489 seconds before precise validator completion. The
immutable raw inputs were rederived with a bounded rule that accepts only a
sub-second quantization inversion and rejects one second or more; recovery
receipt SHA-256
`818e6f30d211141ee4ef51241d51cc2cfac3a2c445169b98aed3ca966a836b80`.

The exact rows are checked in as `response-boundary-results.tsv` and
`results.json`. Historical raw evidence remains under
`<private-evidence-root>/proteinmpnn-native-f7-20260818T0336Z`.

## Verification

```console
python3 -m unittest discover -s dynamo/tests -v
bash dynamo/tests/test_run_provisioned_trial.sh
bash -n dynamo/run_provisioned_trial.sh
patch --dry-run -p1 -d /path/to/dynamo < ../phase2-agent/buffered-criu-io.patch
```

The full Go suite passed in the pinned Go builder. The generic buffered patch
keeps empty/direct and writeback behavior unchanged and maps only the explicit
`buffered` value to an absent CRIU protobuf field, which selects CRIU's legacy
buffered I/O path.
