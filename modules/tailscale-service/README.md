#####################################################################
# NOTE: This is a module and should not be run manually or standalone
#####################################################################

Creates a Kubernetes `LoadBalancer` Service that is exposed to a Tailscale
tailnet through the Tailscale Kubernetes operator.

Use this module after the operator has already been installed in the cluster.
It is generic and can expose any workload that can be fronted by a Kubernetes
Service selector.

What this module manages:

- One Kubernetes `Service` with `load_balancer_class = "tailscale"`
- Tailscale hostname and tag annotations on that service
- One or more service ports for the selected workload

What this module expects from the caller:

- A running Tailscale Kubernetes operator in the same cluster
- A stable selector for the target workload
- The desired tailnet hostname and service ports

Creation boundary:

- Created outside this module:
  - The Tailscale operator itself
  - The backend workload selected by `selector`
  - Any tailnet ACL policy that governs access to the exposed service
- Created by this module in Terraform:
  - The Tailscale-backed Kubernetes `Service`
  - The Service annotations and ports that drive the operator-managed proxy
  - The optional one-time proxy restart workaround when enabled

Inputs:

- `namespace`
- `name`
- `tailnet_hostname`
- `selector`
- `ports`
- `tags`
- `additional_annotations`
- `load_balancer_class`
- `type`
- `operator_namespace`
- `restart_generated_proxy_once_after_create`
- `restart_generated_proxy_strategy`
- `kubectl_context`
- `proxy_restart_timeout_seconds`

Outputs:

- `service_name`
- `service_namespace`
- `tailnet_hostname`
- `tailnet_endpoints`

Example usage:

```hcl
module "tailscale_service" {
  source = "../../modules/tailscale-service"

  namespace        = "my-app"
  name             = "my-app-ts"
  tailnet_hostname = "my-app"

  selector = {
    "app.kubernetes.io/name" = "my-app"
  }

  ports = [
    {
      name        = "http"
      port        = 8080
      target_port = 8080
    }
  ]

  providers = {
    kubernetes = kubernetes
  }
}
```

Example for exposing an internal REST service:

```hcl
module "my_rest_api_tailscale" {
  source = "../../modules/tailscale-service"

  namespace        = "platform"
  name             = "my-rest-api-ts"
  tailnet_hostname = "my-rest-api"

  selector = {
    "app.kubernetes.io/name"      = "my-rest-api"
    "app.kubernetes.io/instance"  = "platform"
    "app.kubernetes.io/component" = "api"
  }

  ports = [
    {
      name        = "http"
      port        = 8080
      target_port = 8080
    }
  ]

  depends_on = [module.tailscale_operator]

  providers = {
    kubernetes = kubernetes
  }
}
```

Testing workflow:

1. Install the operator with `modules/tailscale-operator`.
2. Apply this module from the workload’s root Terraform module.
3. Wait for the Service to receive a Tailscale ingress hostname.
4. Test from an authorized tailnet device using the returned hostname or
   `tailnet_endpoints` output.
5. Tighten ACLs in Tailscale after confirming basic connectivity.


Notes:

- The Tailscale operator must already be installed in the target cluster.
- Add an explicit `depends_on = [module.tailscale_operator]` in the calling
  module when the operator and exposed service are created in the same apply.
- Tailscale ACLs still determine which users or devices may reach the service.
- For protected APIs, a successful unauthenticated `401` can still be a useful
  validation signal because it proves the tailnet path and service forwarding
  are working.
- `restart_generated_proxy_once_after_create` is a temporary opt-in workaround,
  not normal desired state. Leave it `false` unless you are hitting the stale
  proxy behavior described below.
- `restart_generated_proxy_strategy` defaults to `template_annotations`
  because that is the cleaner declarative strategy. It discovers the
  operator-generated StatefulSet by label and patches its pod-template
  annotations through the Kubernetes provider.
- The module ties that one-time patch to the Service UID and ignores later
  drift in the patched template annotation, because the Tailscale operator can
  reconcile the generated StatefulSet template after the rollout begins.
- `local_exec` remains available as an explicit fallback when you prefer an
  imperative restart plus an explicit readiness wait for the recreated pod.
- If you enable `restart_generated_proxy_once_after_create`, set
  `kubectl_context` explicitly when you choose the `local_exec` strategy so the
  one-time pod delete targets the intended cluster.

Design guidance:

- Create the operator once per cluster, then add one `tailscale-service` module
  per exposed workload.
- Use stable, descriptive service names and hostnames so replacements are
  obvious in both Terraform state and the tailnet admin console.
- Prefer a narrow selector that matches only the intended backend pods.

Troubleshooting:

- If the proxy device is present on the tailnet and the backend is reachable
  from inside the proxy pod, but external TCP or HTTP requests still time out,
  the issue may be stale proxy state rather than your service definition.
- Observed root cause:
  the operator could create a proxy that looked healthy from Kubernetes and
  Tailscale's point of view (`Machines` entry present, `tailscale ping` works,
  backend reachable from inside the proxy), but the proxy still timed out for
  inbound TCP and HTTP until the generated pod was recreated.
- This matches the upstream Tailscale operator ingress issue tracked in
  `tailscale/tailscale` issue `#12079`:
  <https://github.com/tailscale/tailscale/issues/12079>
- Temporary automation:
  set `restart_generated_proxy_once_after_create = true` and
  choose a restart strategy:
  - `restart_generated_proxy_strategy = "template_annotations"` to perform a
    provider-managed rollout against the discovered operator-generated
    StatefulSet without relying on `local-exec`. This is the default and the
    recommended option.
  - `restart_generated_proxy_strategy = "local_exec"` plus
    `kubectl_context = "<your-context>"` to automate the single post-create
    pod delete and explicit readiness wait when you want that stronger
    synchronous behavior.
- Why the workaround helps:
  recreating the generated proxy causes the operator-managed StatefulSet to
  start a fresh pod, and that fresh pod can come up with working forwarding
  state.
- Follow-up work:
  the declarative `template_annotations` strategy is cleaner, but unlike the
  `local_exec` path it does not itself perform an explicit readiness wait for
  the restarted proxy. Keep that tradeoff in mind if you intentionally choose
  the fallback strategy.
