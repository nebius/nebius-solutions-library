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
    weights_filesystem   = { enabled = false, size_gib = 0, type = "NETWORK_SSD" }
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
  # the chart's own shape. `images.source` is the registry the cloud stage created; the platform stage fills
  # it in from the cloud state, everything else is known from the tfvars alone (tools/check.sh renders the
  # charts from this document without any cloud access).
  chart_fleet = {
    name           = local.f.name
    labels         = local.f.labels
    node_reserve   = { cpu = local.f.node_reserve.cpu, memory_gib = local.f.node_reserve.memory_gib }
    capacity_order = local.f.capacity_order
    images = {
      host      = local.images_host
      source    = coalesce(local.f.images.source, "created-by-the-cloud-stage")
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
      image_cache_size_gib = coalesce(r.image_cache_size_gib, local.f.images.cache.size_gib)
      pools                = { for pn, p in r.pools : pn => merge(p, { capacity = { type = p.capacity.type, max_price = p.capacity.max_price } }) }
    } }
    prices = local.chart_prices
  }

  # GPU classes of the fleet -> scheduling profiles prefer-<class> (plus default).
  gpu_classes = sort(distinct(flatten([for id, c in local.region_clusters : [for pn, p in c.pools : p.gpu_class]])))

  # Plan the wrapper reads (stack.sh evaluates `local.plan` with `terraform console`).
  plan = {
    name       = local.f.name
    profile    = local.f.nebius_profile
    hub_region = local.hub_region
    hub_id     = local.hub_id
    control_id = local.control_id
    manager_id = local.manager_id
    dedicated  = local.dedicated
    clusters   = { for id, c in local.clusters : id => { region = c.region, project_id = c.project_id, roles = local.roles[id] } }
    # apply order: workers first (they publish the identities the manager consumes), then the control cluster
    order = concat([for id in local.cluster_ids : id if !local.roles[id].control], [for id in local.cluster_ids : id if local.roles[id].control])
    state = { bucket = local.state_bucket, region = local.state_region, endpoint = local.state_endpoint }
  }
}

# ---------------------------------------------------------------------------
# Model catalog: the bundled entries (catalog/models/*.yaml, filtered by models.enabled) merged with the
# tfvars entries (models.entries, HCL in the same schema). An entry whose id matches a bundled one extends it:
# its top-level keys replace the bundled ones (give only `deployments` to re-home a bundled class to your
# regions). `deployments.hub` is an alias for the hub cluster.
locals {
  catalog_dir     = "${path.module}/../../catalog/models"
  bundled_catalog = { for f in fileset(local.catalog_dir, "*.yaml") : yamldecode(file("${local.catalog_dir}/${f}")).id => yamldecode(file("${local.catalog_dir}/${f}")) }
  catalog_enabled = { for id, e in local.bundled_catalog : id => e if local.f.models.enabled == null || contains(local.f.models.enabled, id) }
  catalog_raw = merge(local.catalog_enabled, { for id, e in local.f.models.entries :
    coalesce(try(e.id, null), id) => merge(try(local.bundled_catalog[coalesce(try(e.id, null), id)], {}), e)
  })
  # gpu.classes are filtered to the classes the fleet's pools declare (order kept): the API names the Kueue
  # queue after the first class, and that queue only exists for fleet classes. An entry left without a class
  # (none of its classes is in the fleet) keeps the bundled list and will not be admitted: preflight warns.
  catalog = { for id, e in local.catalog_raw : id => merge(e, {
    deployments = { for k, d in try(e.deployments, {}) : (k == "hub" ? local.hub_id : k) => d if contains(keys(local.clusters), k == "hub" ? local.hub_id : k) }
    }, can(e.gpu.classes) && length([for c in e.gpu.classes : c if contains(local.gpu_classes, c)]) > 0 ? {
    gpu = merge(e.gpu, { classes = [for c in e.gpu.classes : c if contains(local.gpu_classes, c)] })
  } : {}) }
  catalog_without_fleet_class = [for id, e in local.catalog_raw : id if can(e.gpu.classes) && length([for c in e.gpu.classes : c if contains(local.gpu_classes, c)]) == 0]
  # Per cluster: the entries that run or may run there.
  catalog_by_cluster = { for id in local.cluster_ids : id => { for mid, e in local.catalog : mid => e if contains(keys(e.deployments), id) } }
  # Endpoint entries (a runtime block) per worker cluster, run classes (mode run / async) are the rest.
  endpoints_by_cluster = { for id, es in local.catalog_by_cluster : id => { for mid, e in es : mid => e if can(e.runtime) && try(e.deployments[id].paused, false) != true } }
  # Images to pre-pull per worker cluster (endpoints and run classes with a fixed image).
  prepull_by_cluster = { for id, es in local.catalog_by_cluster : id => [for mid, e in es : { name = mid, image = try(e.runtime.image, e.job.image) } if can(e.runtime.image) || (can(e.job.image) && !can(regex("{{", try(e.job.image, ""))))] }
}
