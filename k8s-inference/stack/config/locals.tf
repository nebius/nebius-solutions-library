# Derived view of var.fleet shared by every stage (through the module outputs). Nothing here talks to a provider.
locals {
  f = var.fleet

  hub_region = coalesce(local.f.hub_region,
  contains(keys(local.f.regions), local.f.control_plane.region) ? local.f.control_plane.region : sort(keys(local.f.regions))[0])
  hub_project = local.f.regions[local.hub_region].project_id
  dedicated   = local.f.control_plane.dedicated

  # GPU regions keyed by cluster id.
  region_clusters = { for rn, r in local.f.regions : coalesce(r.id, rn) => merge(r, {
    id     = coalesce(r.id, rn)
    region = rn
    role   = "worker"
  }) }
  hub_id = coalesce(local.f.regions[local.hub_region].id, local.hub_region)

  # The control cluster (dedicated mode) is a cluster without GPU pools in the control plane's project.
  control_cluster = local.dedicated ? { control = {
    id                   = "control"
    region               = local.f.control_plane.region
    project_id           = local.f.control_plane.project_id
    subnet_id            = local.f.control_plane.subnet_id
    system_pool          = local.f.control_plane.system_pool
    allowed_cidrs        = local.f.control_plane.allowed_cidrs
    service_cidrs        = local.f.control_plane.service_cidrs
    weights_filesystem   = { enabled = false, size_gib = 0, type = "NETWORK_SSD", mount_on_system = false }
    image_cache_size_gib = local.f.control_plane.image_cache_size_gib
    pools                = {}
    role                 = "control"
  } } : {}

  clusters = merge(local.region_clusters, local.control_cluster)

  # Which cluster runs what. manager = Kueue MultiKueue manager + cost dispatcher (dedicated mode only).
  control_id  = local.dedicated ? "control" : local.hub_id
  manager_id  = local.dedicated ? "control" : null
  cluster_ids = sort(keys(local.clusters))
  roles = { for id, c in local.clusters : id => {
    control = id == local.control_id
    manager = local.dedicated && id == "control"
    worker  = c.role == "worker"
  } }

  projects = distinct([for id, c in local.clusters : c.project_id])

  state_bucket   = coalesce(local.f.state.bucket, "${local.f.name}-tfstate")
  state_region   = coalesce(local.f.state.region, local.hub_region)
  state_endpoint = coalesce(local.f.state.endpoint, "https://storage.${local.state_region}.nebius.cloud")

  images_host = local.f.images.host
  # Prices in the shape the charts read.
  chart_prices = merge({ for p, v in local.f.prices : p => { on_demand = v.on_demand, spot = v.spot } }, { reserved_marginal = local.f.reserved_marginal_price })

  # The fleet document the charts read (charts/fleet, charts/tenant, the API's fleet ConfigMap): the tfvars in
  # the chart's own shape. `images.source` is the registry the cloud stage created (`cloud_facts` below: the cloud
  # state in the platform stage, a placeholder in tools/check.sh, which renders the charts without any cloud access).
  chart_fleet = {
    name   = local.f.name
    labels = local.f.labels
    node_reserve = {
      cpu                = local.f.node_reserve.cpu
      memory_gib         = local.f.node_reserve.memory_gib
      ephemeral_fraction = local.f.node_reserve.ephemeral_fraction
      ephemeral_gib      = local.f.node_reserve.ephemeral_gib
    }
    system_boot_disk_gib = 128 # stack/modules/cluster main.tf: the system nodes' boot disk is fixed
    capacity_order       = local.f.capacity_order
    images = {
      host      = local.images_host
      source    = local.images_source
      cache     = local.f.images.cache
      upstreams = local.f.images.upstreams
    }
    control = {
      id                   = coalesce(local.manager_id, "none")
      region               = local.f.control_plane.region
      project              = local.f.control_plane.project_id
      image_cache_size_gib = local.f.control_plane.image_cache_size_gib
    }
    regions = { for rn, r in local.f.regions : rn => {
      id                   = coalesce(r.id, rn)
      project              = r.project_id
      system_pool          = r.system_pool # the CPU nodes the `system` Kueue flavor budgets (charts/fleet)
      image_cache_size_gib = coalesce(r.image_cache_size_gib, local.f.images.cache.size_gib)
      # charts/tenant multinode.yaml renders the shared scratch claim (`checkpoints: shared`) only where the region has it
      weights_filesystem = { enabled = r.weights_filesystem.enabled, size_gib = r.weights_filesystem.size_gib, mount_on_system = r.weights_filesystem.mount_on_system }
      pools              = { for pn, p in r.pools : pn => merge(p, { capacity = { type = p.capacity.type, max_price = p.capacity.max_price } }) }
      # CPU-only pools (charts/fleet: one flavor each after `system`, quota from max_nodes x preset; docs/FLEET.md "CPU pools")
      cpu_pools = { for pn, p in r.cpu_pools : pn => { platform = p.platform, preset = p.preset, min_nodes = p.min_nodes, max_nodes = p.max_nodes, boot_disk_gib = p.boot_disk_gib, shared_filesystem = p.shared_filesystem } }
    } }
    prices = local.chart_prices
    # Kueue queues per tenant (charts/fleet): the fair-share weight, the per-pool GPU cap and the pools only this tenant
    # may use (docs/FLEET.md "Tenants")
    tenants = { for tn, t in local.f.tenants : tn => { gpu_quota = t.gpu_quota, fair_share_weight = t.fair_share_weight, dedicated_pools = t.dedicated_pools, assets = t.assets } }
    assets  = local.f.assets
  }

  # GPU classes of the fleet -> scheduling profiles prefer-<class> (plus default).
  gpu_classes = sort(distinct(flatten([for id, c in local.region_clusters : [for pn, p in c.pools : p.gpu_class]])))

  # Plan the wrapper reads (stack.sh evaluates `local.plan` with `terraform console`).
  plan = {
    name       = local.f.name
    hub_region = local.hub_region
    hub_id     = local.hub_id
    control_id = local.control_id
    manager_id = local.manager_id
    dedicated  = local.dedicated
    clusters   = { for id, c in local.clusters : id => { region = c.region, project_id = c.project_id, roles = local.roles[id] } }
    # apply order: the workers first (they publish the identities the manager consumes), then the control cluster;
    # the tenant buckets every cluster needs are cloud-stage resources, so no worker depends on another
    order = concat([for id in local.cluster_ids : id if !local.roles[id].control], [for id in local.cluster_ids : id if local.roles[id].control])
    state = { bucket = local.state_bucket, region = local.state_region, endpoint = local.state_endpoint }
    # the environment variables that carry secret inputs (terraform.tfvars `secrets`), by platform-stage variable name
    secrets = {
      ngc_api_key          = local.f.secrets.ngc_api_key_env
      hf_token             = local.f.secrets.hf_token_env
      registry_credentials = local.f.secrets.registry_credentials_env
      # the image cache's static key: a required input of every cluster's platform stage while the cache is on (the
      # same key everywhere: one Secret value per fleet, never issued by a plan); nothing without the cache
      registry_key = local.images_cache ? local.f.secrets.registry_key_env : ""
    }
  }
}

