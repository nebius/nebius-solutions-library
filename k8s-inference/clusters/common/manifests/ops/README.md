# ops: hardening manifests

Shared base for both clusters (Terraform component `ops`, wave 2, from
`clusters/common/apps/ops.yaml`); the per-cluster overlay in
`clusters/<cluster>/apps/overlays/ops/` patches project, node group and
registry (the fleet database is a Nebius Managed PostgreSQL whose backups are the service's,
`docs/OPERATIONS.md` "Database"; nothing to back up from the cluster). Runbook: `docs/OPERATIONS.md`; numbers:
`spikes/S13-ops/RESULT.md`. Image: `services/ops` (Nebius CLI, crane, jq, curl),
tag pinned here.

| File | What |
|---|---|
| `namespace.yaml` | namespace `ops`; Secret `nebius-sa` (SA key, created with kubectl, never in git) |
| `recover-stopped-nodes.yaml` | CronJob, every 2 min: `instance start` for STOPPED VMs of the spot node groups, delete on start failure |
| `tenant-isolation.yaml` | CiliumClusterwideNetworkPolicy for every namespace labeled `serverless2.nebius/tenant`; the per-namespace `tenant-local` policy comes from `charts/tenant` |

API RBAC lives with the API (`clusters/common/manifests/api/api.yaml`).
Secret expected: `ops/nebius-sa` (key `<region>.json`, the ops service account's key of this cluster's project;
the platform stage writes it, `stack/platform/secrets.tf`). `ops-login.sh` of the image turns every such file
into a CLI profile and a registry credential helper for that region.
