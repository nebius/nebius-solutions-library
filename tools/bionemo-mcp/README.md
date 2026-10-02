# Nebius BioNeMo MCP

`nebius-bionemo-mcp` exposes a customer-owned BioNeMo NIM fleet through typed
Model Context Protocol tools. The server reads the `nim_catalog` Terraform
output from `modules/nims`; it does not maintain another service or port map.
At startup it probes `/v1/health/ready` and registers model tools only for
catalog entries that are both enabled and ready.

The same Python implementation supports local `stdio` and production
Streamable HTTP. HTTP mode is stateless so requests can be balanced across the
chart's gateway replicas, and requires a static bearer token in v1. OAuth
discovery and token issuance are intentionally not implemented.

## Supported tools

Always present:

- `list_models`
- `fleet_health`

Health-gated model tools:

- `boltz2_predict`
- `openfold2_predict`
- `openfold3_predict`
- `diffdock_dock`
- `genmol_generate`
- `molmim_run`
- `msa_search`
- `rfdiffusion_generate`
- `proteinmpnn_design`
- `evo2_run`

Health-gated pipelines:

- `drug_discovery_pipeline` requires GenMol, DiffDock, and Boltz2.
- `msa_structure_prediction_pipeline` requires MSA Search and OpenFold3.

Every inference writes the complete validated input as `request.json`, the
complete NIM response as `response.json`, and recognized scientific files to
the configured artifact store. Production uses Nebius Object Storage and returns
time-limited presigned download URLs plus SHA-256 checksums. Local development
writes `file:` URLs below `BIONEMO_ARTIFACT_DIRECTORY`.

## Catalog input

Produce the routing contract from the ARCHVTEAMS-2369 module:

```bash
terraform -chdir=<deployment> output -json nim_catalog > nim-catalog.json
```

The complete `terraform output -json` object is also accepted. By default,
every service URL must be plain HTTP on a `*.svc.cluster.local` hostname, the
hostname must begin with the exported service name, and the URL port must equal
the exported service port. This prevents an altered catalog from turning the
gateway into a general network proxy. Tests and explicit local development can
set `BIONEMO_ALLOW_NON_CLUSTER_URLS=true`.

## Local development

```bash
uv sync --all-groups
BIONEMO_CATALOG_FILE=./nim-catalog.json \
  BIONEMO_ALLOW_NON_CLUSTER_URLS=true \
  uv run nebius-bionemo-mcp --transport stdio
```

For Streamable HTTP, use a random token of at least 32 characters:

```bash
export BIONEMO_BEARER_TOKEN="$(openssl rand -hex 32)"
BIONEMO_CATALOG_FILE=./nim-catalog.json \
  BIONEMO_ALLOW_NON_CLUSTER_URLS=true \
  uv run nebius-bionemo-mcp --transport streamable-http --host 127.0.0.1 --port 8000
```

The MCP endpoint is `/mcp`; unauthenticated requests receive `401`. The
unauthenticated `/healthz` endpoint reports process readiness and registered
tool names without exposing service URLs or credentials, while `/livez` is a
minimal process-liveness check. `stdio` relies on the launching process as its
security boundary and does not use HTTP bearer auth.

## Production configuration

The Helm chart under `deploy/helm/nebius-bionemo-mcp` mounts the catalog and
references pre-created Kubernetes Secrets for the MCP bearer token and fresh
Object Storage credentials. Important settings are:

| Environment variable | Purpose |
| --- | --- |
| `BIONEMO_CATALOG_FILE` | Path to `terraform output -json nim_catalog`. |
| `BIONEMO_TRANSPORT` | `stdio` or `streamable-http`. |
| `BIONEMO_BEARER_TOKEN_FILE` | Mounted HTTP bearer-token file. |
| `BIONEMO_ARTIFACT_BACKEND` | `s3` in production, `local` for development. |
| `BIONEMO_S3_BUCKET` | Fresh task/customer-owned Object Storage bucket. |
| `BIONEMO_S3_ENDPOINT_URL` | Regional Nebius Object Storage S3 endpoint. |
| `BIONEMO_S3_REGION` | Bucket region. |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | Fresh service credentials. |
| `BIONEMO_REQUEST_TIMEOUT_SECONDS` | NIM call timeout; default 30 minutes. |
| `BIONEMO_PRESIGN_TTL_SECONDS` | Artifact URL lifetime; default one hour. |

