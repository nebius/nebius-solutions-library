# OpenCost

Installs the upstream OpenCost **2.5.33** Helm chart. Connect it to an existing Prometheus; the module installs no monitoring
stack and does not own application billing or tenant data.

```hcl
module "opencost" {
  source         = "../modules/opencost"
  prometheus_url = "http://prometheus-server.monitoring.svc:9090"
  values         = [file("opencost-values.yaml")]
  providers      = { helm = helm }
}
```

Inputs are `prometheus_url`, `namespace`, `chart_version`, `chart_path` and
`values`. Native values can set cluster identity, pricing, credentials,
ServiceMonitor integration and UI configuration. Keep provider-specific pricing
explicit for the target pools; this module supplies no guessed GPU prices.

Prometheus must scrape the metrics needed by OpenCost, including kube-state-metrics
and kubelet/cAdvisor. Enabling a ServiceMonitor additionally requires a Prometheus
Operator and its CRDs. Review the existing monitoring owner before enabling it.

Reference: [upstream chart](https://github.com/opencost/opencost-helm-chart/tree/opencost-2.5.33/charts/opencost).
