# The one input of the solution: `fleet` (terraform.tfvars at the repository root). Every stage
# (stack/cloud, stack/platform, stack/models) passes it to this module (`config.tf` in each stage);
# locals.tf derives the per-cluster view and outputs.tf hands it back. Nothing else is edited to bring
# a fleet up. terraform.tfvars.example documents every key; the validations below reject the mistakes
# the stages cannot recover from.

variable "fleet" {
  description = "The inference fleet: control plane, GPU regions with their pools, images, prices, edge, tenants and models."
  type = object({
    schema_version   = optional(number, 1)
    trust_bundle_pem = optional(string) # public CA bundle, including private gateway/database roots when needed
    # Prefix of every cloud resource (clusters, service accounts, buckets, registry): <name>-<cluster id>-...
    name               = string
    kubernetes_version = optional(string, "1.35")
    labels             = optional(map(string), {})
    # Weights filesystems and tenant buckets refuse `terraform destroy` while true (README "Destroying").
    protect_data = optional(bool, true)
    # Per node, kept free of Kueue quota for daemonsets and the kubelet.
    node_reserve = optional(object({
      cpu        = optional(number, 2)
      memory_gib = optional(number, 20)
    }), {})
    # Order of capacity types inside a GPU class in every preference queue (reservations first).
    capacity_order = optional(list(string), ["reserved", "on_demand", "spot"])
    # The region whose project holds the registry, the backups bucket and the tenants' primary buckets.
    # Default: the control plane's region when it is also a GPU region, else the first region (sorted).
    hub_region = optional(string)

    # Terraform state: one Object Storage bucket in the hub project (stack/bootstrap/state-bucket.sh creates
    # it); stack.sh passes bucket/endpoint to `terraform init`. Credentials: AWS_ACCESS_KEY_ID/SECRET env.
    state = optional(object({
      bucket   = optional(string) # default <name>-tfstate
      region   = optional(string) # default: the hub region
      endpoint = optional(string) # default https://storage.<region>.nebius.cloud
    }), {})

    # Control plane: API, LiteLLM, UI, Kueue MultiKueue manager, cost dispatcher, central Grafana, the managed database.
    # dedicated = true (default): its own CPU-only cluster in `project_id`/`subnet_id` (region `region`), the
    # fleet can span N regions. dedicated = false: single-cluster mode, the control-plane services run on
    # the hub region's system pool, jobs are admitted locally (no MultiKueue); exactly one region is
    # allowed; switching to dedicated later creates the control cluster and re-points the tenants.
    control_plane = object({
      dedicated  = optional(bool, true)
      project_id = string
      region     = string
      subnet_id  = string
      system_pool = optional(object({
        platform   = optional(string, "cpu-d3")
        preset     = optional(string, "8vcpu-32gb")
        node_count = optional(number, 2)
      }), {})
      # CIDRs allowed to reach the public Kubernetes API endpoint. Empty = open. Terraform itself must be
      # able to reach it from where you run it (your NAT egress, VPN).
      allowed_cidrs        = optional(list(string), [])
      image_cache_size_gib = optional(number, 100)
      # The fleet database: Nebius Managed Service for PostgreSQL in this project and region, databases
      # `platform` (model definitions, the API) and `litellm` (keys, budgets, spend). Backups are the
      # service's (retention below). The service exists in eu-north1, eu-west1, eu-west2, me-west1,
      # us-central1 and uk-south1 (docs.nebius.com/postgresql): the control plane must be in one of them.
      database = optional(object({
        platform            = optional(string, "cpu-e2")
        preset              = optional(string, "2vcpu-8gb")
        disk_gib            = optional(number, 64)
        hosts               = optional(number, 1)
        backup_retention    = optional(string, "14d")
        backup_window_start = optional(string, "03:00:00")
      }), {})
    })

    # GPU regions (Kueue workers). Key = Nebius region name (eu-north1, eu-south1, ...). `id` is the short
    # cluster id used in names, catalog `deployments.<id>`, queue and state keys (default: the region name).
    regions = map(object({
      id         = optional(string)
      project_id = string
      subnet_id  = string
      system_pool = optional(object({
        platform   = optional(string, "cpu-d3")
        preset     = optional(string, "8vcpu-32gb")
        node_count = optional(number, 2) # 2: Nebius' cilium-operator keeps a replica Pending on a single node
      }), {})
      allowed_cidrs = optional(list(string), [])
      # Shared Nebius filesystem for model weights, mounted on every GPU node at /mnt/weights and exposed to
      # endpoints as the RWX claim models/weights-shared (catalog `weights.sharedFilesystem`).
      weights_filesystem = optional(object({
        enabled  = optional(bool, false)
        size_gib = optional(number, 1024)
        type     = optional(string, "NETWORK_SSD")
      }), {})
      image_cache_size_gib = optional(number) # default images.cache.size_gib
      # GPU node pools. capacity.type: spot (max_price caps the USD per GPU-hour, null follows the spot
      # price), on_demand, or reserved (reservation_ids = capacity block groups, policy STRICT; rolled with
      # zero surge because a full reservation cannot surge). min_nodes = the node floor (the first model occupies it; warm_nodes keeps
      # spares with no model); max_nodes = the autoscaler's ceiling. endpoint_floor_gpus = GPUs held by endpoints with a warm
      # floor (subtracted from the Kueue quota). local_nvme = the preset's host NVMe disks are attached
      # (docs/FLEET.md "Local NVMe" lists the platform/preset combinations that ship them; the apply fails
      # with "local_disks ... is invalid" on a preset without them): local_nvme_mode = kubelet-ephemeral (default) formats them as the
      # kubelet's ephemeral storage, so every emptyDir, image layer and `scratch: local-nvme` run volume
      # lands on NVMe, and the node gets the label serverless2.nebius/local-nvme=true; raw leaves the
      # devices unformatted for a workload that owns them. Boot disk: a node scaled from zero advertises
      # about 80% of it minus 32 GiB as ephemeral storage (never the NVMe); size it for the largest
      # emptyDir a pod requests, or keep min_nodes >= 1.
      pools = map(object({
        platform  = string
        preset    = string
        gpu_class = string
        capacity = optional(object({
          type            = optional(string, "on_demand")
          max_price       = optional(string)
          reservation_ids = optional(list(string), [])
        }), {})
        min_nodes = optional(number, 0)
        max_nodes = number
        # warm_nodes: spare nodes kept running WITHOUT a model on them (a placeholder pod of negative priority
        # holds each one; any endpoint or run pod preempts it and starts without an instance boot, the
        # placeholder then brings the next spare up through the autoscaler, within max_nodes). Costs the
        # node price while idle; min_nodes alone keeps a floor that the first model occupies.
        warm_nodes          = optional(number, 0)
        endpoint_floor_gpus = optional(number, 0)
        driver_preset       = optional(string, "cuda13.0")
        boot_disk_gib       = optional(number, 512)
        local_nvme          = optional(bool, false)
        local_nvme_mode     = optional(string, "kubelet-ephemeral")
        # interconnect = infiniband: the pool's nodes join one Nebius GPU cluster (nebius_compute_v1_gpu_cluster,
        # one per pool) on `infiniband_fabric`, so multi-node jobs run over InfiniBand; needs a full-node preset
        # the platform allows clustering for (8gpu-* on H100/H200/B200/B300, 4gpu-* on GB300; preflight checks
        # the platform's `allow_gpu_clustering`). Fabrics per region (docs.nebius.com/compute/clusters/gpu,
        # 2026-10): eu-north1 fabric-2/3/4/6, eu-north2-a; eu-west1 fabric-5; eu-west2 eu-west2-a; uk-south1
        # uk-south1-a; us-central1 us-central1-a/-b; us-north1 us-north1-a; me-west1 me-west1-a. Nodes get the
        # label serverless2.nebius/interconnect=infiniband and the pool's Kueue flavor carries it, so a job
        # class can require it. A reservation (capacity block) must be on the same fabric.
        interconnect      = optional(string, "none")
        infiniband_fabric = optional(string)
        labels            = optional(map(string), {})
      }))
      # Optional CPU-only pools (batch pre/post-processing without a GPU): label serverless2.nebius/pool,
      # taint serverless2.nebius/cpu-pool=present:NoSchedule, Kueue flavor like a GPU pool with 0 GPUs.
      cpu_pools = optional(map(object({
        platform      = string
        preset        = string
        min_nodes     = optional(number, 0)
        max_nodes     = number
        boot_disk_gib = optional(number, 128)
        labels        = optional(map(string), {})
      })), {})
    }))

    # Images: every image reference in the platform and the catalog names the logical host, which each node
    # resolves to its Spegel peers and the cluster's Zot pull-through cache. `source` is the registry the
    # platform images (services/*) are pushed to; default: the registry this solution creates in the hub
    # project (`stack.sh output cloud registry`). `upstreams`: third-party registries reachable as
    # <host>/<alias>/<path> (credentials for private ones come from the environment, see `secrets`).
    images = optional(object({
      host   = optional(string, "registry.serverless2.local")
      source = optional(string)
      cache = optional(object({
        node_port = optional(number, 30500)
        size_gib  = optional(number, 1024)
        keep_days = optional(number, 30)
      }), {})
      upstreams = optional(map(string), {
        nvcr   = "https://nvcr.io"
        ghcr   = "https://ghcr.io"
        docker = "https://registry-1.docker.io"
        quay   = "https://quay.io"
        k8s    = "https://registry.k8s.io"
      })
      # Tags of the platform images under <source>/serverless2/<component>:<tag> (tools/images.sh).
      versions = optional(object({
        api        = optional(string, "0.10.3")
        dispatcher = optional(string, "0.2.5")
        jobs       = optional(string, "0.1.10")
        ops        = optional(string, "0.1.11")
        ui         = optional(string, "0.5.1")
      }), {})
    }), {})

    # USD per GPU-hour by platform (list prices); the scheduler orders pools by these and the cost export
    # prices node time with them. Reservations have zero marginal cost. The default covers every current
    # Nebius GPU platform (nebius.com/prices, 2026-10); an entry given here replaces the default one.
    prices = optional(map(object({
      on_demand = number
      spot      = number
      })), {
      gpu-h100-sxm   = { on_demand = 4.50, spot = 0.79 } # H100 SXM (Intel host)
      gpu-h200-sxm   = { on_demand = 5.40, spot = 0.79 } # H200 SXM
      gpu-b200-sxm   = { on_demand = 7.50, spot = 0.99 } # B200 SXM
      gpu-b200-sxm-a = { on_demand = 8.50, spot = 0.99 } # B200 SXM, the me-west1 variant
      gpu-b300-sxm   = { on_demand = 9.50, spot = 0.99 } # B300 SXM
      gpu-gb300      = { on_demand = 9.50, spot = 0.99 } # GB300 NVL (ARM host)
      gpu-l40s-a     = { on_demand = 1.55, spot = 0.74 } # L40S (Intel host)
      gpu-l40s-d     = { on_demand = 1.82, spot = 0.90 } # L40S (AMD host)
      gpu-rtx6000-a  = { on_demand = 1.80, spot = 0.95 } # RTX PRO 6000 Blackwell
    })
    reserved_marginal_price = optional(number, 0)

    # Public edge. mode = public: one static public IP per cluster gateway (kept across Service
    # re-creation); mode = internal: the gateway gets a private load balancer (VPN/peering access only, no
    # ACME). domain = null: hostnames are <service>.<gateway ip>.sslip.io with Let's Encrypt HTTP-01
    # certificates (no DNS needed); domain = "inference.example.com": hostnames <service>.<cluster id>.<domain>,
    # you create the A records from `stack.sh output` and the same ACME path issues the certificates once they
    # resolve. ip_certificate: additionally issue a Let's Encrypt IP-address certificate (ACME profile
    # `shortlived`, 6 days, renewed by cert-manager) so https://<ip> is browser-trusted without any name.
    # source_cidrs: who may reach the public listeners (empty = anyone; the API still needs a key).
    edge = optional(object({
      mode              = optional(string, "public")
      domain            = optional(string)
      private_ca_secret = optional(string) # cert-manager namespace; tls.crt + tls.key
      source_cidrs      = optional(list(string), [])
      ip_certificate    = optional(bool, false)
      acme = optional(object({
        email   = optional(string, "")
        staging = optional(bool, false)
      }), {})
      api_rate_limit_per_minute = optional(number, 600)
      grafana_public            = optional(bool, true)
    }), {})

    observability = optional(object({
      prometheus_retention_days = optional(number, 30)
      loki_retention_days       = optional(number, 30)
      # Alertmanager webhook for critical/warning alerts (Slack/Teams/PagerDuty webhook URL); null = the
      # in-cluster alert sink only.
      alert_webhook_url = optional(string)
      cost_export       = optional(bool, true)
    }), {})

    # Secret values never go into this file: they are read from the environment at apply time, by the
    # variable NAMES given here (unset = the feature stays off).
    secrets = optional(object({
      ngc_api_key_env               = optional(string, "NGC_API_KEY")          # NVIDIA NGC key (NIM images through the cache)
      hf_token_env                  = optional(string, "HF_TOKEN")             # Hugging Face token for gated weights (seed-weights, runtimes)
      registry_credentials_env      = optional(string, "REGISTRY_CREDENTIALS") # JSON {"<host>": {"username": "...", "password": "..."}} for other private upstreams
      mysterybox_credentials_secret = optional(string)                         # pre-provisioned subject-credentials.json, per cluster
      workload_secrets = optional(map(object({
        namespace        = string
        name             = string
        secret_id        = string
        version          = optional(string)
        create_namespace = optional(bool, false)
      })), {})
    }), {})

    # Tenants: namespace tenant-<name> on every cluster, a bucket + identity per region, LiteLLM API keys.
    # gpu_quota: GPUs the tenant may hold at once across the fleet (null = no cap: it shares the fleet
    # queues); fair_share_weight orders tenants when the fleet is full (Kueue weighted fair sharing).
    # dedicated_pools: pool names (as in regions.*.pools) only this tenant may use (its queues carry only those
    # flavors; the shared queues exclude them). allowed_images: image prefixes a tenant's container-run may
    # use (empty = any image through the cache).
    tenants = optional(map(object({
      regions           = optional(list(string)) # default: all regions
      lifecycle_days    = optional(number, 90)   # run outputs under operations/ expire after N days (0 = keep)
      gpu_quota         = optional(number)
      fair_share_weight = optional(number, 1)
      dedicated_pools   = optional(list(string), [])
      allowed_images    = optional(list(string), [])
      keys = optional(map(object({
        budget_usd = optional(number, 10)
        models     = optional(list(string), []) # allow-list (empty = all)
        routes     = optional(list(string), [])
        admin      = optional(bool, false)
      })), { default = {} })
    })), {})

    # Models are NOT defined here. The platform ships its generic classes (catalog/models: hello-run,
    # container-run, distributed-run) on every GPU region; every model is a container defined at runtime
    # through the API (`POST /v1/models`, the console's "New model" form; README "Deploy a model") and
    # stored in the fleet database.

    # After the models stage: a probe Job on the control cluster runs `model` (a built-in run class) and,
    # when `example_endpoint` is true, creates the example endpoint `llm-example` (a stock vLLM container
    # with a small model, on the fleet's first GPU class) through the API with the first admin key and calls
    # it; a failure fails the apply. The example stays as the first model of the console (delete it there).
    acceptance = optional(object({
      probe            = optional(bool, true)
      model            = optional(string, "hello-run")
      example_endpoint = optional(bool, true) # needs a tenant key with admin = true
    }), {})

  })

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,22}$", var.fleet.name))
    error_message = "fleet.name: lowercase letters, digits and dashes, 2-23 characters (it prefixes cloud resource names)."
  }
  validation {
    condition     = length(var.fleet.regions) >= 1
    error_message = "fleet.regions: at least one GPU region."
  }
  validation {
    # Let's Encrypt refuses accounts whose contact is example.com/.test/.invalid (400 invalidContact), and then
    # no certificate is ever issued: port 443 stays dark on every cluster (measured on a fresh fleet, 2026-10-08).
    condition     = var.fleet.edge.mode != "public" || can(regex("^[^@[:space:]]+@[^@[:space:]]+\\.[^@[:space:]]+$", var.fleet.edge.acme.email)) && !can(regex("@(example\\.(com|net|org)|.*\\.(test|invalid|localhost))$", var.fleet.edge.acme.email))
    error_message = "edge.acme.email: a real mailbox for the Let's Encrypt account (renewal and incident mail); example.com, .test and .invalid are refused by the ACME server and leave the gateway without a certificate."
  }
  validation {
    condition     = !var.fleet.acceptance.example_endpoint || !var.fleet.acceptance.probe || anytrue(flatten([for tn, t in var.fleet.tenants : [for kn, k in t.keys : k.admin]]))
    error_message = "acceptance.example_endpoint defines the example model through the API, which needs a tenant key with admin = true (or set example_endpoint = false)."
  }
  validation {
    condition     = contains(["eu-north1", "eu-west1", "eu-west2", "me-west1", "us-central1", "uk-south1"], var.fleet.control_plane.region)
    error_message = "control_plane.region: the fleet database is Nebius Managed PostgreSQL, which exists in eu-north1, eu-west1, eu-west2, me-west1, us-central1 and uk-south1 only (docs.nebius.com/postgresql)."
  }
  validation {
    condition     = var.fleet.control_plane.dedicated || length(var.fleet.regions) == 1
    error_message = "control_plane.dedicated = false is the single-cluster mode: exactly one region. Multi-region needs the dedicated control cluster (Kueue's MultiKueue manager cannot be its own worker)."
  }
  validation {
    condition = alltrue([for rn, r in var.fleet.regions : alltrue([for pn, p in r.pools :
      contains(["spot", "on_demand", "reserved"], p.capacity.type)
      && (p.capacity.type != "reserved" || length(p.capacity.reservation_ids) > 0)
      && (p.capacity.type == "reserved" || length(p.capacity.reservation_ids) == 0)
      && (p.capacity.type == "spot" || p.capacity.max_price == null)
      && (p.capacity.max_price == null || try(tonumber(p.capacity.max_price) > 0, false))
      && p.max_nodes >= p.min_nodes && p.min_nodes >= 0
    ])])
    error_message = "pools: capacity.type is spot | on_demand | reserved; reserved needs reservation_ids (and only reserved may set them); max_price (USD per GPU-hour, > 0) only on spot: with it the pool is capped by a pricing policy, without it the pool follows the spot price; 0 <= min_nodes <= max_nodes."
  }
  validation {
    condition     = alltrue([for rn, r in var.fleet.regions : alltrue([for pn, p in r.pools : can(regex("^[0-9]+gpu-[0-9]+vcpu-[0-9]+gb$", p.preset))])])
    error_message = "pools: preset must look like <n>gpu-<n>vcpu-<n>gb (the Kueue quota is derived from it)."
  }
  validation {
    condition     = alltrue([for rn, r in var.fleet.regions : alltrue([for pn, p in r.pools : contains(["kubelet-ephemeral", "raw"], p.local_nvme_mode)])])
    error_message = "pools: local_nvme_mode is kubelet-ephemeral | raw."
  }
  validation {
    condition = alltrue([for rn, r in var.fleet.regions : alltrue([for pn, p in r.pools :
      contains(["none", "infiniband"], p.interconnect)
      && (p.interconnect != "infiniband" || (p.infiniband_fabric != null && can(regex("^(8gpu-|4gpu-)", p.preset))))
      && (p.interconnect == "infiniband" || p.infiniband_fabric == null)
    ])])
    error_message = "pools: interconnect is none | infiniband; infiniband needs infiniband_fabric (the region's fabric name) and a full-node preset (8gpu-*, or 4gpu-* on gpu-gb300). The autoscaler never adds InfiniBand nodes (DRA): set min_nodes to the node count of the largest multi-node run before submitting it, 0 when idle (docs/FLEET.md)."
  }
  validation {
    condition     = alltrue([for rn, r in var.fleet.regions : alltrue([for pn, p in merge(r.pools, r.cpu_pools) : can(regex("^[a-z0-9-]+$", pn))]) && alltrue([for pn, p in r.pools : can(regex("^[a-z0-9-]+$", p.gpu_class))])])
    error_message = "pools: pool names and gpu_class are DNS labels (lowercase, digits, dashes); the class names the queue prefer-<class>."
  }
  validation {
    condition     = alltrue([for rn, r in var.fleet.regions : can(regex("^[a-z][a-z0-9-]{0,15}$", coalesce(r.id, rn)))])
    error_message = "regions: id (or the region name) must be a short DNS label (max 16 chars)."
  }
  validation {
    condition     = length(distinct([for rn, r in var.fleet.regions : coalesce(r.id, rn)])) == length(var.fleet.regions) && !contains([for rn, r in var.fleet.regions : coalesce(r.id, rn)], "control")
    error_message = "regions: cluster ids must be unique and may not be `control` (reserved for the control cluster)."
  }
  validation {
    condition     = var.fleet.hub_region == null ? true : contains(keys(var.fleet.regions), var.fleet.hub_region)
    error_message = "hub_region must be one of the regions."
  }
  validation {
    condition     = alltrue([for rn, r in var.fleet.regions : alltrue([for pn, p in r.pools : p.warm_nodes >= 0 && p.warm_nodes <= p.max_nodes && (p.warm_nodes == 0 || p.interconnect != "infiniband")])])
    error_message = "pools: warm_nodes is between 0 and max_nodes, and 0 on an InfiniBand pool (the autoscaler never adds InfiniBand nodes; use min_nodes there)."
  }
  validation {
    condition     = alltrue([for rn, r in var.fleet.regions : alltrue([for pn, p in r.pools : contains(keys(var.fleet.prices), p.platform)])])
    error_message = "prices: every GPU pool platform needs an entry (USD per GPU-hour, on_demand and spot)."
  }
  validation {
    condition     = alltrue([for tn, t in var.fleet.tenants : can(regex("^[a-z][a-z0-9-]{0,30}$", tn)) && alltrue([for r in coalesce(t.regions, []) : contains(keys(var.fleet.regions), r)])])
    error_message = "tenants: names are DNS labels; tenant regions must be fleet regions."
  }
  validation {
    condition     = contains(["public", "internal"], var.fleet.edge.mode) && (var.fleet.edge.domain == null || can(regex("^[a-z0-9.-]+$", var.fleet.edge.domain)))
    error_message = "edge.mode is public | internal; edge.domain is a DNS name (lowercase)."
  }

  validation {
    condition     = var.fleet.edge.mode != "internal" || (var.fleet.edge.domain != null && var.fleet.edge.private_ca_secret != null && var.fleet.trust_bundle_pem != null)
    error_message = "Internal gateways require edge.domain, edge.private_ca_secret and trust_bundle_pem; public ACME cannot issue private gateway certificates."
  }

  validation {
    condition     = length(var.fleet.secrets.workload_secrets) == 0 || var.fleet.secrets.mysterybox_credentials_secret != null
    error_message = "workload_secrets requires a pre-provisioned MysteryBox credentials Secret in each target namespace."
  }

  validation {
    condition = alltrue([for k, w in var.fleet.secrets.workload_secrets :
    !w.create_namespace || !contains(["ops", "monitoring", "api", "models", "registry", "spegel", "kueue-system", "knative-serving", "litellm", "ui-app", "envoy-gateway-system", "cert-manager"], w.namespace)])
    error_message = "workload_secrets: create_namespace is only for a namespace of your own; the platform's namespaces already exist."
  }
  validation {
    condition     = !var.fleet.edge.ip_certificate || var.fleet.edge.mode == "public"
    error_message = "edge.ip_certificate needs edge.mode = public."
  }
}