Do not put tokens or S3 secret keys in Helm values. The chart accepts existing
Secret names and keys only.

The chart keeps its Service at `ClusterIP`. Production exposure is an optional
TLS Ingress route for `/mcp` only; `/healthz` and `/livez` remain cluster-internal. The
default NetworkPolicies allow the gateway to reach enabled NIM pods, cluster
DNS, and HTTPS Object Storage, and restrict ingress to those NIM pods to the
gateway plus explicitly configured monitoring clients. This makes the NIM
LoadBalancer paths created by older deployments ineffective when the cluster's
CNI enforces Kubernetes NetworkPolicy.

Create fresh secrets and a values overlay from the Terraform output, then deploy
an immutable image digest:

```bash
terraform -chdir=<nim-deployment> output -json nim_catalog \
  | jq '{nimCatalog: .}' > /tmp/bionemo-catalog-values.json

kubectl --namespace bionemo create secret generic bionemo-mcp-auth \
  --from-literal=token="$BIONEMO_MCP_TOKEN"
kubectl --namespace bionemo create secret generic bionemo-mcp-s3 \
  --from-literal=access-key-id="$AWS_ACCESS_KEY_ID" \
  --from-literal=secret-access-key="$AWS_SECRET_ACCESS_KEY"

helm upgrade --install bionemo-mcp deploy/helm/nebius-bionemo-mcp \
  --namespace bionemo --create-namespace \
  --values /tmp/bionemo-catalog-values.json \
  --values <task-owned-production-values.yaml> \
  --set-string image.digest=sha256:<published-digest> \
  --wait --timeout 15m
```

`nimCatalog` is the direct map returned by `terraform output -json nim_catalog`,
not a hand-maintained endpoint list. Restart the Deployment after changing model
readiness if the catalog itself did not change: tool registration is intentionally
fixed for each MCP process lifetime so all requests see a stable tool surface.
`list_models` and `fleet_health` report the catalog image and version for every
model so result records can identify the exact fleet configuration.

## MCP clients

Export `BIONEMO_MCP_TOKEN`, replace the example hostname, and use the checked-in
configuration for the client:

- Codex: `examples/codex-config.toml` is the four-line MCP configuration and sets
  `tool_timeout_sec = 3600` for model warm-up and long inference calls.
- Claude Code: `examples/claude-mcp.json` uses Streamable HTTP and an environment-
  expanded `Authorization` header.
- Cursor: `examples/cursor-mcp.json` uses its HTTP MCP configuration with the same
  bearer header and Cursor's `${env:BIONEMO_MCP_TOKEN}` interpolation. Launch the
  IDE from an environment that contains the token, or provide it through the
  desktop session's environment.
- Local agents: `examples/stdio-mcp.json` starts the same implementation over
  stdio; bearer authentication applies only to HTTP.

OAuth discovery and token issuance are future work. Static bearer credentials
must be random, at least 32 characters, rotated through the existing Kubernetes
Secret, and sent only over TLS.

## Agent skills

`skills/nebius-bionemo/SKILL.md` is the operator-facing selection and artifact
guidance for MCP clients. Detailed model science and parameter references remain
in the upstream NVIDIA BioNeMo Agent Toolkit; the MCP tool schemas are the
authoritative execution contract for this gateway.

## Retrieve artifacts

Save a tool's structured result as JSON and download every referenced artifact
into a dedicated run directory:

```bash
uv run nebius-bionemo-download result.json --run-dir runs/my-experiment
```

The downloader accepts HTTPS presigned URLs (and `file:` URLs for local
development), does not follow redirects, and verifies every advertised SHA-256
checksum.

## Verification

```bash
uv run ruff check .
uv run mypy
uv run pytest
helm lint deploy/helm/nebius-bionemo-mcp \
  --values deploy/helm/nebius-bionemo-mcp/values.test.yaml --strict
docker build --tag nebius-bionemo-mcp:test .
```

The production boundaries and capacity-gated live validation checklist are in
[`deploy/README.md`](deploy/README.md).
