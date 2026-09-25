# RFdiffusion native H100 fast-start lane

This lane now has a production-shaped live result for RFdiffusion on a warm
single-H100 instance with its image, cache, and checkpoint storage attached.
The selected route is a native CRIU checkpoint with buffered image I/O and an
explicit full read of both the NIM cache and checkpoint outside the measured
window.

## Result

The selected `rfd-f7-warm` cohort passed 3/3 fresh target restores and 6/6
strict RF backbone generations. Times are seconds; ranges are minimum to
maximum.

| Metric | Median | Range |
|---|---:|---:|
| T0 to semantic HTTP Ready | 17.662044 | 17.456876–17.965447 |
| T0 to Kubernetes Pod Ready | 19.609357 | 19.532522–21.124378 |
| first inference response body | 7.892573 | 7.792848–7.980680 |
| second inference response body | 5.584081 | 5.552619–5.726694 |
| T0 through second inference body | 31.379359 | 30.843879–31.420852 |
| T0 through semantic validation | 31.383563 | 30.849501–31.430252 |
| validation after the second body | 0.005622 | 0.004204–0.009400 |
| native restore worker | 11.521 | 11.487–11.554 |

Trial-order arrays and evidence paths are in `results.json`. The authoritative
aggregate is
`<private-evidence-root>/rfdiffusion-native-f7-20260818T080831Z/aggregates/rfd-f7-warm-buffered-n3.json`,
SHA-256
`5e27493276dfd1eda3eb640c1bfe4655e378060ceba8a77619abb3271f27f0b6`.

A direct-I/O compatibility canary also passed two strict calls. It took
199.036267 seconds to semantic HTTP Ready, 8.323738 seconds for call 1,
5.639307 seconds for call 2, and 213.009981 seconds from T0 through the second
response body. Its restore worker took 193.032 seconds. It is a canary, not a
direct n=3, and is not mixed into the selected buffered cohort.

## Metric boundary

`T0` is the timestamp persisted on the line immediately before `kubectl
create` submits the inert target Pod. The H100 instance is already running,
the exact image is resident, both PVCs are attached, and the selected storage
holder is Ready before T0. Image pull, capture, artifact construction, PVC
attachment, and full-read prewarming are setup measurements outside T0.

HTTP Ready means the first successful semantic application response from
`GET /v1/health/ready`. Kubernetes Pod Ready is retained as a separate
diagnostic timestamp. Each call latency starts immediately before request
dispatch and ends immediately after the complete HTTP response body arrives;
semantic validation completion is recorded separately. T0 through call 2
therefore includes target creation, native restore, readiness, and exactly two
inferences, but excludes validation performed after the second body arrived.

The selected pre-T0 refresh read and hashed 674 cache files totaling
2,590,162,178 bytes in 32.633541 seconds and 90 buffered checkpoint files
totaling 22,087,352,229 bytes in 16.332096 seconds. Total full-read work was
48.965637 seconds; Pod create to Ready was 50.644535 seconds. Its receipt is
`setup/buffered-holder-r7-refresh-receipt.json` in the evidence root, SHA-256
`17afc7961933a10cd7b1ab6d0d391a54f459bf1f5db67bbb51be61cae5d0920d`.

## Exact workload

- image: `nvcr.io/nim/ipd/rfdiffusion@sha256:15e40e466d8ebe9a53f1feea599373720428c9de65da750bf4271c96ec35ceb4`
- node: `gpu-node-c.example.invalid`
- hardware: one full, non-MIG NVIDIA H100, compute capability 9.0
- target resources: 12 CPU, 128 GiB memory, one GPU, Guaranteed QoS, and a
  64 GiB `/dev/shm`
- cache and `NIM_CACHE_PATH`: `/home/user/.cache/nim`
- inference endpoint: `POST /biology/ipd/rfdiffusion/generate`
- fixture: `fixtures/1UBQ.pdb`, SHA-256
  `d4a6812d8951cf6594e6a0763f089e35f5a80b62acb3c117b2c5565228a7b161`
- request: contigs `A20-60/0 20-30`, 15 diffusion steps, seeds 2370 and 2371
- request SHA-256 values:
  `da696caf8aba3511e63df5a293622e91b4c063f1593c60038bedca16d4865b2d`
  and
  `8fa20730e48a66c62fc5d095b4d26afac00cf7c4768e59300b95e447bc200c3c`

