# eMudhra CA trust remediation

The October 2026 compromise affects intermediate certificates that chain through
emSign ECC Root CA - G3 and AAA Certificate Services. Both paths must be distrusted.
An updated browser does not update CA bundles inside containers or command-line tools.

Sources: [incident and test chains](https://emudhra.we-have-a-problem.com/),
[certifi releases](https://github.com/certifi/python-certifi/releases/tag/2026.10.19),
[Go security release](https://groups.google.com/g/golang-announce/c/66pojueaQek),
[Debian update](https://bugs.debian.org/1151413).

## Changes in this repository

The inference API uses one public trust source, in this order: an explicit
`SSL_CERT_FILE`, the operator's complete `trust_bundle_pem`, the system CA bundle,
then certifi if the system bundle is absent. It adds the mounted private CAs without
reintroducing certifi roots or Terraform's duplicate aggregate. The final bundle is
validated before an atomic replacement. A bad update fails the outbound operation;
it does not silently keep using the old trust file.

API and dispatcher builds require certifi 2026.10.19 or later and pip 26.2.2 or
later. Their pip bootstrap uses the refreshed OS bundle. The jobs image sets
`AWS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt`; cost-report uploads use that image
instead of `amazon/aws-cli:2.22.35`. OS packages are refreshed during clean builds.

CI checks the effective OS, certifi, pip, AWS CLI and merged API bundles against all
twelve published TLS test chains, including the AAA cross-signs. A positive trust
control must also pass. These checks run offline and use only public certificates.

OSMO Keycloak calls verify TLS, including `.local` names. For a private or self-signed
certificate, set `OSMO_KC_CA_BUNDLE` to a readable PEM file containing its trusted CA.
`OSMO_KC_SKIP_TLS_VERIFY=true` is refused. The authenticated Kubernetes port-forward
fallback remains available when there is no Keycloak ingress.

## Roll out the inference images

1. Update every custom `trust_bundle_pem` and externally supplied CA file. A stale
   custom bundle will now fail validation. Keep the private CAs the deployment needs.
2. Build and publish the new component versions using `k8s-inference/tools/images.sh build`.
   The helper uses `--pull --no-cache` so package-update layers actually run.
   The defaults are api `0.11.40`, dispatcher `0.2.13`, jobs `0.1.16` and ops `0.1.13`.
   Update explicit `fleet.images.versions` overrides as well. Use fresh tags or digests;
   republishing an unchanged tag does not update a running pod or an existing cache entry.
3. Apply the platform changes and wait for the API and dispatcher rollouts. Check the
   images in the ops and cost-export CronJobs. Existing Jobs retain their old pod
   templates: finish or replace them deliberately before considering rollout complete.
4. Run `tools/check-image-trust.sh <tag>` against locally built `serverless2/<component>:<tag>`
   images, or run `python3 tools/check-ca-trust.py /path/to/bundle.pem` on exported runtime
   bundles. Verify both rejection of the affected chains and a legitimate HTTPS request
   using the application's actual client. Check private gateway and database connections too.

## Existing nodes and externally supplied images

This branch does not patch running machines or images owned by other projects.
Inventory and update these separately:

- Managed Kubernetes and NFS node images, bastions, and existing VMs. Refresh
  `ca-certificates` through the supported OS or node-image update process. Cloud-init
  `package_update` only refreshes package indexes and does not upgrade installed CAs.
  Avoid broad, untested package upgrades on GPU nodes.
- Model and operator images, LiteLLM, Zot, and Keycloak. Check the effective image
  digest and every relevant trust store, including Java stores. A host update does
  not modify a container's bundled roots.
- Conda environments and Python virtual environments, plus bundled AWS CLI, pip and
  uv installations. Updating certifi in one environment does not update another.
- Go-based CLI tools and controllers. Go 1.26.10 and 1.27.3 added an intermediate
  block; verify vendor rebuilds or the actual trust path with patched system roots.
- Servers presenting a certificate under the distrusted hierarchy. Replace that
  certificate through the issuing CA. Private fleet, Kubernetes and MSP CAs do not
  need rotation merely because public roots changed.

Refresh trust before running HTTPS bootstrap/install scripts. For downloaded
executables, retain checksum or signature verification independent of that download.
Never bypass TLS verification to make an installation succeed.
