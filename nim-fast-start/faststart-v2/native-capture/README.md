# OpenFold2 native artifact capture

These manifests reproduce the native Dynamo artifact used by the production
canary:

- exact OpenFold2 image:
  `nvcr.io/nim/openfold/openfold2@sha256:fc64916731fee39e124225829dca78e80ec24fe1891be47057d0d69209b93ab4`;
- checkpoint ID `openfold2-native-f7-v1`, artifact version `1`;
- one H100, 14 CPU and 128 GiB requested for the donor;
- the same six external mount paths used by the restore target; and
- two distinct successful semantic warm-up requests before capture.

`snapshot-values.yaml` configures the native capture agent and
`openfold2-podsnapshotcontent.yaml.tmpl` binds capture to the exact donor Pod
UID. `openfold2-donor-job.yaml` references Kubernetes image-pull/API-key Secret
objects by name; it contains no credential value.

The retained artifact has 202 regular files totaling 7,290,652,785 bytes and a
manifest SHA-256 of
`78368af3e6f143d7dc681632c4150b29f6354717103638b56e776244d9631b04`.
Raw artifact and Pod evidence intentionally remain outside Git.
