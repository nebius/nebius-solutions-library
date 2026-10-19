# Software Bill of Materials — `runai/` Terraform Solution

## Metadata

| Field | Value |
|---|---|
| Date | 2026-10-17 (refreshed after remediation commits 9102a0d7 / 6e0ab132 / 78971dea) |
| Branch | `main` |
| Scope | `runai/` solution: Terraform providers, direct Helm charts (incl. chart-bundled subchart deps), container-image sources, external services, tooling/CLI dependencies, shared local modules |
| SBOM tooling | Terraform v1.9.8 (`terraform init -backend=false` → `.terraform.lock.hcl`), anonymous `index.yaml` fetches from the Helm repos, `.github/workflows/terraform.yml` |
| Inventory method | Provider versions from the **committed** `runai/.terraform.lock.hcl`; constraints from `runai/provider.tf` (updated in 9102a0d7); chart metadata from published `index.yaml` files |
| Component inventory | 4 Terraform providers, 3 direct Helm charts (+ 11 subchart dependencies), 7 image/registry sources, 11 external services, 6 tooling deps, 2 shared modules |

## Terraform Providers

Locked via the committed `.terraform.lock.hcl` (see Known Gaps). Constraints from `runai/provider.tf`.

| Name | Source | Constraint | Resolved | License |
|---|---|---|---|---|
| nebius | `registry.terraform.io/nebius/nebius` | `>= 0.6.23, < 1.0` | **0.6.71** | Apache-2.0 |
| helm | `registry.terraform.io/hashicorp/helm` | `~> 3.3` | **3.3.0** | MPL-2.0 |
| kubernetes | `registry.terraform.io/hashicorp/kubernetes` | `~> 3.3` | **3.3.0** | MPL-2.0 |
| tls | `registry.terraform.io/hashicorp/tls` | `>= 4.0, < 5.0` | **4.4.1** | MPL-2.0 |

