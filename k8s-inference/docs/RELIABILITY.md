# Reliability and upgrade contracts

The deployment owner is Terraform (`stack.sh`). Helm owns upstream controllers; the API owns customer model definitions. The platform has no GitOps controller or GitHub deploy key.

## Deploy, change and validate

Follow the root README for bootstrap, credentials and the cloud/platform/models stages. Chart versions are declared once in `clusters/common/apps/*.yaml`; cluster-specific native values are computed in `stack/platform/stage.tf`. Terraform 1.12+ is required by the shared External Secrets modules.

Before a change, run `make check` and the UI tests/build. PostgreSQL regression tests run when `TEST_DATABASE_URL` points to a disposable database; CI supplies a PostgreSQL service. These checks create no cloud resources.

For an existing platform state using `alekc/kubectl`, back up the state and run the following against each initialized platform backend before upgrading. The reused MysteryBox module requires the library's `gavinbunney/kubectl` provider. Resource addresses remain unchanged.

```sh
terraform -chdir=stack/platform state replace-provider registry.terraform.io/alekc/kubectl registry.terraform.io/gavinbunney/kubectl
terraform -chdir=stack/platform init -upgrade
```

Review the plan for every cluster. Do not accept unexpected database, volume or bucket replacement. Publish changed platform images before applying their version tags. CRD-bearing charts precede dependent charts through explicit Terraform waves.

## Model updates and recovery

`POST /v1/models`, `PUT /v1/models/{id}` and `DELETE /v1/models/{id}` require an admin key and the control API. A write commits the definition, monotonic history and a pending deployment record in one transaction before contacting a worker. The API attempts deployment immediately; a response with `pending: true` means the desired definition was saved and deployment will be retried every 15 seconds. Multiple API replicas serialize reconciliation through a database advisory lock.

Retries apply the same endpoint chart, certificates, LiteLLM groups and regional catalogue copies. Old regions remain in the cleanup record until the change completes. A concurrent newer generation cannot be removed by an older reconciliation. Deleting and recreating an ID continues its version history. Send `If-Match: <version>` with replace/delete to reject a stale edit with 409.

Inspect API logs and the `model_changes` table for queued errors. Restoring worker access allows reconciliation to resume; no manual catalogue edits or database rollback are needed. Regional APIs serve read-only copies and reject model writes. Bundled/terraform-managed classes remain read-only in the UI.

Jobs use Kubernetes Jobs or JobSet. An idempotent replay repairs a missing per-operation PVC or Secret; reusing the key with a different rendered job returns 409. A resumed run remains in the original region so its checkpoint volume is available. PVC contents survive pod preemption but not deletion of the volume; local NVMe scratch is disposable. Attempt records and outputs are uploaded to the tenant's Object Storage bucket.

## Accounting and budgets

LiteLLM's upstream chart owns keys, token accounting, database migrations and proxy budgets. Its two replicas share the existing gateway Redis service. Redis uses a persistent AOF, TTL-based eviction when its memory limit is reached (every counter carries a TTL; a failed write would make LiteLLM fail closed) and namespace-restricted ingress. LiteLLM 1.104.0 fails closed on budget checks when Redis is down; its rate limits then stay per pod (the fail-closed rate-limit setting exists only in later releases). Database-authoritative budget enforcement and fail-closed rate-limit enforcement are enabled. Redis remains one replica: an outage can reject proxy traffic until recovery rather than silently bypassing limits. For a strict availability SLO, supply an operated HA Redis service through the native chart values and validate failover.

Completed GPU jobs use a small ledger in the **existing LiteLLM database**. `(region, Kubernetes Job UID)` is the unique charge identity. Insertion of the receipt and atomic increments of `LiteLLM_VerificationToken.spend` and `total_spend` commit together. Retrying after an annotation failure never charges twice; concurrent proxy/GPU increments cannot overwrite one another. The Job annotation is a display receipt, not the deduplication authority.

This integration is intentionally tied to pinned LiteLLM 1.104.0's token schema. Before upgrading, verify its token hash, spend columns, budget checks and budget reset behavior, then run transaction tests and a proxy smoke test. Missing keys, unknown prices or unavailable databases defer accounting with an error. Never replace this with a read/add/write `/key/update` call.

Jobs snapshot regional pool rates at submission, including explicitly configured model prices. Attempt records include the admitted pool, so API-created GPU jobs retain a price after model edits/deletion. Legacy jobs without snapshots use their catalogue's regional price. GPU usage is charged after completion; this is metering, not a reservation system or a hard cap on already running work. API authorization caches key state for a short configurable interval (`AUTH_CACHE_S`).

