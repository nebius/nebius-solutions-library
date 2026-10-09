# Per-cluster view: hostnames, image references, chart list and the per-component values/patches that
# are shared by every cluster. Shared manifests and values stay in
# clusters/common; nothing here is cluster-specific except what is derived from terraform.tfvars.
locals {
  repo   = abspath("${path.module}/../..")
  id     = var.target
  region = local.cluster.region
  name   = local.f.name

  # Public hostnames: <service>.<gateway ip>.sslip.io, or <service>.<cluster id>.<domain>.
  host_of = { for cid, c in local.cloud.clusters : cid => (
    local.f.edge.domain == null ? "${c.gateway_ip}.sslip.io" : "${cid}.${local.f.edge.domain}"
  ) }
  host               = local.host_of[local.id]
  control_host       = local.host_of[local.control_id]
  certificate_issuer = local.f.edge.mode == "internal" ? "${local.name}-private-ca" : (local.f.edge.acme.staging ? "letsencrypt-staging" : "letsencrypt")
  hostnames = merge({
    api     = "api.${local.host}"
    grafana = "grafana.${local.host}"
    }, local.role.control ? {
    app     = "app.${local.host}"
    litellm = "litellm.${local.host}"
  } : {})
  api_urls = { for cid, c in local.cloud.clusters : c.region => "https://api.${local.host_of[cid]}" if local.roles[cid].worker }
  grafana_urls = join(",", concat(
    [for cid, c in local.cloud.clusters : "${c.region}=https://grafana.${local.host_of[cid]}" if local.roles[cid].worker],
    local.dedicated ? ["control=https://grafana.${local.control_host}"] : []
  ))
  litellm_url_in_cluster = "http://litellm.litellm.svc:4000"
  litellm_url            = local.role.control ? local.litellm_url_in_cluster : "https://litellm.${local.control_host}"

  image = { for c, t in local.f.images.versions : c => "${local.images_host}/nebius/serverless2/${c}:${t}" }
  # The fleet document for the charts (stack/config/locals.tf), with the registry the cloud stage created.
  fleet_doc = merge(local.chart_fleet, { images = merge(local.chart_fleet.images, { source = local.cloud.hub.registry }) })

  # Chart list: clusters/common/apps/*.yaml (one shared release inventory).
  apps_all = { for f in fileset("${local.repo}/clusters/common/apps", "*.yaml") : trimsuffix(f, ".yaml") => yamldecode(file("${local.repo}/clusters/common/apps/${f}")) }
  # Components this stage renders itself from templates (control-only manifests) or not at all.
  apps = { for n, a in local.apps_all : n => a if(
    a.placement == "all" || (a.placement == "worker" && local.role.worker) || (a.placement == "control" && local.role.control)
  ) }
  helm_apps      = { for n, a in local.apps : n => a if a.kind == "helm" }
  manifest_apps  = { for n, a in local.apps : n => a if a.kind == "manifests" }
  manifests_base = { for n, a in local.manifest_apps : n => "../../clusters/common/manifests/${split("/", a.manifestsPath)[1] == "knative-serving" ? "knative" : split("/", a.manifestsPath)[1]}" }

  # Per-cluster Helm values (merged over clusters/common/values/<component>.yaml).
  gpu_prices = [for pn, p in try(local.clusters[local.id].pools, {}) : local.f.prices[p.platform]]
  values_override = {
    kueue = local.role.manager ? yamldecode(file("${local.repo}/clusters/control/values/kueue.yaml")) : {}
    opencost = { opencost = {
      customPricing = { costModel = {
        GPU     = length(local.gpu_prices) > 0 ? max([for p in local.gpu_prices : p.on_demand]...) : 0
        spotGPU = length(local.gpu_prices) > 0 ? max([for p in local.gpu_prices : p.spot]...) : 0
      } }
      exporter = { defaultClusterId = "${local.name}-${local.id}" }
    } }
    kube-prometheus-stack = merge(
      { prometheus = { prometheusSpec = { retention = "${local.f.observability.prometheus_retention_days}d" } } },
      local.f.observability.alert_webhook_url == null ? {} : { alertmanager = { config = { receivers = [
        { name = "null" },
        { name = "sink", webhook_configs = [{ url = "http://alert-sink.monitoring.svc:8080/alerts", send_resolved = true }] },
        { name = "heartbeat", webhook_configs = [{ url = "http://alert-sink.monitoring.svc:8080/heartbeat", send_resolved = false }] },
        { name = "platform", webhook_configs = [
          { url = "http://alert-sink.monitoring.svc:8080/alerts", send_resolved = true },
          { url = local.f.observability.alert_webhook_url, send_resolved = true },
        ] },
      ] } } }
    )
    loki = {
      loki = {
        limits_config = { retention_period = "${local.f.observability.loki_retention_days * 24}h" }
        storage = {
          type        = "s3"
          bucketNames = { chunks = local.cloud.hub.logs_buckets[local.id], ruler = local.cloud.hub.logs_buckets[local.id], admin = local.cloud.hub.logs_buckets[local.id] }
          s3 = {
            endpoint         = "storage.${local.cloud.hub.region}.nebius.cloud"
            region           = local.cloud.hub.region
            accessKeyId      = "$${AWS_ACCESS_KEY_ID}"
            secretAccessKey  = "$${AWS_SECRET_ACCESS_KEY}"
            s3ForcePathStyle = true
          }
        }
      }
      singleBinary = {
        extraArgs = ["-config.expand-env=true"]
        extraEnv = [
          { name = "AWS_ACCESS_KEY_ID", valueFrom = { secretKeyRef = { name = "cost-export-s3", key = "ACCESS_KEY_ID" } } },
          { name = "AWS_SECRET_ACCESS_KEY", valueFrom = { secretKeyRef = { name = "cost-export-s3", key = "ACCESS_SECRET_KEY" } } },
        ]
      }
    }
    # LiteLLM on the managed database (stack/platform/database.tf); UI behind the gateway only.
    litellm = {
      db           = { useExisting = true, deployStandalone = false, endpoint = "${local.cloud.database.host}:${local.cloud.database.port}", database = "litellm", url = "postgresql://$(DATABASE_USERNAME):$(DATABASE_PASSWORD)@$(DATABASE_HOST)/$(DATABASE_NAME)?sslmode=require&sslaccept=strict&sslcert=/etc/ssl/certs/ca-certificates.crt", secret = { name = "litellm-db", usernameKey = "username", passwordKey = "password" } }
      volumes      = local.trust_volumes
      volumeMounts = local.trust_mounts
    }
  }
  fleet_values = {
    cluster = local.id
    fleet   = local.fleet_doc
    prepull = { enabled = local.role.worker, images = try(local.prepull_by_cluster[local.id], []) }
  }

  # API environment (replaces the manifest's list wholesale; the overlays used to patch indices).
  api_env_common = concat([
    { name = "LITELLM_URL", value = local.litellm_url },
    { name = "LITELLM_MASTER_KEY", valueFrom = { secretKeyRef = { name = "litellm-master", key = "masterkey" } } },
    { name = "PUBLIC_API_URL", value = "https://${local.hostnames.api}" },
    { name = "GRAFANA_URLS", value = local.grafana_urls },
    { name = "CATALOG_DIRS", value = "/etc/catalog" },
    { name = "RUNNER_IMAGE", value = local.image.jobs },
    { name = "HUB_REGION", value = local.hub_region },
    ], local.f.trust_bundle_pem == null ? [] : [
    # the API's own HTTPS clients (regional forwards, LiteLLM) trust the mounted bundle, not only libpq/Prisma
    { name = "SSL_CERT_FILE", value = "/etc/ssl/certs/ca-certificates.crt" },
  ])
  # The fleet database (services/api/db.py): only the control API has it and writes model definitions.
  api_env_control = [for e in [
    { name = "DATABASE_URL", valueFrom = { secretKeyRef = { name = "database", key = "platform_url" } } },
    { name = "LITELLM_INTERNAL_KEY", valueFrom = { secretKeyRef = { name = "litellm-internal", key = "key" } } }, # the model groups' key (litellm-internal.tf)
    { name = "IMAGES_HOST", value = local.images_host },
    { name = "IMAGES_SOURCE", value = local.cloud.hub.registry },
    { name = "ENDPOINT_DOMAINS", value = join(",", [for cid, c in local.region_clusters : "${c.region}=${local.host_of[cid]}"]) },
    { name = "ACME_ISSUER", value = local.certificate_issuer },
  ] : e if local.role.control]
  api_env = concat(local.api_env_common, local.api_env_control, local.role.manager ? [
    { name = "REGION", value = "control" },
    { name = "REGION_KUBECONFIGS", value = join(",", [for cid, c in local.region_clusters : "${c.region}=/etc/kubeconfigs/${c.region}"]) },
    { name = "REGION_API_URLS", value = join(",", [for r, u in local.api_urls : "${r}=${u}"]) },
    { name = "FLEET_MANAGER", value = "true" },
    { name = "S3_ENV_SECRET", value = "s3-fleet" },
    ] : [
    { name = "REGION", value = local.region },
    { name = "REGION_KUBECONFIGS", value = "" },
    { name = "ENDPOINT_DOMAIN", value = local.host }, # async endpoint calls go through this cluster's gateway (TLS, key check)
  ])

  # JSON 6902 patches per manifests component (what clusters/<cluster>/apps/overlays did).
  patches = {
    api = concat([
      { target = { kind = "Deployment", name = "api", namespace = "api" }, patch = yamlencode(concat([
        { op = "replace", path = "/spec/template/spec/containers/0/env", value = local.api_env },
        { op = "replace", path = "/spec/template/spec/containers/0/image", value = local.image.api },
        { op = "add", path = "/spec/template/spec/volumes/-", value = { name = "catalog", configMap = { name = "catalog" } } },
        { op = "add", path = "/spec/template/spec/containers/0/volumeMounts/-", value = { name = "catalog", mountPath = "/etc/catalog", readOnly = true } },
        ], [for v in local.trust_volumes : { op = "add", path = "/spec/template/spec/volumes/-", value = v }],
      [for v in local.trust_mounts : { op = "add", path = "/spec/template/spec/containers/0/volumeMounts/-", value = v }])) },
      { target = { kind = "HTTPRoute", name = "api", namespace = "api" }, patch = yamlencode([{ op = "replace", path = "/spec/hostnames/0", value = local.hostnames.api }]) },
      { target = { kind = "BackendTrafficPolicy", name = "api-rate-limit", namespace = "api" }, patch = yamlencode([{ op = "replace", path = "/spec/rateLimit/global/rules/0/limit/requests", value = local.f.edge.api_rate_limit_per_minute }]) },
      ], local.role.worker ? [] : [
      { target = { kind = "Role", name = "serverless2-api-endpoints", namespace = "models" }, patch = yamlencode([{ op = "replace", path = "/metadata/name", value = "serverless2-api-endpoints-unused" }]) },
    ])
    edge = []
    gateway = concat([
      { target = { kind = "Gateway", name = "serverless2-external", namespace = "envoy-gateway-system" }, patch = yamlencode([
        { op = "replace", path = "/spec/listeners/1/hostname", value = "*.${local.host}" },
        { op = "replace", path = "/spec/listeners/1/tls/certificateRefs/0/name", value = "${local.id}-wildcard-tls" },
      ]) },
      { target = { kind = "EnvoyProxy", name = "external", namespace = "envoy-gateway-system" }, patch = yamlencode([
        { op = "add", path = "/spec/provider/kubernetes/envoyService/annotations", value = merge(
          local.cluster.gateway_allocation_id == null ? {} : { "nebius.com/load-balancer-allocation-id" = local.cluster.gateway_allocation_id },
          local.f.edge.mode == "internal" ? { "nebius.com/load-balancer-type" = "internal" } : {}
        ) },
      ]) },
      ], local.f.edge.mode == "internal" ? [
      { target = { kind = "ClusterIssuer", name = "letsencrypt" }, patch = "$patch: delete\napiVersion: cert-manager.io/v1\nkind: ClusterIssuer\nmetadata:\n  name: letsencrypt\n" },
      { target = { kind = "ClusterIssuer", name = "letsencrypt-staging" }, patch = "$patch: delete\napiVersion: cert-manager.io/v1\nkind: ClusterIssuer\nmetadata:\n  name: letsencrypt-staging\n" },
      ] : local.f.edge.acme.email == "" ? [] : [
      { target = { kind = "ClusterIssuer", name = "letsencrypt" }, patch = yamlencode([{ op = "add", path = "/spec/acme/email", value = local.f.edge.acme.email }]) },
      { target = { kind = "ClusterIssuer", name = "letsencrypt-staging" }, patch = yamlencode([{ op = "add", path = "/spec/acme/email", value = local.f.edge.acme.email }]) },
    ])
    knative-serving = [
      { target = { kind = "KnativeServing", name = "knative-serving", namespace = "knative-serving" }, patch = yamlencode([{ op = "replace", path = "/spec/config/domain", value = { (local.host) = "" } }]) },
    ]
    observability = [
      { target = { kind = "CronJob", name = "cost-export", namespace = "monitoring" }, patch = yamlencode(concat([
        { op = "replace", path = "/spec/jobTemplate/spec/template/spec/containers/0/env/0/value", value = "${local.name}-${local.id}" },
        { op = "replace", path = "/spec/jobTemplate/spec/template/spec/containers/0/env/1/value", value = local.cloud.hub.backups_bucket },
        { op = "replace", path = "/spec/jobTemplate/spec/template/spec/containers/0/env/2/value", value = local.cloud.hub.backups_bucket_endpoint },
        { op = "replace", path = "/spec/jobTemplate/spec/template/spec/containers/0/env/3/value", value = try(local.cloud.hub.backups_bucket_region, local.hub_region) },
      ], local.f.observability.cost_export ? [] : [{ op = "replace", path = "/spec/suspend", value = true }])) },
    ]
    # cost dispatcher + spot price feed (control only): the platform image tag from images.versions
    dispatcher = [
      { target = { kind = "Deployment", name = "dispatcher", namespace = "kueue-system" }, patch = yamlencode([{ op = "replace", path = "/spec/template/spec/containers/0/image", value = local.image.dispatcher }]) },
      { target = { kind = "CronJob", name = "price-feed", namespace = "kueue-system" }, patch = yamlencode([{ op = "replace", path = "/spec/jobTemplate/spec/template/spec/containers/0/image", value = local.image.dispatcher }]) },
    ]
    ops = concat([
      { target = { kind = "CronJob", name = "rotate-nebius-keys", namespace = "ops" }, patch = yamlencode([{ op = "replace", path = "/spec/suspend", value = true }, { op = "replace", path = "/metadata/name", value = "rotate-nebius-keys-unused" }]) },
      ], local.role.worker ? [
      { target = { kind = "CronJob", name = "recover-stopped-nodes", namespace = "ops" }, patch = yamlencode([
        { op = "replace", path = "/spec/jobTemplate/spec/template/spec/containers/0/env/0/value", value = local.cluster.project_id },
        { op = "replace", path = "/spec/jobTemplate/spec/template/spec/containers/0/env/2/value", value = local.cluster.cluster_id },
        { op = "replace", path = "/spec/jobTemplate/spec/template/spec/containers/0/image", value = local.image.ops },
      ]) },
      ] : [
      { target = { kind = "CronJob", name = "recover-stopped-nodes", namespace = "ops" }, patch = yamlencode([{ op = "replace", path = "/spec/suspend", value = true }]) },
    ])
    ui = concat([
      { target = { kind = "HTTPRoute", name = "ui-grafana", namespace = "monitoring" }, patch = yamlencode(concat(
        [{ op = "replace", path = "/spec/hostnames/0", value = local.hostnames.grafana }],
        local.f.edge.grafana_public ? [] : [{ op = "replace", path = "/spec/parentRefs/0/name", value = "serverless2-internal" }]
      )) },
    ])
    # node-config bootstraps the mirror configuration itself: its init image stays a public reference
    node-config = []
    scheduling  = []
  }
}
