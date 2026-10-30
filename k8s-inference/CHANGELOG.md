# Changelog

## CA trust remediation — 2026-10-22

- Use one authoritative public trust store for the API and reject the compromised eMudhra hierarchy.
- Refresh Python CA packages and OS packages in clean image builds. Route AWS CLI through system roots.
- Reuse the jobs image for cost-report uploads and remove the old standalone AWS CLI image.
- Check OS, Python, pip, AWS CLI and merged API trust against both compromised signing paths in CI.
- Image versions: api 0.11.40, dispatcher 0.2.13, jobs 0.1.16, ops 0.1.13.

Build and deploy these tags before changing existing fleets. See the
[rollout instructions](../docs/security/emudhra-ca-remediation.md).

The platform images (`services/api`, `services/dispatcher`, `services/jobs`, `services/ops`; tags in
`stack/config/variables.tf` `images.versions`) and the charts (`charts/fleet`, `charts/platform`, `charts/tenant`;
`version` in each `Chart.yaml`). The current versions are in `README.md` "Versions".

## Initial release

The Kubernetes inference fleet solution: a control cluster and one cluster per GPU region from one
`terraform.tfvars` (two Terraform stages: `cloud`, then `platform` per cluster), one customer API for models,
endpoints that scale to zero, queued and multi-node runs with checkpoint resume and automatic recovery,
per-tenant Kueue queues, a per-region image cache, tenants with API keys and budgets, observability and cost
reporting. Versions at the initial release: api 0.11.39, dispatcher 0.2.12, jobs 0.1.15, ops 0.1.12; charts fleet
0.1.7, platform 0.1.3, tenant 0.3.2.

The pre-merge correctness review is covered by regressions: operation artifact ownership and scopes,
binding revocation permission, key rotation/revocation with durable LiteLLM deletion retries, removed endpoint
variants in retained regions, and record-aware operation lists and replays. Rotation rollback also cancels a
pending deletion of the restored value and serializes it with in-flight mirror retirement.

The API now shares request/response schemas, generates OpenAPI from code, centralizes access policy, and
separates fleet reconciliation from HTTP handlers. `make check` and CI gate Python format/lint (pinned Ruff),
ShellCheck, Terraform formatting/validation, Helm rendering and the API/dispatcher tests.