## Secrets and TLS

Use `env_secrets: ["app-credentials"]` in a model definition to reference workload Secrets. Endpoint Secrets live in `models`; run Secrets live in the tenant namespace. Provision them on every eligible cluster. The optional `secrets.workload_secrets` map reuses the library's `modules/external-secrets-operator` and `modules/external-secret-mysterybox`; it requires a pre-provisioned credentials Secret containing `subject-credentials.json` in each target namespace. `create_namespace` defaults to false; enable it only for namespaces not already owned by platform/tenant Terraform. First deploy the platform with this map empty, provision the credentials Secret and any tenant namespaces, then enable the map and re-apply. Do not target Secrets already generated by this solution.

Generated infrastructure credentials still appear in sensitive Terraform state. Restrict and encrypt the remote state bucket, retain recoverable versions, and limit its operators. MysteryBox payloads stay outside model definitions and Terraform values. Literal environment values are accepted for ordinary configuration; secrets belong in Secret references. Only admins can read full model specs/history.

Database clients verify the server certificate and hostname, and the API's own HTTPS clients (regional forwards, LiteLLM) read the same bundle through `SSL_CERT_FILE`. `trust_bundle_pem` optionally supplies a complete PEM trust bundle, mounted over the image's system CA bundle for the API, billing, database bootstrap and LiteLLM. It must include both public roots and any private roots needed by the fleet. There is no TLS verification bypass.

Public gateways use configured DNS and cert-manager ACME. Internal gateways require `edge.domain`, `edge.private_ca_secret` (`tls.crt`/`tls.key` in `cert-manager`) and the fleet trust bundle. They use a private CA ClusterIssuer and do not install public ACME issuers. Clients/browsers must trust that CA. See `EDGE.md`.

## Storage, backups and monitoring

Managed PostgreSQL contains both `platform` (models/history/pending changes) and `litellm` (keys/spend/GPU receipts). Back up and restore them together with the matching LiteLLM master/encryption credentials. Practice a restore to an isolated environment before a production launch; database backups do not back up checkpoint PVCs or image/weights caches.

Loki uses a separate Object Storage bucket per cluster, native chart S3 settings and the existing operations S3 identity. Compactor retention follows `observability.loki_retention_days`; its PVC holds working data rather than the only copy of logs. Existing fleets with filesystem-backed Loki must migrate old data or retain the old schema/storage for its retention period before changing the schema; switching the store in place does not copy historical logs.

Prometheus, Alloy, DCGM and OpenCost remain upstream charts. The UI embeds endpoint/app monitoring through the API; Grafana remains available to operators. Alert rules and dashboards are in `clusters/common/manifests/observability`.

The cost dispatcher keeps two ranking replicas and reuses Kubernetes-client leader election. Only the elected replica nominates workloads or changes worker PVCs. Losing renewal terminates that process; Kubernetes restarts it. Freshness probes remove stale ranking replicas from service. Prices and checkpoint-aware placement remain custom because the built-in MultiKueue dispatchers do not implement all these policies.

Spot-node recovery and credential rotation remain in `services/ops`. Review their logs, stale key/rotation alerts and worker token expiry. Growing shared weights or image-cache volumes must be planned through the existing volume owner; do not recreate a PVC to resize it.

## Release acceptance and destruction

Local/CI validation is necessary but does not prove a fresh Managed PostgreSQL deployment, real certificate issuance, MultiKueue failover, GPU preemption/resume or backup restoration. The previously reported fresh deployment was blocked by PostgreSQL quota; repeat those integration drills on an authorized test fleet before a customer production release. Also complete the organization's security review and agree an availability SLO.

After a real deployment, verify unauthorized/forbidden endpoint calls, allowed-key HTTP/WebSocket traffic, scale from zero, pending regional recovery, one charged/retried GPU operation, dispatcher leader replacement, Redis failure behavior, TLS rejection of an untrusted CA, log persistence and database restore.

`protect_data` controls the wrapper's destruction guard. Destruction can remove tenant artifact/log buckets and databases. Export required data and backups first; confirm the generated plan and bucket cleanup behavior. See the root README for the exact commands.

## Upgrades

At every start the control API queues a no-op change for each model that has none pending, and the reconciler re-renders every endpoint and run class once (server-side apply, idempotent). Objects a release renders and the previous one did not, such as the per-endpoint `SecurityPolicy` that replaced the namespace-wide key check, therefore exist after the upgrade without an admin re-saving every model.