# ---------------------------------------------------------------------------
# Built-in classes: the bundled entries (catalog/models/*.yaml: hello-run, container-run, distributed-run),
# deployed on every GPU region. Models themselves are defined at runtime through the API and live in the
# fleet database (services/api/models.py); nothing model-specific is in the tfvars.
locals {
  catalog_dir     = "${path.module}/../../catalog/models"
  bundled_catalog = { for f in fileset(local.catalog_dir, "*.yaml") : yamldecode(file("${local.catalog_dir}/${f}")).id => yamldecode(file("${local.catalog_dir}/${f}")) }
  catalog_enabled = local.bundled_catalog
  catalog_raw = { for id, e in local.bundled_catalog : id => merge(e, {
    deployments = { for cid, c in local.region_clusters : cid => try(e.deployments[cid], try(e.deployments["hub"], {})) }
  }) }
  # gpu.classes are filtered to the classes the fleet's pools declare (order kept): the API names the Kueue
  # queue after the first class, and that queue only exists for fleet classes. An entry left without a class
  # (none of its classes is in the fleet) keeps the bundled list and will not be admitted: preflight warns.
  catalog = { for id, e in local.catalog_raw : id => merge(e, {
    deployments = { for k, d in try(e.deployments, {}) : (k == "hub" ? local.hub_id : k) => d if contains(keys(local.clusters), k == "hub" ? local.hub_id : k) }
    }, length([for c in try(e.gpu.classes, []) : c if contains(local.gpu_classes, c)]) > 0 ? {
    gpu = merge(e.gpu, { classes = [for c in e.gpu.classes : c if contains(local.gpu_classes, c)] })
    } : {},
    can(e.job.image) ? { job = merge(e.job, { image = local.direct_image[e.job.image] }) } : {},
    can(e.runtime.image) ? { runtime = merge(e.runtime, { image = local.direct_image[e.runtime.image] }) } : {},
  ) }
  catalog_without_fleet_class = [for id, e in local.catalog_raw : id if can(e.gpu.classes) && length([for c in try(e.gpu.classes, []) : c if contains(local.gpu_classes, c)]) == 0]
  # Per cluster: the entries that run or may run there.
  catalog_by_cluster = { for id in local.cluster_ids : id => { for mid, e in local.catalog : mid => e if contains(keys(e.deployments), id) } }
  # Endpoint entries (a runtime block) per worker cluster, run classes (mode run / async) are the rest.
  # Images to pre-pull per worker cluster (endpoints and run classes with a fixed image).
  prepull_by_cluster = { for id, es in local.catalog_by_cluster : id => [for mid, e in es : { name = mid, image = try(e.runtime.image, e.job.image) } if can(e.runtime.image) || (can(e.job.image) && !can(regex("{{", try(e.job.image, ""))))] }
}

