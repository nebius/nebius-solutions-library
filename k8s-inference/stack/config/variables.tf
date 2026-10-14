# The one input of the solution: `fleet` (terraform.tfvars at the repository root). Both stages
# (stack/cloud, stack/platform) pass it to this module (`config.tf` in each stage);
# locals.tf derives the per-cluster view and outputs.tf hands it back. Nothing else is edited to bring
# a fleet up. terraform.tfvars.example documents every key; the validations below reject the mistakes
# the stages cannot recover from.

variable "fleet" {
  description = "The inference fleet: control plane, GPU regions with their pools, images, prices, edge, tenants and models."
  type = object({
    schema_version   = optional(number, 1)
    trust_bundle_pem = optional(string)                                                            # public CA bundle, including private gateway/database roots when needed
    msp_ca_url       = optional(string, "https://storage.eu-north1.nebius.cloud/msp-certs/ca.pem") # the Nebius managed-services CA (docs.nebius.com/postgresql/databases/connect)
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
      # Ephemeral storage (the kubelet's node-local scratch on the boot disk) a pool flavor may admit per node: the
      # boot disk x `ephemeral_fraction` minus `ephemeral_gib`, under what the kubelet reports allocatable (measured:
      # a 128 GiB boot disk reports ~109.6 Gi, a 512 GiB one ~444.4 Gi; 0.85 x disk - 4 gives 104 / 431 Gi): the
      # system's reservation, the image cache and the writable layers keep their headroom. Nothing is provisioned.
      ephemeral_fraction = optional(number, 0.85)
      ephemeral_gib      = optional(number, 4)
    }), {})
    # Order of capacity types inside a GPU class in every preference queue (reservations first).
    capacity_order = optional(list(string), ["reserved", "on_demand", "spot"])
    # The region whose project holds the registry, the backups bucket and the tenants' primary buckets.
    # Default: the control plane's region when it is also a GPU region, else the first region (sorted).
    hub_region = optional(string)
    # GPU drivers on the nodes. Default: the Managed Kubernetes node image with the driver preset of each pool
    # (pools.<name>.driver_preset). enabled = true: the node groups are created WITHOUT a driver preset and the
    # NVIDIA GPU Operator (Nebius marketplace chart, clusters/common/apps/gpu-operator.yaml) installs the driver,
    # the container toolkit, the device plugin and GPU feature discovery on every GPU node; the fleet's own
    # nvidia-device-plugin release is then not installed. driver_version = the driver the operator installs
    # (an image tag of the marketplace driver image; 580.173.02 is what the cuda13.0 preset ships);
    # kernel_module = open | proprietary | auto (GPUDirect Storage needs the open modules); gds = GPUDirect
    # Storage (the nvidia-fs module next to the driver). network_operator: the NVIDIA Network Operator (DOCA
    # drivers for the nodes' Mellanox NICs) is installed before the GPU Operator on every worker cluster (the
    # Nebius driver profiles wait for it on any node with such a NIC: InfiniBand pools, B200/B300 nodes); false
    # leaves it out on a fleet whose nodes have no Mellanox NIC. docs/FLEET.md "GPU drivers".
    gpu_operator = optional(object({
      enabled          = optional(bool, false)
      driver_version   = optional(string, "580.173.02")
      kernel_module    = optional(string, "open")
      gds              = optional(bool, false)
      network_operator = optional(bool)
    }), {})

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
      allowed_cidrs = optional(list(string), [])
      # Kubernetes Service CIDRs of the cluster (kube_network.service_cidrs). Unset: Nebius allocates a /16 from the
      # project's private pool; when that pool is exhausted ("Unable to allocate private ipv4 cidr /16",
      # NotEnoughResources) name a smaller block, e.g. ["/20"]. Immutable after creation; existing clusters
      # keep what they have when this stays unset.
      service_cidrs        = optional(list(string))
      image_cache_size_gib = optional(number, 100)
      # The fleet database: Nebius Managed Service for PostgreSQL in this project and region, databases
      # `platform` (model definitions, the API) and `litellm` (keys, budgets, spend). Backups are the
      # service's (retention below). The control plane must be in a region where the service is available
      # (docs.nebius.com/postgresql); the provider refuses the cluster otherwise.
      database = optional(object({
        platform            = optional(string, "cpu-e2")
        preset              = optional(string, "2vcpu-8gb")
        disk_gib            = optional(number, 64)
        hosts               = optional(number, 1)
        backup_retention    = optional(string, "14d")
        backup_window_start = optional(string, "03:00:00")
      }), {})
    })

    # GPU regions (Kueue workers). Key = Nebius region name. `id` is the short
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
      service_cidrs = optional(list(string)) # as control_plane.service_cidrs
      # Shared Nebius filesystem for model weights, mounted on every GPU node at /mnt/weights and exposed to
      # endpoints as the RWX claim models/weights-shared (catalog `weights.sharedFilesystem`).
      weights_filesystem = optional(object({
        enabled  = optional(bool, false)
        size_gib = optional(number, 1024)
        type     = optional(string, "NETWORK_SSD")
        # CPU snapshot operator must retain storage access when GPUs scale to zero.
        # Off by default; enabling rolls only the system pool at cloud apply.
        mount_on_system = optional(bool, false)
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
        # the platform's `allow_gpu_clustering`). The fabric names per region are in docs.nebius.com/compute/clusters/gpu;
        # the provider refuses an unknown one. Nodes get the
        # label serverless2.nebius/interconnect=infiniband and the pool's Kueue flavor carries it, so a job
        # class can require it. A reservation (capacity block) must be on the same fabric.
        interconnect      = optional(string, "none")
        infiniband_fabric = optional(string)
        labels            = optional(map(string), {})
      }))
      # Optional CPU-only pools (batch pre/post-processing without a GPU): label serverless2.nebius/pool,
      # taint serverless2.nebius/cpu-pool=present:NoSchedule, Kueue flavor like a GPU pool with 0 GPUs.
      cpu_pools = optional(map(object({
        platform          = string
        preset            = string # <n>vcpu-<n>gb: the Kueue quota of the pool's flavor is max_nodes x preset minus the per-node reserve
        min_nodes         = optional(number, 0)
        max_nodes         = number
        boot_disk_gib     = optional(number, 128)
        labels            = optional(map(string), {})
        shared_filesystem = optional(bool, true) # attach and mount the region's weights filesystem (weights/data/assets on the node)
      })), {})
    }))

    # Images: with the cache (default), every image reference in the platform and the catalog names the logical
    # host, which each node resolves to its Spegel peers and the cluster's Zot pull-through cache. `source` is the
    # registry the platform images (services/*) are pushed to; default: the registry this solution creates in the
    # hub project (`stack.sh output cloud registry`). `upstreams`: third-party registries reachable as
    # <host>/<alias>/<path> (credentials for private ones come from the environment, see `secrets`).
    # cache.enabled = false leaves the cache out (docs/IMAGES.md "The cache as an add-on"): no Zot, no registry
    # credential, no containerd drop-in; the platform images are pulled from `source` as the nodes' own identity
    # allows, catalog and model images from their upstream registries, and the API leaves references as written.
    # Spegel stays either way.
    images = optional(object({
      host   = optional(string, "registry.serverless2.local")
      source = optional(string)
      cache = optional(object({
        enabled   = optional(bool, true)
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
        api        = optional(string, "0.11.112")
        dispatcher = optional(string, "0.2.12")
        jobs       = optional(string, "0.1.27")
        ops        = optional(string, "0.1.12")
      }), {})
    }), {})

    # USD per GPU-hour by platform (list prices); the scheduler orders pools by these and the cost export
    # prices node time with them. Reservations have zero marginal cost. The default covers every current
    # Nebius GPU platform (nebius.com/prices); an entry given here replaces the default one.
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
      gpu-rtx6000    = { on_demand = 1.80, spot = 1.09 } # RTX PRO 6000 Blackwell (us-central1 name)
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
      # Certificates of the GPU regions' gateways: "fleet-ca" (default) issues them from a CA the cloud stage
      # generates (no public reachability of a regional gateway needed; the control plane and LiteLLM trust it;
      # a browser on a regional Grafana sees a warning), "acme" asks Let's Encrypt per region (HTTP-01 through
      # the gateway, so port 80 of every regional gateway must answer the internet). The control cluster's
      # public hostnames always use ACME in `public` mode.
      regional_certificates = optional(string, "fleet-ca")
      # Further public hostnames on the control cluster's certificate, under the gateway host (<ip>.sslip.io or the
      # domain): what else answers behind the gateway, e.g. a console deployed next to the API as app.<gateway host>.
      extra_hostnames = optional(list(string), [])
      acme = optional(object({
        email   = optional(string, "")
        staging = optional(bool, false)
      }), {})
      api_rate_limit_per_minute = optional(number, 600)
      grafana_public            = optional(bool, true)
    }), {})

    # Knative Serving on every cluster (charts/platform knative-serving): the longest request/revision timeout an
    # endpoint may declare (`timeout_s`, seconds). The SAME value reaches Knative's config-defaults
    # (max-revision-timeout-seconds) and the API (KNATIVE_MAX_REVISION_TIMEOUT_S), which refuses a definition above it
    # at registration (400) instead of storing an endpoint that never reconciles. Raise it for long inferences (a
    # medical pipeline at 7500 s); never lower it below a registered endpoint's timeout_s.
    knative = optional(object({
      max_revision_timeout_s = optional(number, 3600)
    }), {})

    # Transitional acceptance of the admission tokens of API releases up to 0.11.53 at the gateway hook (key-only, key+model)
    # for the endpoint callers they rendered; false once nothing of them is in flight (docs/API.md "Endpoint calls").
    admitted_legacy = optional(bool, false)

    # Live sessions (docs/API.md "Sessions"): admitted streams to endpoints, bound to one connection and one worker pod.
    # Off by default; the workers of a model must implement the attach/release protocol before it is switched on.
    sessions = optional(object({
      enabled        = optional(bool, false)
      claim_ttl_s    = optional(number, 120)           # the connection must present its claim within this
      attach_wait_s  = optional(number, 60)            # from the first Ready replica observed (never from admission; capped by the session deadline): a bound session no worker attached within it ends with usage unknown
      token_audience = optional(string, "serverless2") # TokenReview audience of the workers' projected tokens (the lease clients' projection uses it)
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
    # variable NAMES given here. stack.sh refuses to run while a named variable is unset; an empty name ("")
    # switches that feature off (ngc_api_key_env = "": no NIM access, no NGC pull Secrets).
    secrets = optional(object({
      ngc_api_key_env               = optional(string, "NGC_API_KEY")          # NVIDIA NGC key (NIM images through the cache); "" = off
      hf_token_env                  = optional(string, "HF_TOKEN")             # Hugging Face token for gated weights (seed-weights, runtimes); "" = off
      registry_credentials_env      = optional(string, "REGISTRY_CREDENTIALS") # JSON {"<host>": {"username": "...", "password": "..."}} for other private upstreams; "" = off
      registry_key_env              = optional(string, "REGISTRY_STATIC_KEY")  # the image cache's static Container Registry key of the hub ops SA (issued once by stack/scripts/registry-static-key.sh); required with images.cache.enabled, ignored without
      mysterybox_credentials_secret = optional(string)                         # pre-provisioned subject-credentials.json, per cluster
      workload_secrets = optional(map(object({
        namespace        = string
        name             = string
        secret_id        = string
        version          = optional(string)
        create_namespace = optional(bool, false)
      })), {})
    }), {})

    # Tenants: namespace tenant-<name> on every cluster, a bucket + identity per region (cloud stage), LiteLLM API keys.
    # gpu_quota: GPUs the tenant may hold at once across the fleet (null = no cap: it shares the fleet
    # queues); fair_share_weight orders tenants when the fleet is full (Kueue weighted fair sharing).
    # dedicated_pools: pool names (as in regions.*.pools) only this tenant may use (its queues carry only those
    # flavors; the shared queues exclude them). allowed_images: image prefixes a tenant's container-run may
    # use (empty = any image through the cache).
    # Restricted assets (docs/JOBS.md "Restricted assets"): operator-registered files or pinned trees on the weights
    # filesystem (host paths on the nodes that mount it), mounted read-only on a run's main container only, by run
    # classes that name the alias, for tenants granted it (tenants.<name>.assets). kind: file | tree; gid: the group
    # the files are readable by (added to the pod's supplemental groups; no ownership change ever); sha256: the file's
    # digest, or a tree's inventory digest (identity recorded on every run); size: bytes, informational.
    assets = optional(map(object({
      kind      = string
      host_path = string
      gid       = optional(number, 65532)
      sha256    = optional(string)
      size      = optional(number)
    })), {})
    # Fleet operators manage tenant/key metadata, not customer inference. Their keys use the existing
    # bootstrap under tenant `platform`, remain separate from tenant admins, and are sensitive outputs.
    fleet_operator_keys = optional(set(string), [])
    tenants = optional(map(object({
      regions           = optional(list(string)) # default: all regions
      lifecycle_days    = optional(number, 90)   # run outputs under operations/ expire after N days (0 = keep)
      gpu_quota         = optional(number)
      fair_share_weight = optional(number, 1)        # Kueue weighted fair sharing between the tenants' queues (docs/FLEET.md "Tenants")
      dedicated_pools   = optional(list(string), []) # pools only this tenant's runs use: their quota is the tenant's alone, no endpoint lands there
      allowed_images    = optional(list(string), [])
      assets            = optional(list(string), []) # restricted assets (fleet `assets`) this tenant's run classes may mount
      # Execution options of this tenant's runs (docs/JOBS.md "Execution options").
      storage = optional(string, "managed") # managed: a bucket and S3 key per region for the runs' inputs and outputs; external: none
      #                                       (runs use application-managed I/O only; identity, queues and keys as usual)
      identities = optional(map(object({     # workload identities a run may name (workload_identity_ref): a ServiceAccount whose projected
        audience = string                    # token (this audience) the run presents to a storage provider's OIDC federation
        env      = optional(map(string), {}) # environment the run gets with it (e.g. AWS_ROLE_ARN); never a credential
      })), {})
      execution_profiles = optional(map(object({       # execution profiles a run may name (profile_ref): its pods reach only these
        egress_fqdns      = optional(list(string), []) # destinations (plus DNS, the API server, the gateway and the fleet's APIs),
        egress_cidrs      = optional(list(string), []) # and the shared filesystem stays out of the run unless allowed
        shared_filesystem = optional(bool, false)
      })), {})
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

    # The acceptance probe, tools/verify.sh: runs `model` (a built-in run class) with the first tenant key and,
    # when `example_endpoint` is true, creates the example endpoint `llm-example` (a stock vLLM container with a
    # small model, on the fleet's first GPU class) through the API with the first admin key and calls it. Run it
    # after the apply (`tools/verify.sh`, README.md). probe = true runs the same script as a Job on the control
    # cluster at apply time: the apply then waits for it (up to 40 minutes on a cold region) and fails with it.
    # The example stays as the first model of the console (delete it there).
    acceptance = optional(object({
      probe            = optional(bool, false)
      model            = optional(string, "hello-run")
      example_endpoint = optional(bool, true) # needs a tenant key with admin = true
    }), {})

  })

  validation {
    condition = alltrue([for name in var.fleet.fleet_operator_keys :
      can(regex("^[a-z0-9]([-a-z0-9]{0,38}[a-z0-9])?$", name)) &&
      !contains(flatten([for tn, t in var.fleet.tenants : [for kn in keys(t.keys) : "${tn}-${kn}"]]), "platform-operator-${name}")
    ])
    error_message = "fleet_operator_keys: names are DNS labels of at most 40 characters; platform-operator-<name> must not collide with a tenant key alias."
  }

  validation {
    condition = alltrue([for an, a in var.fleet.assets : contains(["file", "tree"], a.kind) && startswith(a.host_path, "/") && can(regex("^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$", an))
    && (a.sha256 == null || can(regex("^[0-9a-f]{64}$", a.sha256)))]) && alltrue([for tn, t in var.fleet.tenants : alltrue([for an in t.assets : contains(keys(var.fleet.assets), an)])])
    error_message = "assets: alias is a DNS label, kind is file or tree, host_path is absolute, sha256 is 64 hex digits; a tenant's assets name registered aliases."
  }

  validation {
    condition     = alltrue([for rn, r in var.fleet.regions : alltrue([for pn, p in r.cpu_pools : can(regex("^[0-9]+vcpu-[0-9]+gb$", p.preset)) && p.max_nodes >= 1])])
    error_message = "cpu_pools: preset is <n>vcpu-<n>gb (the pool's Kueue quota is derived from it) and max_nodes is at least 1."
  }

  validation {
    condition = alltrue([for tn, t in var.fleet.tenants : contains(["managed", "external"], t.storage)
    && alltrue([for n in concat(keys(t.identities), keys(t.execution_profiles)) : can(regex("^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$", n))])])
    error_message = "tenants: storage is managed or external; identity and execution profile names are DNS labels."
  }

  validation {
    condition = alltrue(flatten([for tn, t in var.fleet.tenants : [for p in t.dedicated_pools :
      length([for rn, r in var.fleet.regions : rn if contains(keys(r.pools), p)]) == 1
      && length([for tn2, t2 in var.fleet.tenants : tn2 if contains(t2.dedicated_pools, p)]) == 1
    ]]))
    error_message = "tenants.<name>.dedicated_pools: every name is a pool of exactly one region (dedicated pool names are fleet-wide) and dedicated to one tenant only."
  }
  validation {
    condition     = contains(["open", "proprietary", "auto"], var.fleet.gpu_operator.kernel_module) && (!var.fleet.gpu_operator.gds || var.fleet.gpu_operator.kernel_module != "proprietary")
    error_message = "gpu_operator.kernel_module is open, proprietary or auto; GPUDirect Storage (gds) needs the open kernel modules."
  }
  validation {
    condition     = var.fleet.node_reserve.ephemeral_fraction > 0 && var.fleet.node_reserve.ephemeral_fraction <= 1 && var.fleet.node_reserve.ephemeral_gib >= 0
    error_message = "node_reserve.ephemeral_fraction is in (0, 1] and node_reserve.ephemeral_gib is 0 or more (a reserve larger than the disk's share budgets zero scratch, which is allowed)."
  }
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
    # no certificate is ever issued: port 443 stays dark on every cluster.
    condition     = var.fleet.edge.mode != "public" || can(regex("^[^@[:space:]]+@[^@[:space:]]+\\.[^@[:space:]]+$", var.fleet.edge.acme.email)) && !can(regex("@(example\\.(com|net|org)|.*\\.(test|invalid|localhost))$", var.fleet.edge.acme.email))
    error_message = "edge.acme.email: a real mailbox for the Let's Encrypt account (renewal and incident mail); example.com, .test and .invalid are refused by the ACME server and leave the gateway without a certificate."
  }
  validation {
    condition     = !var.fleet.acceptance.example_endpoint || !var.fleet.acceptance.probe || anytrue(flatten([for tn, t in var.fleet.tenants : [for kn, k in t.keys : k.admin]]))
    error_message = "acceptance.example_endpoint defines the example model through the API, which needs a tenant key with admin = true (or set example_endpoint = false)."
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
    error_message = "pools: interconnect is none | infiniband; infiniband needs infiniband_fabric (the region's fabric name) and a full-node preset (8gpu-*, or 4gpu-* on gpu-gb300). The autoscaler never adds InfiniBand nodes (DRA): the dispatcher raises the pool's floor to a waiting multi-node run's node count and restores min_nodes afterwards; max_nodes bounds it (docs/SCHEDULING.md)."
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
    error_message = "pools: warm_nodes is between 0 and max_nodes, and 0 on an InfiniBand pool (the autoscaler never adds InfiniBand nodes; the dispatcher moves min_nodes there, docs/SCHEDULING.md)."
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
    condition     = contains(["fleet-ca", "acme"], var.fleet.edge.regional_certificates)
    error_message = "edge.regional_certificates is fleet-ca or acme."
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

# What the cloud stage created, as far as the platform's manifests depend on it: the gateway address and
# allocation of every cluster, the cluster ids, the registry, the backups bucket. The platform stage passes the
# cloud state's `clusters` and `hub` outputs; without them (tools/check.sh renders the charts for the example
# with no cloud access) deterministic placeholders stand in (locals.tf `cloud_facts`), so a render is reproducible.
variable "cloud" {
  description = "The cloud stage's outputs `clusters` and `hub` (null: placeholders for an offline render)."
  type        = any
  default     = null
}
