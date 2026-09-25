# OpenFold2 regional mirror qualification

The exact OpenFold2 multi-architecture index was copied registry-to-registry to
the regional Nebius registry and qualified before changing the new-node
harness.

## Immutable identity

Source and destination raw index bytes are identical:

`sha256:fc64916731fee39e124225829dca78e80ec24fe1891be47057d0d69209b93ab4`

The raw index is 687 bytes and retains both source child manifests:

- `sha256:29135d5166d567ac9d5b433039df857da35da7dc83e73e3de44a9fdff3c084a5`
- `sha256:4ace51b0217ffa8a51ad63d91ad8672e254ffa0f9a0e7a5155a376f67e2cfa41`

Skopeo copied both platforms with digest preservation. Registry work began at
`2026-08-18T05:19:08.003994786Z` and the destination index was written at
`2026-08-18T05:20:36.170010201Z`, an observed **88.166 seconds**. This is a
registry-copy measurement, not a fresh-node pull measurement.

## Runtime identity gate

An exact-digest inert placeholder on the allowed provisioned H100 started from
the regional reference with restart count zero. Both strings used by the
one-shot worker matched exactly:

- submitted `spec.containers[0].image`
- observed `status.containerStatuses[0].imageID`

Kubernetes displayed the image-config tag as `nvcr.io/nim/openfold/openfold2:latest`
in the separate status `image` field. The worker intentionally does not bind
that display-only field, so no restore-interface change is required.

The gate ran on a node that already had the content and therefore proved
identity compatibility only. The subsequent true `0 -> 1` run measured the
regional cold pull at **260.653 seconds**, versus **251.094 seconds** from NGC in
r4. The mirror therefore did not improve cold-pull latency in this sample. The
complete comparison is in [`R5_REGIONAL_RESULT.md`](R5_REGIONAL_RESULT.md).

Raw local receipts are retained under:

`<private-evidence-root>/openfold2-registry-mirror-20260818/`