# ---------------------------------------------------------------------------
# Per-cluster view of the platform: the gateway hostnames, the image references, the API's environment and the
# values of charts/platform (what the per-cluster JSON patches of the platform stage used to set). Everything here
# is a function of the tfvars and the cloud facts, so tools/check.sh renders the same objects without a cloud.
locals {
  # `try` rather than a conditional: the live outputs and the placeholders are objects of different shapes, which a
  # conditional refuses ("inconsistent conditional result types" on a live plan)
  cloud_facts = {
    clusters = try(var.cloud.clusters, local.cloud_placeholder.clusters)
    hub      = try(var.cloud.hub, local.cloud_placeholder.hub)
  }
  cloud_placeholder = {
    # TEST-NET-1 addresses and example ids: distinct per cluster, stable across renders (the golden set)
    clusters = { for i, id in local.cluster_ids : id => {
      id                 = id, region = local.clusters[id].region, project_id = local.clusters[id].project_id
      cluster_id         = "mk8scluster-${id}", gateway_ip = "192.0.2.${i + 1}", gateway_allocation_id = "allocation-${id}"
      gpu_node_group_ids = {}
    } }
    hub = {
      region                  = local.hub_region, id = local.hub_id, project_id = local.hub_project
      registry                = coalesce(local.f.images.source, "cr.${local.hub_region}.nebius.cloud/registry-example")
      backups_bucket          = "${local.f.name}-backups"
      backups_bucket_endpoint = "https://storage.${local.hub_region}.nebius.cloud"
      backups_bucket_region   = local.hub_region
      logs_buckets            = { for id in local.cluster_ids : id => "${local.f.name}-logs-${id}" }
    }
  }
  images_source = local.cloud_facts.hub.registry
  images_cache  = local.f.images.cache.enabled
  # Where an image reference points: with the cache, <host>/<alias>/<path> on the logical host every node resolves
  # to its cache; without it, the registry itself (the fleet registry for `nebius`, docker.io for Docker Hub, ...).
  upstream_host = { for alias, url in local.f.images.upstreams : alias => replace(trimprefix(url, "https://"), "registry-1.docker.io", "docker.io") }
  registry_prefix = merge(
    { for alias, host in local.upstream_host : alias => local.images_cache ? "${local.images_host}/${alias}" : host },
    { nebius = local.images_cache ? "${local.images_host}/nebius" : local.images_source },
  )
  image = { for c, t in local.f.images.versions : c => "${local.registry_prefix.nebius}/serverless2/${c}:${t}" }
  # The bundled classes name their images through the logical host; with the cache off they are rewritten to the
  # upstream once here (a reference without a known prefix, or a `{{image}}` parameter, stays as it is).
  logical_prefix = { for alias, p in local.registry_prefix : "${local.images_host}/${alias}/" => "${p}/" }
  direct_image = { for ref in distinct(compact(flatten([for id, e in local.catalog_raw : [try(e.job.image, ""), try(e.runtime.image, "")]]))) : ref =>
    local.images_cache ? ref : try([for lp, dp in local.logical_prefix : "${dp}${trimprefix(ref, lp)}" if startswith(ref, lp)][0], ref)
  }

  # Public hostnames: <service>.<gateway ip>.sslip.io, or <service>.<cluster id>.<domain>.
  host_of      = { for id, c in local.clusters : id => local.f.edge.domain == null ? "${local.cloud_facts.clusters[id].gateway_ip}.sslip.io" : "${id}.${local.f.edge.domain}" }
  control_host = local.host_of[local.control_id]
  # the API, the console and LiteLLM are the control cluster's; a GPU region's gateway carries Grafana and the endpoints
  hostnames_of = { for id, c in local.clusters : id => merge(
    { grafana = "grafana.${local.host_of[id]}" },
    local.roles[id].control ? { api = "api.${local.host_of[id]}", litellm = "litellm.${local.host_of[id]}" } : {}
  ) }
  api_host = "api.${local.control_host}"
  grafana_urls = join(",", concat(
    [for id in local.cluster_ids : "${local.clusters[id].region}=https://grafana.${local.host_of[id]}" if local.roles[id].worker],
    local.dedicated ? ["control=https://grafana.${local.control_host}"] : []
  ))
  litellm_url_in_cluster = "http://litellm.litellm.svc:4000"
  # a GPU region's gateway: the fleet CA (edge.regional_certificates) unless the cluster also carries the control
  # plane (single-cluster mode: its hostnames are public); the control cluster: ACME; internal mode: the private CA
  acme_issuer   = local.f.edge.acme.staging ? "letsencrypt-staging" : "letsencrypt"
  fleet_ca_here = { for id, r in local.roles : id => local.f.edge.mode != "internal" && local.f.edge.regional_certificates == "fleet-ca" && r.worker && !r.control }
  certificate_issuer = { for id, r in local.roles : id => (
    local.f.edge.mode == "internal" ? "${local.f.name}-private-ca" : (local.fleet_ca_here[id] ? "fleet-ca" : local.acme_issuer)
  ) }
  # the `models` certificates of the GPU regions are written by the control API: their issuer, not the control's
  models_issuer = local.f.edge.mode == "internal" ? "${local.f.name}-private-ca" : (local.f.edge.regional_certificates == "fleet-ca" && local.dedicated ? "fleet-ca" : local.acme_issuer)

  # The CA bundle ConfigMap `trust` (stack/platform/trust.tf) as the API and LiteLLM mount it; a private bundle
  # (`trust_bundle_pem`) replaces the image's system roots.
  trust_volumes = [{ name = "trust", configMap = { name = "trust" } }]
  trust_mounts = concat(
    [{ name = "trust", mountPath = "/etc/ssl/msp", readOnly = true }],
    local.f.trust_bundle_pem == null ? [] : [{ name = "trust", mountPath = "/etc/ssl/certs/ca-certificates.crt", subPath = "ca.pem", readOnly = true }],
  )

  # The API's environment (services/api/config.py), the control cluster only: the whole list, in this order (a reordered
  # env rolls the Deployment). Every HTTPS client of the API trusts the system roots plus every CA under /etc/ssl/msp.
  api_env = { for id, r in local.roles : id => [for e in concat([
    { name = "LITELLM_URL", value = local.litellm_url_in_cluster },
    { name = "LITELLM_MASTER_KEY", valueFrom = { secretKeyRef = { name = "litellm-master", key = "masterkey" } } },
    { name = "PUBLIC_API_URL", value = "https://${local.api_host}" },
    { name = "GRAFANA_URLS", value = local.grafana_urls },
    { name = "CATALOG_DIRS", value = "/etc/catalog" },
    { name = "RUNNER_IMAGE", value = local.image.jobs },
    { name = "HUB_REGION", value = local.hub_region },
    # the OIDC issuer of every GPU region's cluster (docs.nebius.com/kubernetes/networking/oidc-public-endpoint): what a
    # storage provider trusts for a run's projected workload-identity token (docs/JOBS.md "Execution options")
    { name = "REGION_OIDC_ISSUERS", value = join(",", [for cid in local.cluster_ids : "${local.clusters[cid].region}=https://oidc.mk8s.api.nebius.cloud/${local.cloud_facts.clusters[cid].cluster_id}" if local.roles[cid].worker]) },
    # The fleet database (services/api/db.py): the model definitions, keys and attempt records.
    { name = "DATABASE_URL", valueFrom = { secretKeyRef = { name = "database", key = "platform_url" } } },
    { name = "ROUTER_KEY", valueFrom = { secretKeyRef = { name = "router-key", key = "key" } } }, # the model groups' key (router-key.tf)
    { name = "IMAGES_HOST", value = local.images_cache ? local.images_host : "" },                # empty: the API leaves image references as written
    { name = "IMAGES_SOURCE", value = local.images_source },
    # live sessions (docs/API.md "Sessions"): off unless `sessions.enabled`
    { name = "ADMITTED_LEGACY", value = local.f.admitted_legacy ? "true" : "false" },
    { name = "SESSIONS_ENABLED", value = local.f.sessions.enabled ? "true" : "false" },
    { name = "SESSION_CLAIM_TTL_S", value = tostring(local.f.sessions.claim_ttl_s) },
    { name = "SESSION_ATTACH_WAIT_S", value = tostring(local.f.sessions.attach_wait_s) },
    { name = "SESSION_TOKEN_AUDIENCE", value = local.f.sessions.token_audience },
    # the gateway domain of every GPU region: the endpoints' hostnames (LiteLLM, sync calls for another region, async calls)
    { name = "ENDPOINT_DOMAINS", value = join(",", [for cid in local.cluster_ids : "${local.clusters[cid].region}=${local.host_of[cid]}" if local.roles[cid].worker]) },
    { name = "ACME_ISSUER", value = local.certificate_issuer[id] },
    { name = "MODELS_ISSUER", value = local.models_issuer },
    ], r.manager ? [
    { name = "REGION", value = "control" },
    { name = "REGION_KUBECONFIGS", value = join(",", [for cid in local.cluster_ids : "${local.clusters[cid].region}=/etc/kubeconfigs/${local.clusters[cid].region}" if local.roles[cid].worker]) },
    { name = "FLEET_MANAGER", value = "true" },
    { name = "S3_ENV_SECRET", value = "s3-fleet" },
    ] : [ # single-cluster mode: the one cluster is the control plane and the GPU region
    { name = "REGION", value = local.clusters[id].region },
    { name = "REGION_KUBECONFIGS", value = "" },
  ]) : e if r.control] }

  # The release inventory (clusters/common/apps/*.yaml) per cluster: Helm releases and the components of
  # charts/platform (kind: manifests), by placement. Releases that depend on the fleet definition: the GPU Operator
  # and, before it, the Network Operator replace the fleet's nvidia-device-plugin release when gpu_operator.enabled
  # (docs/FLEET.md "GPU drivers"). The Network Operator goes wherever the GPU Operator goes unless
  # gpu_operator.network_operator = false: the Nebius driver profiles of every SXM platform wait for MOFED on nodes
  # with a Mellanox NIC (a B300 node without InfiniBand waited forever without it).
  apps_dir = "${path.module}/../../clusters/common/apps"
  apps_all = { for f in fileset(local.apps_dir, "*.yaml") : trimsuffix(f, ".yaml") => yamldecode(file("${local.apps_dir}/${f}")) }
  # The image cache (docs/IMAGES.md): the Zot release and the containerd DaemonSet exist only with it; Spegel stays.
  app_enabled = {
    gpu-operator         = local.f.gpu_operator.enabled
    network-operator     = local.f.gpu_operator.enabled && coalesce(local.f.gpu_operator.network_operator, true)
    nvidia-device-plugin = !local.f.gpu_operator.enabled
    zot                  = local.images_cache
    node-config          = local.images_cache
  }
  apps_by_cluster = { for id, r in local.roles : id => { for n, a in local.apps_all : n => a if(
    (a.placement == "all" || (a.placement == "worker" && r.worker) || (a.placement == "control" && r.control))
    && lookup(local.app_enabled, n, true)
  ) } }
  # The identities of charts/platform are not apps: the worker's api-agent and MultiKueue ServiceAccounts (the
  # control cluster holds tokens of them) and the tenant ClusterRole, which the control cluster's API needs too.
  identity_components = { for id, r in local.roles : id => r.worker ? ["api-agent", "fleet-access", "tenant-clusterrole"] : ["tenant-clusterrole"] }

  # charts/platform values per cluster (charts/platform/values.yaml documents them).
  platform_values = { for id, r in local.roles : id => {
    cluster    = id
    role       = r
    components = merge({ for n, a in local.apps_by_cluster[id] : n => true if a.kind == "manifests" }, { for n in local.identity_components[id] : n => true })
    host       = local.host_of[id]
    hostnames  = local.hostnames_of[id]
    images     = { host = local.images_host, api = local.image.api, dispatcher = local.image.dispatcher, ops = local.image.ops }
    # the logical host resolves only on the nodes (containerd), not in the cluster network: Knative keeps such a
    # reference as written; nvcr.io needs credentials Knative does not have
    knative = { skipTagResolving = local.images_cache ? [local.images_host, "nvcr.io"] : ["nvcr.io"] }
    api     = { rateLimitPerMinute = local.f.edge.api_rate_limit_per_minute, env = local.api_env[id] }
    # a GPU region's gateway authorizes endpoint calls against the fleet's API by its public hostname (templates/edge):
    # validated against the system roots (ACME) in public mode, the trust bundle (`trust`, key ca.crt) in internal mode
    edge  = { controlApiHost = local.api_host, caConfigMap = local.f.edge.mode == "internal" ? "trust" : "" }
    trust = { volumes = local.trust_volumes, mounts = local.trust_mounts }
    gateway = {
      wildcardSecret = "${id}-wildcard-tls"
      serviceAnnotations = merge(
        local.cloud_facts.clusters[id].gateway_allocation_id == null ? {} : { "nebius.com/load-balancer-allocation-id" = local.cloud_facts.clusters[id].gateway_allocation_id },
        local.f.edge.mode == "internal" ? { "nebius.com/load-balancer-type" = "internal" } : {}
      )
      acme = { enabled = local.f.edge.mode != "internal", email = local.f.edge.acme.email }
    }
    observability = { costExport = {
      enabled  = local.f.observability.cost_export
      cluster  = "${local.f.name}-${id}"
      bucket   = local.cloud_facts.hub.backups_bucket
      endpoint = local.cloud_facts.hub.backups_bucket_endpoint
      region   = try(local.cloud_facts.hub.backups_bucket_region, local.hub_region)
    } }
    ops = {
      recoverStoppedNodes = r.worker ? { enabled = true, projectId = local.clusters[id].project_id, clusterId = local.cloud_facts.clusters[id].cluster_id } : { enabled = false, projectId = "project-example", clusterId = "mk8scluster-example" }
      rotateKeys          = false
    }
    grafanaRoute = { public = local.f.edge.grafana_public }
  } }
}
