resource "helm_release" "ingress_controller" {
  name             = "haproxy"
  namespace        = "haproxy"
  repository       = "https://haproxytech.github.io/helm-charts"
  chart            = "kubernetes-ingress"
  version          = var.haproxy_ingress_version
  create_namespace = true
  wait             = true
  timeout          = var.helm_timeout
  cleanup_on_fail  = true

  values = [
    yamlencode({
      controller = {
        # Run unprivileged with a restricted container securityContext
        # (runAsNonRoot, drop ALL caps, no privilege escalation, runtime-default
        # seccomp). These are the chart defaults, pinned here so a future chart
        # upgrade cannot silently regress them.
        unprivileged                       = true
        allowPrivilegeEscalation           = false
        enableRuntimeDefaultSeccompProfile = true
        # pprof (/debug/pprof heap dumps) and the unauthenticated stats page are
        # enabled by default and exposed on the public LoadBalancer;
        # disable them on the Service. The /metrics endpoint on the stat port
        # is also unauthenticated; nothing in this solution scrapes Prometheus
        # metrics, so disable it at the controller too.
        prometheus = {
          enabled = false
        }
        pprof = {
          enabled = false
        }
        service = {
          type = "LoadBalancer"
          enablePorts = {
            stat  = false
            admin = false
            quic  = false
          }
        }
        # Pin the minimum TLS version on the TLS-terminating frontend
        # (global-config-snippet is the documented configmap key for
        # ssl-default-bind-options; chart values only pass configmap keys
        # through verbatim).
        config = {
          "global-config-snippet" = "ssl-default-bind-options no-sslv3 no-tlsv10 no-tlsv11"
        }
      }
    })
  ]

  depends_on = [
    nebius_mk8s_v1_node_group.cpu-only
  ]
}

data "kubernetes_service" "haproxy" {
  depends_on = [helm_release.ingress_controller]
  metadata {
    name      = "haproxy-kubernetes-ingress"
    namespace = helm_release.ingress_controller.namespace
  }
}