Terraform core: `required_version = ">= 1.11"` (raised from `>= 1.9` in 9102a0d7 — the gpu-operator module's write-only `sensitive` block requires Terraform 1.11+). CI pins `hashicorp/setup-terraform@v3` with `terraform_version: v1.11.4`. All four providers now have upper-bounded constraints, and the previously unconstrained `kubernetes` provider is pinned to `~> 3.3`.

## Helm Charts (direct)

| Chart | Repo | Version | appVersion | kubeVersion | Auth required |
|---|---|---|---|---|---|
| control-plane | `https://helm.ngc.nvidia.com/nvidia/runai` | **2.26.50** | 2.26.50 | `>=1.19.0-0` | Yes — NGC API key (`$oauthtoken`) |
| runai-cluster | `https://helm.ngc.nvidia.com/nvidia/runai` | **2.26.50** | 2.26.50 | `>=1.19.0-0` | Yes — NGC API key (`$oauthtoken`) |
| kubernetes-ingress (HAProxy) | `https://haproxytech.github.io/helm-charts` | **1.54.3** | 3.2.17 | `>=1.23.0-0` | No (public) |

Versions default from `runai/variables.tf` (`runai_version = "2.26.50"`, `haproxy_ingress_version = "1.54.3"`); both are overridable via `TF_VAR_`. All releases share the `helm_timeout` variable (default 2400s, added in 6e0ab132) and set `cleanup_on_fail = true`. The HAProxy release additionally pins unprivileged/restricted-securityContext operation, disables the stats/admin/QUIC service ports, pprof, and Prometheus metrics, and sets a TLS ≥1.2 minimum (`no-sslv3 no-tlsv10 no-tlsv11`) in its values (6e0ab132). Chart digests (`index.yaml`, fetched 2026-10-17):

- `control-plane` 2.26.50 — digest `6ff2fa8ac05098895e2e98a16ed0fe9f4e2b8bd22d3742ed54c477d64b734dae`, created 2026-09-23T19:48:26Z
- `runai-cluster` 2.26.50 — digest `d10510489f5b7202ed1c9c611fdc83b363d67b3c16998727122a071aeab5f95b`, created 2026-09-23T19:44:41Z
- `kubernetes-ingress` 1.54.3 — digest `1d3258dc0979e94c4ae5c4cd1c05e4ac6291a5a8bada4ab046b16f107c6d1750`, created 2026-10-16T16:43:05Z, license Apache-2.0, controller image `docker.io/haproxytech/kubernetes-ingress:3.2.17`

### GPU Operator — not a Helm chart in this solution

Contrary to a chart-repo assumption, `modules/gpu-operator/main.tf` deploys the GPU operator via the **Nebius managed-applications API** (`nebius_applications_v1alpha1_k8s_release`, `product_slug = "nebius/nvidia-gpu-operator"`, namespace `gpu-operator`). No Helm repo URL or chart version is pinned anywhere — the version is resolved by the Nebius platform at deploy time.

### Subchart dependencies (from `control-plane` 2.26.50 index entry)

| Subchart | Version | Repo |
|---|---|---|
| runai-common-chart | 1.0.62 | bundled (`file://../../runai-common-chart`) |
| postgresql | 15.5.38 | `https://charts.bitnami.com/bitnami` |
| keycloakx | 7.2.2 | `https://codecentric.github.io/helm-charts` |
| thanos | 16.0.7 | `https://charts.bitnami.com/bitnami` |
| notifications-service | 1.5.43 | `https://nvcr.io/artifactory/notifications` |
| nats | 9.0.28 | `https://charts.bitnami.com/bitnami` |
| common | 2.40.0 | `https://charts.bitnami.com/bitnami` |
| unleash-edge | 2.7.4 | `https://docs.getunleash.io/helm-charts` |

### Subchart dependencies (from `runai-cluster` 2.26.50 index entry)

| Subchart | Version | Repo |
|---|---|---|
| runai-engine | v3.41.19 | `https://nvcr.io/artifactory/op-charts-prod/` |
| karta | 0.1.0 | `oci://ghcr.io/run-ai/karta` |
| crds | 0.0.0 | bundled (`file://charts/crds`) |

Subchart versions are pinned by the parent chart's `Chart.lock` at package time; they are listed here for reachability/supply-chain awareness and are **not** pinned at the Terraform layer.

## Container Image Sources

No container images are referenced directly in Terraform — charts resolve images at deploy time. Registries/domains images originate from:

| Registry / domain | Used by | Auth |
|---|---|---|
| `nvcr.io` | All Run:ai control-plane and cluster component images; `notifications-service` and `runai-engine` subcharts | Yes — `runai-reg-creds` pull secret (`$oauthtoken` + NGC API key) |
| `docker.io/haproxytech` | HAProxy ingress controller (`kubernetes-ingress:3.2.17` per chart 1.54.3) | No |
| `docker.io/bitnami` | postgresql, thanos, nats, common subchart images (typical; chart-resolved) | No |
| `ghcr.io/run-ai` | `karta` subchart (OCI) | No (public) |

Image tags/digests are **chart-resolved and unpinned at the Terraform layer**; the pinned unit is the chart version (and its packaged `values.yaml`).

## External Services

| Host | Purpose | Failure mode if unreachable | Auth |
|---|---|---|---|
| `registry.terraform.io` | Terraform provider downloads at `init` | `init` fails; no plan/apply | None |
| `helm.ngc.nvidia.com` | Run:ai chart repo (control-plane, runai-cluster) | Helm releases fail; no Run:ai install | NGC API key (`$oauthtoken`) |
| `xfiles.ngc.nvidia.com` | Signed-URL redirect target for NGC chart tarballs | Chart download fails | Signed URL (redirect) |
| `nvcr.io` | Container registry + artifactory chart repos for Run:ai images/subcharts | Image pulls / subchart fetches fail | NGC API key pull secret |
| `haproxytech.github.io` | HAProxy ingress chart index | Ingress release fails | None |
| `release-assets.githubusercontent.com` / `github.com` | HAProxy chart tarball download (GitHub release assets) | Chart download fails | None |
| `charts.bitnami.com`, `codecentric.github.io`, `docs.getunleash.io`, `ghcr.io` | Subchart dependency repos (fetched by helm at dep resolution inside the chart package or at install for OCI) | Subchart install fails | None (public) |
| `sslip.io` | Runtime DNS wildcard for self-signed mode (`<LB-IP>.sslip.io`) | UI unreachable at its URL in self-signed mode | None |
| `storage.<region>.nebius.cloud:443` | S3-compatible Terraform state backend (`runai.tfstate`) | State read/write fails | Temp SA access key created by `environment.sh` (1-day expiry) |
| Nebius platform APIs (mk8s, iam, vpc, applications, storage) | All `nebius_*` resources incl. managed GPU-operator app | All infrastructure ops fail | `NEBIUS_IAM_TOKEN` from `nebius iam get-access-token` |

## Tooling / CLI Dependencies

| Tool | Version | Where required |
|---|---|---|
| Terraform | `>= 1.11` (`provider.tf`, raised from `>= 1.9` in 9102a0d7); CI pins **1.11.4**; SBOM originally inventoried locally with 1.9.8 | apply, CI (`terraform.yml`) |
| Helm CLI | `>= 3.14` per Run:ai install docs; **not pinned** in this repo (used implicitly by the `helm` Terraform provider) | chart installs |
| nebius CLI | **unversioned** — any recent release; `environment.sh` and CI both shell out to it (`iam get-access-token`, `vpc subnet list`, `storage bucket`, `mk8s ...`) | env setup, CI plan/test |
| jq | unversioned | `environment.sh`, CI workflows |
| coreutils (`md5sum`, GNU/BSD `date`) | unversioned | `environment.sh` (bucket-name hash, access-key expiry) |
| openssl | unversioned | CI only — generates `TF_VAR_runai_admin_password` (`openssl rand -hex 16`) |

## Shared Local Modules

| Module | Source | Versioning |
|---|---|---|
| `modules/gpu-operator` | `source = "../modules/gpu-operator"` (path-pinned) | None — tracks repo `main`; deploys via Nebius managed-apps API, chart version platform-resolved |
| `modules/cloud-init` | `templatefile("${path.module}/../modules/cloud-init/k8s-cloud-init.tftpl")` | None — tracks repo `main` |

## Known Gaps

Resolved since the initial SBOM (**fixed in 9102a0d7 unless noted**): the lockfile is now committed and the `kubernetes` provider constrained (see below); `k8s_version` is pinned to `"1.36"` and validated against the Run:ai support matrix. Remaining gaps:

1. **Chart image digests are unpinned at the Terraform layer** — images are resolved from chart `values.yaml` at deploy time; pinning the chart version is the only digest-level control. The `helm` provider cannot pin chart digests (accepted risk; digests recorded above).
2. **GPU operator version is platform-resolved** — `nebius_applications_v1alpha1_k8s_release` selects the version served by the Nebius marketplace for `nebius/nvidia-gpu-operator`; nothing in the repo records which version was deployed. (The write-only-`sensitive` silent-no-op gap was fixed in 9102a0d7: `sensitive.version` is now a SHA-256 of the rendered values.)
3. **Tooling unversioned** — nebius CLI, jq, coreutils, openssl, and the Helm CLI have no version floor recorded in the repo (only Terraform has `required_version`).
4. **NGC chart repo requires auth** — subchart deps behind `nvcr.io/artifactory/*` could not be metadata-verified anonymously; their versions are taken from the published `index.yaml` dependency lists of the parent charts (publicly fetchable).

**Supply-chain improvement (9102a0d7):** `runai/.terraform.lock.hcl` is now committed (root `.gitignore` scopes an exception for it), making the four provider versions above reproducible from any fresh checkout; constraints are upper-bounded (`nebius >= 0.6.23, < 1.0`, `helm ~> 3.3`, `kubernetes ~> 3.3`, `tls >= 4.0, < 5.0`).