The validator requires exactly two distinct 200 responses and checks the
generated PDB backbone, complete N/CA/C atoms, residue bounds, coordinates,
adjacent CA geometry, fixed request hashes, seeds, and response distinctness.
There are no inference retries.

## Native capture and artifacts

The final capture is `f7-r7` / `PodSnapshotContent` `rfd-f7-r7`, sourced from
sanitized Pod-UID fixture `00000000-0000-4000-8000-000000000002`. Its validator is materialized
into a bounded `1Mi` `/validator` `emptyDir`, so capture and restore reproduce
the same d5ce-compatible emptyDir/PVC topology. The donor passed two strict
calls in 5.678462 and 5.565097 seconds before capture. Capture wall time was
270.512581880 seconds, including 264.621051939 seconds in CRIU.

The immutable artifacts are:

- direct `rfdiffusion-native-h100-v2`, version 1: 90 files,
  22,087,352,218 bytes, manifest SHA-256
  `21c83eaa10facc54f9483f5f47528a19cacb6d568bd46224ecfe013af5f68608`;
- buffered `rfdiffusion-native-h100-v2-buffered`, version 1: 90 files,
  22,087,352,229 bytes, manifest SHA-256
  `5d47f0fac7bba60bdab3e29843f2fd99150491e917f7f3758a84176aef8c7f9d`;
  and
- pinned NIM cache: 674 files, 2,590,162,178 bytes, three safe internal
  symlinks, tree SHA-256
  `8b79aa4f4ca6a3121ca6d3d7e8083addd949a28a84b375bd5754580415eb80fd`.

The buffered variant changes only checkpoint identity and `imageIoMode`; all
89 payload files, totaling 22,087,346,372 bytes, are hard-linked to the direct
artifact. It is a true buffered CRIU artifact, not a separate read-ahead
process around direct I/O.

## Cohort integrity

The first exploratory buffered sequence is retained but excluded. Its first
restore took 175.616 seconds and its second took 11.999 seconds, proving that
the earlier holder receipt no longer described live page-cache residency after
the direct canary. The selected holder was recreated and performed a fresh
full read after all direct activity; only the three subsequent `rfd-f7-warm`
trials share that byte-identical receipt and storage state.

Setup-only cache population failures, the provider reboot, and five presemantic
direct topology/identity canaries are also preserved as exclusions. None is
included in the aggregate. The provider reboot happened before a benchmark T0
and produced sanitized boot-ID fixture
`00000000-0000-4000-8000-000000000001`.

## Runner behavior

`run_one_provisioned_trial.sh` rejects a mismatched API server, node/GPU
topology, active GPU request, image holder, live imageID, artifact holder,
manifest, cache tree, runtime topology, worker, validator, or fixture before
creating a run directory and target. It saves the exact image-holder receipt,
storage receipt, target-submit timestamp, API-defaulted Pod, UID/container/IP
binding, worker receipt, semantic receipt, EndpointSlice, and final timestamps.

`aggregate_results.py` accepts exactly three passing fresh UIDs with one
immutable storage state. It recomputes T0-to-HTTP, T0-to-Kubernetes Ready,
T0-to-call-2, T0-to-validation, call body timings, and validation overhang from
absolute timestamps, and requires exact `target-submit-at.txt` provenance.

After the run, all run-scoped GPU targets, workers, probes, Services, RBAC, and
policies were removed. The selected image/direct/buffered CPU holders, both
PVCs, and captured `PodSnapshotContent` evidence remain. Active run GPU
requests are zero. The post-cleanup read-only receipt is
`<private-evidence-root>/rfdiffusion-native-f7-20260818T080831Z/final-cluster-state.json`,
SHA-256
`153db897876cbb7a748758db8e5fe74418cddeb3eef478f192f2bab2c2f5e77c`.

## Verification

```console
python3 -m unittest discover -s tests -v
python3 -m py_compile *.py
bash -n run_one_provisioned_trial.sh run_provisioned_n3.sh
shellcheck -x run_one_provisioned_trial.sh run_provisioned_n3.sh
```
