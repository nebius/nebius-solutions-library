# ops: hardening manifests

Shared base for both clusters (Argo CD Application `ops`, wave 2, from
`clusters/common/apps/ops.yaml`); the per-cluster overlay in
`clusters/<cluster>/apps/overlays/ops/` patches project, node group and
registry and, on the hub, adds the CNPG ScheduledBackup. Runbook: `docs/OPERATIONS.md`; numbers:
`spikes/S13-ops/RESULT.md`. Image: `services/ops` (Nebius CLI, crane, jq, curl),
tag pinned here.

| File | What |
|---|---|
| `namespace.yaml` | namespace `ops`; Secret `nebius-sa` (SA key, created with kubectl, never in git) |
| `recover-stopped-nodes.yaml` | CronJob, every 2 min: `instance start` for STOPPED VMs of the spot node groups, delete on start failure |
| hub overlay `backups.yaml` | CNPG ScheduledBackup for `data/postgres` (barman to the backups bucket); LiteLLM runs on that cluster, so this is the one backup |
| `tenant-isolation.yaml` | CiliumClusterwideNetworkPolicy for every namespace labeled `serverless2.nebius/tenant`; the per-namespace `tenant-local` policy comes from `charts/tenant` |

API RBAC lives with the API (`clusters/hub/apps/manifests/api/api.yaml`).
Secrets expected (kubectl, from `infra/state/<cluster>/ops/`):
`ops/nebius-sa` (key `<region>.json`), hub also `ops/nebius-sa-eu-south1`,
`data/backup-s3` (S3 access key of the hub ops SA).
