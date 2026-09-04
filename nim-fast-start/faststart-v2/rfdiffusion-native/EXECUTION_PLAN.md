# RFdiffusion native execution record

The production-shaped run completed on 2026-08-18. Setup was performed before
the demand edge and preserved separately from counted timing.

1. The exact RFdiffusion image was pulled to the selected H100 node. Initial
   pull-to-Ready took 239.641210 seconds outside T0.
2. The pinned NIM cache was materialized and recursively inventoried: 674
   files, 2,590,162,178 bytes, three safe internal symlinks, tree SHA-256
   `8b79aa4f4ca6a3121ca6d3d7e8083addd949a28a84b375bd5754580415eb80fd`.
3. Donor `f7-r7` reproduced the final bounded-emptyDir/PVC topology and passed
   exactly two strict semantic calls. `PodSnapshotContent` `rfd-f7-r7` became
   Ready after a 270.512581880-second checkpoint operation.
4. The direct artifact was inventoried, then the buffered variant was built by
   hard-linking all 89 payload files and changing only checkpoint identity and
   `imageIoMode`.
5. A direct compatibility canary passed. Its target was cleaned before any
   selected buffered trial.
6. An exploratory buffered cohort exposed stale page-cache residency and was
   preserved as excluded evidence. No aggregate from that cohort was accepted.
7. The buffered holder was recreated after all direct activity and performed a
   new full read: cache 32.633541 seconds, artifact 16.332096 seconds, total
   48.965637 seconds. The exact receipt SHA-256 is
   `17afc7961933a10cd7b1ab6d0d391a54f459bf1f5db67bbb51be61cae5d0920d`.
8. A fresh zero-GPU gate verified the exact allowed API server, full non-MIG
   H100, healthy runtime, live imageID, image holder, artifact holder, and the
   absence of all three selected run directories.
9. `rfd-f7-warm-1`, `rfd-f7-warm-2`, and `rfd-f7-warm-3` ran serially. Every
   fresh target had a unique UID, the same immutable storage state, a successful
   native worker receipt, semantic HTTP readiness, Kubernetes Ready evidence,
   exactly two distinct strict calls, and cleanup before the next T0.
10. The n=3 aggregate passed and all run-scoped GPU resources were removed.
    The selected CPU holders, PVCs, captured artifacts, and evidence remain.

## Acceptance record

- selected route: `buffered_fully_prewarmed`
- trials: 3/3 PASS
- strict semantic calls: 6/6 PASS
- median T0 to HTTP Ready: 17.662044 seconds
- median call 1: 7.892573 seconds
- median call 2: 5.584081 seconds
- median T0 through call 2: 31.379359 seconds
- median worker restore: 11.521 seconds
- aggregate SHA-256:
  `5e27493276dfd1eda3eb640c1bfe4655e378060ceba8a77619abb3271f27f0b6`

The aggregate and full arrays are recorded in `results.json` and the retained
evidence root named there.

## Reproduction order

For another run on the same prepared node, refresh the exact buffered holder
after any direct restore or other memory-pressure activity. Wait for its new
Ready condition and save its full-read receipt. Then verify zero active GPU
requests and run:

```console
./run_provisioned_n3.sh \
  --run-prefix UNIQUE_PREFIX \
  --image-io-mode buffered \
  --artifact-manifest-sha256 5d47f0fac7bba60bdab3e29843f2fd99150491e917f7f3758a84176aef8c7f9d \
  --evidence-root ABSOLUTE_EVIDENCE_ROOT \
  --kubeconfig ABSOLUTE_KUBECONFIG \
  --artifact-holder rfd-buffered-holder-f7-r7 \
  --image-cache-holder rfdiffusion-native-f7-image-holder-nkpcb \
  --allow-performance-validation-worker \
  --cleanup
```

Do not combine trials across holder refreshes. A coherent n=3 requires a
byte-identical storage-state receipt in all three summaries, unique target
UIDs, one artifact manifest, exact T0 provenance, and six semantic passes.
