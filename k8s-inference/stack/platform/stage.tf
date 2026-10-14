# Per-cluster Helm values of the upstream releases and the release list of this cluster. The per-cluster view of
# the platform itself (hostnames, images, the values of charts/platform) is computed by stack/config
# (`platform_values`) so that tools/check.sh renders the same objects without a cloud; here only what needs the
# upstream charts' value shapes or the cloud state's secrets.
locals {
  repo   = abspath("${path.module}/../..")
  id     = var.target
  region = local.cluster.region
  name   = local.f.name

  host               = local.host_of[local.id]
  hostnames          = local.hostnames_of[local.id]
  fleet_ca_here      = module.config.fleet_ca_here[local.id]
  certificate_issuer = module.config.certificate_issuer[local.id]
  litellm_url        = local.role.control ? local.litellm_url_in_cluster : "https://litellm.${local.control_host}"
  # The fleet document for the charts (stack/config/locals.tf) with, per pool, the node group the cloud stage created
  # for it: the dispatcher raises an InfiniBand pool's floor through the Nebius API while a multi-node run waits for
  # its nodes (docs/SCHEDULING.md "InfiniBand pools grow on demand"); null for a pool the cloud stage has not applied
  # yet (the dispatcher leaves such a pool alone).
  fleet_doc = merge(local.chart_fleet, {
    regions = { for rn, r in local.chart_fleet.regions : rn => merge(r, {
      pools = { for pn, p in r.pools : pn => merge(p, { node_group_id = try(local.cloud.clusters[r.id].gpu_node_group_ids[pn], null) }) }
    }) }
  })

  # Releases and charts/platform components of this cluster (clusters/common/apps, stack/config `apps_by_cluster`).
  apps          = module.config.apps_by_cluster[local.id]
  helm_apps     = { for n, a in local.apps : n => a if a.kind == "helm" }
  manifest_apps = { for n, a in local.apps : n => a if a.kind == "manifests" }

  # Per-cluster Helm values (merged over clusters/common/values/<component>.yaml).
  gpu_prices            = [for pn, p in try(local.clusters[local.id].pools, {}) : local.f.prices[p.platform]]
  gpu_driver_profiles   = yamldecode(file("${local.repo}/clusters/common/values/gpu-operator.yaml")).nebius.nvidiaDriverCRDPatch.profiles
  gpu_platforms_missing = sort(distinct([for pn, p in try(local.clusters[local.id].pools, {}) : p.platform if !contains([for pr in local.gpu_driver_profiles : pr.name], p.platform)]))
  # the listed profile whose name is the longest prefix of a missing platform (gpu-b200-sxm-a-x -> gpu-b200-sxm-a, not gpu-b200-sxm)
  gpu_driver_sibling = { for platform in local.gpu_platforms_missing : platform => try(
  substr(reverse(sort([for pr in local.gpu_driver_profiles : format("%03d:%s", length(pr.name), pr.name) if startswith(platform, pr.name)]))[0], 4, -1), null) }
  values_override = {
    kueue = local.role.manager ? yamldecode(file("${local.repo}/clusters/control/values/kueue.yaml")) : {}
    # the driver the operator installs (terraform.tfvars gpu_operator); the rest is clusters/common/values/gpu-operator.yaml
    gpu-operator = {
      driver = { version = local.f.gpu_operator.driver_version, kernelModuleType = local.f.gpu_operator.kernel_module }
      gds    = { enabled = local.f.gpu_operator.gds }
      # one NVIDIADriver per platform: the chart's list (clusters/common/values/gpu-operator.yaml) plus a profile for every
      # platform of this cluster's pools the list lacks, with the manager settings of its longest-prefix sibling
      # (gpu-rtx6000-a -> gpu-rtx6000). Helm replaces lists, so the whole list is given here.
      nebius = { nvidiaDriverCRDPatch = { profiles = concat(local.gpu_driver_profiles, [for platform in local.gpu_platforms_missing : {
        name         = platform
        nodeSelector = { "node.kubernetes.io/instance-type" = platform }
        managerEnv   = try([for pr in local.gpu_driver_profiles : pr.managerEnv if pr.name == local.gpu_driver_sibling[platform]][0], [])
      }]) } }
    }
    # with the operator the driver lives in a container and the `nvidia` RuntimeClass (created by the operator) injects it;
    # a pod that only relies on the node's default runtime can be created before the toolkit has made nvidia the default
    # (the exporter then crash-loops on "Cannot init NVML library"), so the fleet's exporters name the class explicitly
    dcgm-exporter      = local.f.gpu_operator.enabled ? { runtimeClassName = "nvidia" } : {}
    dcgm-exporter-l40s = local.f.gpu_operator.enabled ? { runtimeClassName = "nvidia" } : {}
    # warm spare nodes per pool (docs/FLEET.md "Warm spare nodes"): one pause Deployment per pool with warm_nodes > 0,
    # every pod sized to a whole node (the preset's GPU count) so one spare is one node
    overprovisioner = { deployments = [for pn, p in try(local.clusters[local.id].pools, {}) : {
      name         = pn
      replicaCount = p.warm_nodes
      labels       = { "app.kubernetes.io/part-of" = "serverless2", "serverless2.nebius/pool" = pn }
      nodeSelector = { "serverless2.nebius/pool" = pn }
      tolerations  = [{ key = "nvidia.com/gpu", operator = "Exists", effect = "NoSchedule" }]
      resources = {
        requests = { cpu = "5m", memory = "8Mi", "nvidia.com/gpu" = tostring(tonumber(regex("^([0-9]+)gpu", p.preset)[0])) }
        limits   = { cpu = "10m", memory = "16Mi", "nvidia.com/gpu" = tostring(tonumber(regex("^([0-9]+)gpu", p.preset)[0])) }
      }
    } if p.warm_nodes > 0] }
    # Spegel mirrors the logical host and falls through to the cluster's cache (clusters/common/values/spegel.yaml);
    # without the cache it mirrors the public registries between the nodes only
    spegel = local.images_cache ? {} : { spegel = {
      mirroredRegistries      = [for alias, host in module.config.registry_prefix : "https://${host}" if alias != "nebius"]
      additionalMirrorTargets = []
    } }
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
      db           = { useExisting = true, deployStandalone = false, endpoint = "${local.cloud.database.host}:${local.cloud.database.port}", database = "litellm", url = "postgresql://$(DATABASE_USERNAME):$(DATABASE_PASSWORD)@$(DATABASE_HOST)/$(DATABASE_NAME)?sslmode=require&sslaccept=strict&sslcert=${local.msp_ca_path}", secret = { name = "litellm-db", usernameKey = "username", passwordKey = "password" } }
      volumes      = local.trust_volumes
      volumeMounts = local.trust_mounts
      # the model endpoints behind the regional gateways carry fleet-CA certificates; no public HTTPS is needed
      envVars = { SSL_CERT_FILE = local.trust_bundle_path, LITELLM_LOCAL_MODEL_COST_MAP = "True" }
      # a changed trust bundle restarts LiteLLM (its HTTPS clients load the CAs once)
      podAnnotations = { "serverless2.nebius/trust-sha" = substr(sha256(join("", values(local.trust_data))), 0, 16) }
    }
  }
  fleet_values = {
    cluster = local.id
    fleet   = local.fleet_doc
    prepull = { enabled = local.role.worker, images = try(local.prepull_by_cluster[local.id], []) }
  }
}
