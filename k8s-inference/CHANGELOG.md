# Changelog

The platform images (`services/api`, `services/dispatcher`, `services/jobs`, `services/ops`; tags in
`stack/config/variables.tf` `images.versions`) and the charts (`charts/fleet`, `charts/platform`, `charts/tenant`;
`version` in each `Chart.yaml`). The current versions are in `README.md` "Versions".

## Initial release

The Kubernetes inference fleet solution: a control cluster and one cluster per GPU region from one
`terraform.tfvars` (two Terraform stages: `cloud`, then `platform` per cluster), one customer API for models,
endpoints that scale to zero, queued and multi-node runs with checkpoint resume and automatic recovery,
per-tenant Kueue queues, a per-region image cache, tenants with API keys and budgets, observability and cost
reporting. Versions at the initial release: api 0.11.35, dispatcher 0.2.12, jobs 0.1.14, ops 0.1.11; charts fleet
0.1.7, platform 0.1.3, tenant 0.3.2.
