# Filesystem CSI Validation

## Document Metadata

- Created By: Aaron Fagan
- Created On: 2026-03-17
- Version: 0.1.0

## Purpose

This README explains what each helper file in this folder does, why it exists,
and the order in which to use the validation workflow.

## Reference Docs

- https://docs.nebius.com/kubernetes/storage/filesystem-over-csi

This folder contains the commands and manifests used to validate the
Terraform-managed Nebius Shared Filesystem over CSI workflow for this
`k8s-training` deployment.

These files are not run automatically.

Important notes:

- This repo mounts the Nebius Shared Filesystem on nodes at `/mnt/data`.
- The Terraform already attaches the shared filesystem to the node groups and
  mounts it with cloud-init.
- Terraform now installs the CSI driver and patches the default `StorageClass`
  when a shared filesystem is present.
- The remaining purpose of this folder is host-mount verification, pod-level
  validation, and cleanup of temporary validation resources.
- The scripts default to the current `kubectl` namespace. Set
  `TEST_NAMESPACE=<namespace>` if you want to keep the validation resources
  somewhere explicit.
- Steps `02` and `03` intentionally omit `storageClassName` from their test
  PVCs so they can verify that the cluster default `StorageClass` is applied
  automatically.
- Step `01` records the temporary node-debugger pod names in `.state/` so step
  `04` can clean up only the debugger pods created by this workflow.
- Step `01` now defaults to a quick single-node shared filesystem spot check.
  Set
  `VERIFY_ALL_NODES=true` to validate every node, or `TARGET_NODE=<node-name>`
  to validate one specific node.
- The smoke and RWX manifests now use workflow-specific resource names to make
  reruns and cleanup easier to understand.

Suggested order:

1. Run `./01-verify-node-filesystem-mounts.sh`
2. Run `./02-run-csi-smoke-test.sh`
3. Run `./03-run-csi-rwx-cross-node-test.sh`
4. Run `./05-run-checkpoint-restore-test.sh` to validate model-checkpoint
   write/restore on the writer node and across every other GPU node (verifies
   data integrity by checksum; cleans up its own resources on exit)
5. Run `./04-cleanup-csi-test-resources.sh` to remove any temporary test
   resources left from steps 01–03 when you are done testing

Prerequisites:

- `kubectl` points to the target cluster.
- The cluster nodes are already provisioned by this Terraform stack.
- Terraform apply has already completed successfully, including any automatic
  shared filesystem CSI installation and default `StorageClass` patching.

## Shared Defaults

- `TEST_NAMESPACE` defaults to the current `kubectl` namespace, then falls back
  to `default`.
- `MOUNT_POINT` defaults to the mount path discovered from the Terraform
  cloud-init template, then falls back to `/mnt/data`.
- `FILESYSTEM_DEFAULT_STORAGE_CLASS_NAME` defaults to
  `csi-mounted-fs-path-sc`.
- `VERIFY_ALL_NODES` defaults to `false` for step `01`.
- `TARGET_NODE` can be used in step `01` to test one explicit node.
- The validation resources use the following fixed names:
  - `filesystem-csi-smoke-pvc`
  - `filesystem-csi-smoke-pod`
  - `filesystem-csi-rwx-pvc`
  - `filesystem-csi-rwx-writer`
  - `filesystem-csi-rwx-reader`
